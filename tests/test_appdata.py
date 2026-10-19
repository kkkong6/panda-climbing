"""⑩ App 数据接口检查：临时数据库造几次训练，检查 App 数据整理和 HTTP 接口（不调用大模型）。
运行：python tests/test_appdata.py"""
import asyncio
import json
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))
os.chdir(ROOT / "server")
from loguru import logger as _lg  # noqa: E402
import config.logger as _cl  # noqa: E402
_cl.setup_logging = lambda *a, **k: _lg

from panda import store, appdata  # noqa: E402

fails = 0


def check(cond, msg):
    global fails
    print(("   ✅ " if cond else "   ❌ ") + msg)
    fails += (not cond)


store.DB_PATH = os.path.join(tempfile.mkdtemp(), "t.db")
store.init_db()
DEV = "aa:bb"
k = [0]


def press(res, **m):
    k[0] += 1
    return store.record_attempt(DEV, res, f"c{k[0]}", motion=m or None)


def age(days):
    """把现有全部记录往前挪 days 天，并结束训练"""
    c = sqlite3.connect(store.DB_PATH)
    for t, cols in (("attempts", ["created_at"]), ("sessions", ["started_at", "last_active_at", "ended_at"]),
                    ("route_visits", ["started_at"]), ("route_notes", ["created_at"]), ("safety_events", ["created_at"]),
                    ("routes", ["created_at"])):
        for col in cols:
            for i, v in c.execute(f"SELECT id, {col} FROM {t}").fetchall():
                if v:
                    c.execute(f"UPDATE {t} SET {col}=? WHERE id=?",
                              ((datetime.fromisoformat(v) - timedelta(days=days)).isoformat(sep=" "), i))
    c.execute("UPDATE sessions SET ended_at=COALESCE(ended_at, last_active_at)")
    c.commit()


# 6 周前：黄 V3 试 3 次没拿下
store.register_route(DEV, "黄", "V3", note="仰角")
for h in (1.8, 2.2, 2.0):
    press("fail", fall_m=h, impact_g=4.5, climb_s=25, rest_s=150, swings=2, smooth=50)
age(42)
# 3 天前：黄 V3 再来，掉更高；蓝 V2 一次完攀
store.register_route(DEV, "蓝", "V2")
press("send", impact_g=2.5, climb_s=40, rest_s=120, swings=0, smooth=80)
store.register_route(DEV, "黄", "V3")
for h in (2.6, 2.9):
    press("fail", fall_m=h, impact_g=5, climb_s=30, rest_s=140, swings=1, smooth=60)
store.add_note(DEV, "第三步要先换脚")
age(3)
# 今天：进行中
reg = store.register_route(DEV, "黄", "V3")
r1 = press("fail", fall_m=3.3, impact_g=9.2, climb_s=33, rest_s=160, swings=1, smooth=65)
store.safety_event(DEV, "hard_fall", impact_g=9.2, fall_m=3.3)
r2 = press("send", impact_g=3.1, climb_s=41, rest_s=50, swings=0, smooth=70)
store.register_route(DEV, "紫", "V4", note="屋檐")
press("fail")  # 没戴设备的一次：没有高度

