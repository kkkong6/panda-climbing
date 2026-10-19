"""
攀搭 · App 接口（aiohttp，挂在服务端的 HTTP 端口上，默认 8003）

  GET  /panda/                      手机打开这个网址就是 App（同一个 Wi-Fi / 热点下）
  GET  /panda/api/devices           有记录的设备
  GET  /panda/api/state?device=     App 全部数据（见 panda/appdata.py）
  POST /panda/api/attempt           修改 / 删除一次记录 {device, id, action: send|fail|delete}
  POST /panda/api/note              加线路笔记 {device, route_id, text}；删除 {device, id, delete:true}
  POST /panda/api/chat              看板 AI 助手 {device, q, route_id?, history?:[{role, content}]}
  POST /panda/api/end               结束训练并生成复盘 {device}
  GET  /panda/api/voices            小搭的声音：有哪些音色、当前用的哪个
  POST /panda/api/voices            新录一个声音（multipart：audio 文件 + name；或 JSON {name}）+ 可选 text
  POST /panda/api/voices/select     切换音色 {id}
  POST /panda/api/voices/rename     改名 {id, name}
  POST /panda/api/voices/delete     删除 {id}
  GET  /panda/api/voices/preview?id=  试听（mp3；原声现场合成一句）

不填 device 时用最近活动的那台设备。看板助手和设备上的小搭是同一个人设、同一份训练档案。
"""
import asyncio
import json
import os
import re
import time
import types
import uuid
from concurrent.futures import ThreadPoolExecutor

from aiohttp import web

from config.logger import setup_logging
from panda import store, appdata, voices as voices_mod

TAG = __name__
logger = setup_logging()
_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="panda_api")

_last_seen = {}  # device_id → 最后一次收到设备消息的时间（判断「在线」）
ONLINE_SECONDS = 600


def seen(device_id):
    _last_seen[device_id] = time.time()


def online(device_id):
    t = _last_seen.get(device_id)
    return None if t is None else (time.time() - t < ONLINE_SECONDS)


CHAT_RULE = (
    "\n\n【现在你在手机 App 的聊天框里】用户爬完了，拿着手机在看数据。"
    "可以比在墙边说得多一点：两到四句话，先给结论，再说依据（具体到哪条线、几次、多高），最后给一个能马上做的建议。"
    "只用训练档案里的数字，档案里没有的不要编。不用写开头的语气提示括号。"
)


