"""音色复刻的 App 接口（不调用 Step，不会真的复刻）：上传录音 → 登记 → 列表 → 切换 / 改名 / 删除。
运行：python tests/test_voices_api.py"""
import asyncio, os, sys, tempfile
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server")); os.chdir(ROOT / "server")
from loguru import logger as _lg
import config.logger as _cl; _cl.setup_logging = lambda *a, **k: _lg
from panda import voices
voices.use_dir(tempfile.mkdtemp())
from aiohttp import web, FormData
from aiohttp.test_utils import TestClient, TestServer
from panda.api import PandaApi

fails = 0
def check(c, m):
    global fails
    print(("   ✅ " if c else "   ❌ ") + m); fails += (not c)

async def main():
    async with TestClient(TestServer(PandaApi.make_app({"TTS": {"StepTTS": {"api_key": "", "voice": "yuanqishaonv"}}, "prompt": ""}))) as cl:
        r = await cl.get("/panda/api/voices")
        d = await r.json()
        check(d["current"] == "buddy" and len(d["voices"]) == 1, "默认只有小搭原声")
        # 上传一段假录音（不是真音频，只验接口；复刻会因为没 Key 失败，这正是我们要看的行为）
        form = FormData()
        form.add_field("audio", b"fake-audio-bytes", filename="voice.webm", content_type="audio/webm")
        form.add_field("name", "小然")
        r = await cl.post("/panda/api/voices", data=form)
        d = await r.json()
        check(r.status == 200 and d["ok"] and any(v["name"] == "小然" for v in d["voices"]), "上传录音并登记")
        vid = next(v["id"] for v in d["voices"] if v["name"] == "小然")
        await asyncio.sleep(1.0)
        d = await (await cl.get("/panda/api/voices")).json()
        v = next(x for x in d["voices"] if x["id"] == vid)
        check(v["status"] == "failed" and v.get("error"), f"没 Key 时复刻失败并写原因（App 会提示）：{v.get('error')}")
        # 模拟复刻成功后再测切换 / 改名 / 删除
        with voices._lock:
            dd = voices.load(); next(x for x in dd["voices"] if x["id"] == vid).update(status="ready", voice_id="voice-x"); voices._save(dd)
        d = await (await cl.post("/panda/api/voices/select", json={"id": vid})).json()
        check(d["ok"] and d["current"] == vid, "切换到这个声音")
        d = await (await cl.post("/panda/api/voices/rename", json={"id": vid, "name": "小然然"})).json()
        check(d["ok"] and next(x for x in d["voices"] if x["id"] == vid)["name"] == "小然然", "改名")
        r = await cl.get("/panda/api/voices/preview?id=" + vid)
        check(r.status in (200, 404), f"试听接口可用（{r.status}，没有试听文件时 404）")
        d = await (await cl.post("/panda/api/voices/delete", json={"id": vid})).json()
        check(d["ok"] and d["current"] == "buddy", "删除后换回原声")
        r = await cl.post("/panda/api/voices", data=await _json_only())
        check(r.status == 400, "不带录音的请求返回 400")
        r = await cl.post("/panda/api/voices/rename", json={"id": "buddy", "name": "x"})
        check(r.status == 400, "原声不能改名")

async def _json_only():
    return {"name": "x"}

asyncio.run(main())
print()
print("全部通过 ✅" if not fails else f"{fails} 项失败 ❌")
sys.exit(1 if fails else 0)