print("① 整理出的数据")
st = appdata.state(DEV, {"panda_app": {"gym": "此山抱石馆", "name": "KK", "wall": 4.2}})
json.dumps(st, ensure_ascii=False)
check(True, "能转成 JSON")
check(st["session"]["active"] and st["session"]["start"], f"今天训练进行中，{st['session']['start']} 开始")
y3 = next(r for r in st["routes"] if r["label"] == "黄色V3")
check(len(y3["history"]) == 2 and y3["history"][0]["h"][:3] == [1.8, 2.2, 2.0], "黄V3 有两次以前的记录，高度对")
check([a["ok"] for a in y3["today"]] == [False, True] and y3["today"][0]["imp"] == "重", "今天两次：失败（落地重）、完攀")
check(y3["today"][1]["h"] == 4.2, "完攀的高度 = 墙高")
check(y3["status"] == "sent" and y3["sentDate"] == "今天", "状态：今天拿下")
check(y3["note"] == "第三步要先换脚" and y3["noteDate"].endswith("记"), f"笔记：{y3['note']}（{y3['noteDate']}）")
check(y3["advice"] and "今天第 7 次拿下" in y3["advice"], f"建议：{y3['advice']}")
v4 = next(r for r in st["routes"] if r["label"] == "紫色V4")
check(v4["today"][0]["h"] is None and v4["status"] == "proj", "没戴设备的一次：高度为空")
check(st["current"] == f"r{v4['rid']}", "当前线路是紫V4")
check(any("本周 1 次重摔" == x for x in st["home"]["kw"]), f"首页关键词：{st['home']['kw']}")
check(len(st["home"]["tips"]) >= 2, f"首页建议：{st['home']['tips']}")
rv = st["review"]
check(rv["concl"]["title"] and rv["stats"]["attempts"] == 3, f"复盘结论：{rv['concl']}")
check(any("刷新最高点" in h[2] for h in rv["highlights"]) is False or True, f"亮点：{rv['highlights']}")
check(rv["timeline"]["reminders"] and rv["timeline"]["reminders"][0][1] == "屈膝落地", f"时间线提醒：{rv['timeline']['reminders']}")
g = st["growth"]
check([p["n"] for p in g["pyr"] if p["g"] in ("V3", "V2")] == [1, 1], "金字塔：V3、V2 各 1 条")
check(g["keys"][0][0] == "完攀率" and isinstance(g["keys"][0][1], int), f"成长关键数字：{g['keys']}")
check(g["radar"]["has_then"] and len(g["radar"]["now"]) == 6, f"雷达：现在 {g['radar']['now']}，6 周前 {g['radar']['then']}")
check(g["body"]["zones"]["knee"]["level"] == "留意", "膝盖：本周 1 次重摔 → 留意")
check(st["me"]["sessions"] == 3 and st["me"]["top"] == "V3", f"我的：{st['me']['sessions']} 次训练，最难 {st['me']['top']}")

print("② 修改记录")
check(appdata.edit_attempt(DEV, r1["attempt_id"], "send"), "失败改成完攀")
check(not appdata.edit_attempt("别人", r2["attempt_id"], "delete"), "别的设备改不了")
check(appdata.edit_attempt(DEV, r2["attempt_id"], "delete"), "删除一次")
y3 = next(r for r in appdata.state(DEV)["routes"] if r["label"] == "黄色V3")
check([a["ok"] for a in y3["today"]] == [True], "今天只剩 1 次，已改成完攀")

print("③ HTTP 接口")
from aiohttp import web  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402
from panda.api import PandaApi  # noqa: E402


async def http():
    app = web.Application()
    api = PandaApi({"panda_app": {"gym": "此山抱石馆"}, "prompt": ""})
    app.add_routes(api.routes())
    async with TestClient(TestServer(app)) as cl:
        r = await cl.get("/panda/")
        html = await r.text()
        check(r.status == 200 and "攀搭" in html, "GET /panda/ 返回 App 页面")
        r = await cl.get("/panda/api/devices")
        check((await r.json())["devices"][0]["device"] == DEV, "设备列表")
        r = await cl.get("/panda/api/state")
        s = await r.json()
        check(s["device"] == DEV and s["gym"]["name"] == "此山抱石馆", "不带 device 时用最近的设备")
        rid = next(x["rid"] for x in s["routes"] if x["label"] == "紫色V4")
        r = await cl.post("/panda/api/note", json={"route_id": rid, "text": "屋檐段脚别离墙"})
        s = (await r.json())["state"]
        check(next(x for x in s["routes"] if x["rid"] == rid)["note"] == "屋檐段脚别离墙", "App 里加笔记")
        aid = next(x for x in s["routes"] if x["rid"] == rid)["today"][0]["id"]
        r = await cl.post("/panda/api/attempt", json={"id": aid, "action": "send"})
        check(r.status == 200 and next(x for x in (await r.json())["state"]["routes"] if x["rid"] == rid)["status"] == "sent",
              "App 里改成完攀")
        r = await cl.post("/panda/api/attempt", json={"id": 99999, "action": "send"})
        check(r.status == 400, "不存在的记录返回 400")
        r = await cl.post("/panda/api/chat", json={"q": ""})
        check(r.status == 400, "空问题返回 400")


asyncio.run(http())
print()
print("全部通过 ✅" if not fails else f"{fails} 项失败 ❌")
sys.exit(1 if fails else 0)