class PandaApi:
    def __init__(self, config):
        self.config = config
        self._llm = None

    # ---------- 工具 ----------
    def llm(self):
        if self._llm is None:
            from core.utils import llm as llm_mod
            name = self.config["selected_module"]["LLM"]
            conf = self.config["LLM"][name]
            self._llm = llm_mod.create_instance(conf.get("type", name), conf)
        return self._llm

    def _device(self, value):
        return value or appdata.default_device()

    @staticmethod
    def _json(data, status=200):
        return web.json_response(data, status=status, dumps=lambda x: json.dumps(x, ensure_ascii=False),
                                 headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "no-store"})

    async def _run(self, fn, *args):
        return await asyncio.get_running_loop().run_in_executor(_pool, fn, *args)

    async def _body(self, request):
        try:
            return await request.json()
        except Exception:
            return {}

    def app_path(self):
        here = os.path.dirname(os.path.abspath(__file__))
        cands = [(self.config.get("panda_app") or {}).get("app_path"),
                 os.path.join(here, "..", "..", "app", "攀搭App.html"),        # 仓库里：server/../app
                 os.path.expanduser("~/Desktop/攀搭项目/app/攀搭App.html")]     # 本机运行目录
        return next((p for p in cands if p and os.path.exists(p)), None)

    # ---------- 路由 ----------
    @staticmethod
    def make_app(config):
        """给 http_server 和测试用：把接口挂到一个 aiohttp 应用上（请求体上限 20MB，录音要上传）"""
        app = web.Application(client_max_size=20 * 1024 * 1024)
        app.add_routes(PandaApi(config).routes())
        return app

    def routes(self):
        return [
            web.get("/panda", self.page), web.get("/panda/", self.page),
            web.get("/panda/api/devices", self.devices),
            web.get("/panda/api/state", self.state),
            web.post("/panda/api/attempt", self.attempt),
            web.post("/panda/api/note", self.note),
            web.post("/panda/api/chat", self.chat),
            web.post("/panda/api/end", self.end),
            web.get("/panda/api/voices", self.voices),
            web.post("/panda/api/voices", self.voice_add),
            web.post("/panda/api/voices/select", self.voice_select),
            web.post("/panda/api/voices/rename", self.voice_rename),
            web.post("/panda/api/voices/delete", self.voice_delete),
            web.get("/panda/api/voices/preview", self.voice_preview),
            web.options("/panda/api/{tail:.*}", self.options),
        ]

    async def options(self, request):
        return web.Response(headers={"Access-Control-Allow-Origin": "*",
                                     "Access-Control-Allow-Headers": "Content-Type",
                                     "Access-Control-Allow-Methods": "GET, POST, OPTIONS"})

    async def page(self, request):
        p = self.app_path()
        if not p:
            return web.Response(text="找不到 App 页面，请在配置 panda_app.app_path 里写上 攀搭App.html 的路径", status=404)
        return web.FileResponse(p, headers={"Cache-Control": "no-store", "Content-Type": "text/html; charset=utf-8"})

    async def devices(self, request):
        return self._json({"devices": [dict(d, online=online(d["device"])) for d in await self._run(appdata.devices)]})

    async def state(self, request):
        dev = self._device(request.query.get("device"))
        if not dev:
            return self._json({"live": True, "empty": True})
        try:
            data = await self._run(appdata.state, dev, self.config, online(dev))
        except Exception as e:
            logger.bind(tag=TAG).exception(f"App 数据整理失败: {e}")
            return self._json({"error": str(e)}, 500)
        return self._json(data)

    async def attempt(self, request):
        b = await self._body(request)
        dev = self._device(b.get("device"))
        try:
            ok = await self._run(appdata.edit_attempt, dev, int(b.get("id")), b.get("action"))
        except (TypeError, ValueError):
            ok = False
        logger.bind(tag=TAG).info(f"App 修改记录 | 设备 {dev} | {b.get('id')} → {b.get('action')} | {'成功' if ok else '失败'}")
        if not ok:
            return self._json({"ok": False}, 400)
        return self._json({"ok": True, "state": await self._run(appdata.state, dev, self.config, online(dev))})

    async def note(self, request):
        b = await self._body(request)
        dev = self._device(b.get("device"))
        if b.get("delete"):
            ok = await self._run(store.delete_note, int(b.get("id") or 0))
        else:
            r = await self._run(appdata.route_of, dev, int(b.get("route_id") or 0))
            text = (b.get("text") or "").strip()
            ok = bool(r and text)
            if ok:
                res = await self._run(lambda: store.add_note(dev, text, color=r["color"], grade=r["grade"]))
                ok = res.get("ok")
                if ok:
                    from plugins_func.functions.panda_note import organize_guide
                    try:
                        llm = self.llm()
                    except Exception:
                        llm = None
                    await self._run(organize_guide, llm, res["route_id"], res["label"], res["notes"], "panda_app")
        if not ok:
            return self._json({"ok": False}, 400)
        return self._json({"ok": True, "state": await self._run(appdata.state, dev, self.config, online(dev))})

    def _chat_sync(self, dev, q, route_id, history):
        archive = store.archive(dev)
        system = self.config.get("prompt", "") + CHAT_RULE
        if archive:
            system += "\n\n【训练档案】（系统整理，数字准确）\n" + archive
        if route_id:
            r = appdata.route_of(dev, int(route_id))
            if r:
                notes = store.route_notes(r["id"])
                system += (f"\n\n【用户正在看的线路】{store.label(r['color'], r['grade'])}"
                           + (f"（{r['note']}）" if r["note"] else "")
                           + (f"；攻略：{r['guide']}" if r["guide"] else "")
                           + ("；笔记：" + "；".join(n["text"] for n in notes[-3:]) if notes else ""))
                rows = appdata.state(dev, self.config)["routes"]
                x = next((y for y in rows if y["rid"] == r["id"]), None)
                if x:
                    hs = [f"{s['d']}：" + "、".join("完攀" if ok else (f"{h:.1f}米" if h is not None else "失败")
                                                    for h, ok in zip(s["h"], s["ok"])) for s in x["history"]]
                    if x["today"]:
                        hs.append("今天：" + "、".join("完攀" if a["ok"] else (f"{a['h']:.1f}米" if a["h"] is not None else "失败")
                                                     for a in x["today"]))
                    if hs:
                        system += "；每次爬到的高度：" + "；".join(hs)
        msgs = [{"role": "system", "content": system}]
        for m in (history or [])[-6:]:
            if m.get("role") in ("user", "assistant") and m.get("content"):
                msgs.append({"role": m["role"], "content": str(m["content"])[:500]})
        msgs.append({"role": "user", "content": q})
        llm = self.llm()
        for _ in range(2):  # 大模型偶尔空回答，再试一次
            text = "".join(c for c in llm.response("panda_app", msgs, max_tokens=2000) if isinstance(c, str)).strip()
            text = re.sub(r"^[（(][^）)]{1,12}[）)]", "", text).strip()
            if text:
                return text
        return "我刚才没反应过来，再问一次？"

    async def chat(self, request):
        b = await self._body(request)
        dev = self._device(b.get("device"))
        q = (b.get("q") or "").strip()
        if not q:
            return self._json({"ok": False}, 400)
        t0 = time.time()
        try:
            text = await asyncio.wait_for(self._run(self._chat_sync, dev, q, b.get("route_id"), b.get("history")), 40)
        except asyncio.TimeoutError:
            text = "这次想得有点久，稍后再问我一次吧。"
        except Exception as e:
            logger.bind(tag=TAG).exception(f"App 聊天出错: {e}")
            return self._json({"ok": False, "error": str(e)}, 500)
        logger.bind(tag=TAG).info(f"App 聊天 | 设备 {dev} | {q} → {text} | {time.time() - t0:.1f}s")
        return self._json({"ok": True, "text": text})

    def _end_sync(self, dev):
        st = store.session_summary(dev, end=True)
        if not st or not st["attempts"]:
            return False
        store.save_review(st["session_id"], st)
        row = store.get_review(st["session_id"]) or {}
        if not row.get("full"):
            from plugins_func.functions.panda_review import _full_review
            try:
                conn = types.SimpleNamespace(llm=self.llm(), session_id="panda_app")
                _full_review(conn, st)
            except Exception as e:
                logger.bind(tag=TAG).warning(f"App 结束训练：完整复盘没生成（只显示数字）{e}")
        return True

    async def end(self, request):
        b = await self._body(request)
        dev = self._device(b.get("device"))
        try:
            ok = await asyncio.wait_for(self._run(self._end_sync, dev), 60)
        except asyncio.TimeoutError:
            ok = True  # 复盘还在后台生成，先返回数字
        return self._json({"ok": bool(ok), "state": await self._run(appdata.state, dev, self.config, online(dev))})

    # ---------- 小搭的声音（音色复刻） ----------
    async def voices(self, request):
        return self._json(await self._run(voices_mod.list_voices))

    async def voice_add(self, request):
        """手机录音上传 mp4/webm/wav 都行；服务器转 wav 后请 Step 复刻（后台做，约 30–60 秒）"""
        try:
            data = await request.post() if request.content_type.startswith("multipart/") else {}
        except Exception as e:
            return self._json({"ok": False, "error": f"上传失败：{e}"}, 400)
        if data:
            up = data.get("audio")
            name = data.get("name")
            text = data.get("text")
            if not up or not getattr(up, "file", None):
                return self._json({"ok": False, "error": "没有收到录音"}, 400)
            os.makedirs(voices_mod.VOICE_DIR, exist_ok=True)
            tmp = os.path.join(voices_mod.VOICE_DIR, f"upload_{uuid.uuid4().hex[:8]}")
            with open(tmp, "wb") as f:
                while True:
                    chunk = up.file.read(256 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
        else:
            b = await self._body(request)
            return self._json({"ok": False, "error": "要用 multipart 上传录音（audio 字段）"}, 400)
        try:
            rec = await self._run(voices_mod.add, tmp, name, self.config, text)
        except Exception as e:
            logger.bind(tag=TAG).exception(f"音色复刻没开始：{e}")
            return self._json({"ok": False, "error": str(e)}, 500)
        logger.bind(tag=TAG).info(f"新录的声音「{rec['name']}」开始复刻")
        return self._json({"ok": True, "voice": rec, **await self._run(voices_mod.list_voices)})

    async def voice_select(self, request):
        b = await self._body(request)
        r = await self._run(voices_mod.select, b.get("id"))
        return self._json(r, 200 if r.get("ok") else 400)

    async def voice_rename(self, request):
        b = await self._body(request)
        r = await self._run(voices_mod.rename, b.get("id"), b.get("name"))
        return self._json(r, 200 if r.get("ok") else 400)

    async def voice_delete(self, request):
        b = await self._body(request)
        r = await self._run(voices_mod.delete, b.get("id"))
        return self._json(r, 200 if r.get("ok") else 400)

    async def voice_preview(self, request):
        vid = request.query.get("id") or "buddy"
        p = await self._run(voices_mod.preview_now, self.config, vid)
        if not p:
            return self._json({"ok": False, "error": "这个音色还没有试听"}, 404)
        return web.FileResponse(p, headers={"Cache-Control": "no-cache", "Access-Control-Allow-Origin": "*"})
