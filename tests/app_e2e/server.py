"""App 端到端检查用的临时服务：临时数据库 + 造好的训练数据 + 假的大模型（不联网）。
python app_e2e_server.py 18003"""
import os, sys, sqlite3, tempfile, asyncio
from datetime import datetime, timedelta
from pathlib import Path
SERVER = str(Path(__file__).resolve().parents[2] / "server")
sys.path.insert(0, SERVER); os.chdir(SERVER)
from loguru import logger as _lg
import config.logger as _cl
_cl.setup_logging = lambda *a, **k: _lg
from panda import store
from aiohttp import web
from panda.api import PandaApi, seen

store.DB_PATH = os.path.join(tempfile.mkdtemp(), "t.db"); store.init_db()
DEV = "e2:e2"; k = [0]
def press(res, **m):
    k[0] += 1; return store.record_attempt(DEV, res, f"c{k[0]}", motion=m or None)
def age(days):
    c = sqlite3.connect(store.DB_PATH)
    for t, cols in (("attempts", ["created_at"]), ("sessions", ["started_at", "last_active_at", "ended_at"]),
                    ("route_visits", ["started_at"]), ("route_notes", ["created_at"]), ("safety_events", ["created_at"]), ("routes", ["created_at"])):
        for col in cols:
            for i, v in c.execute(f"SELECT id, {col} FROM {t}").fetchall():
                if v: c.execute(f"UPDATE {t} SET {col}=? WHERE id=?", ((datetime.fromisoformat(v) - timedelta(days=days)).isoformat(sep=" "), i))
    c.execute("UPDATE sessions SET ended_at=COALESCE(ended_at, last_active_at)"); c.commit()
def minutes_ago(m):
    c = sqlite3.connect(store.DB_PATH)
    for t, col in (("attempts", "created_at"), ("route_visits", "started_at"), ("safety_events", "created_at")):
        c.execute(f"UPDATE {t} SET {col}=datetime({col}, '-{m} minutes') WHERE {col} >= datetime('now','localtime','-1 minute')")
    c.commit()

store.register_route(DEV, "黄", "V3", note="仰角")
for h in (1.8, 2.2, 2.0): press("fail", fall_m=h, impact_g=4.5, climb_s=25, rest_s=150, swings=2, smooth=50)
store.register_route(DEV, "红", "V4", note="侧拉")
for h in (2.0, 2.6): press("fail", fall_m=h, impact_g=5, climb_s=30, rest_s=160, swings=1, smooth=55)
age(40)
store.register_route(DEV, "蓝", "V2"); press("send", impact_g=2.5, climb_s=40, rest_s=120, swings=0, smooth=80)
store.register_route(DEV, "黄", "V3")
for h in (2.4, 2.6): press("fail", fall_m=h, impact_g=5, climb_s=30, rest_s=140, swings=1, smooth=60)
store.add_note(DEV, "第三手是反提，先把左脚挂上右边的大点")
store.register_route(DEV, "红", "V4"); press("fail", fall_m=3.3, impact_g=6); press("send", impact_g=3, climb_s=50)
age(3)
c = sqlite3.connect(store.DB_PATH); c.execute("UPDATE sessions SET started_at=datetime('now','localtime','-58 minutes'), last_active_at=datetime('now','localtime') WHERE ended_at IS NULL"); c.commit()
store.register_route(DEV, "绿", "V2"); press("send", impact_g=2, climb_s=35, rest_s=200, smooth=82)
store.register_route(DEV, "黄", "V3")
for h, imp in ((2.1, 5), (2.9, 6), (3.4, 9.5)): press("fail", fall_m=h, impact_g=imp, climb_s=33, rest_s=90, swings=1, smooth=62)
store.safety_event(DEV, "hard_fall", impact_g=9.5, fall_m=3.4)
store.register_route(DEV, "紫", "V4", note="屋檐"); press("fail", fall_m=1.8, impact_g=5); press("fail")
seen(DEV)


class FakeLLM:
    def response(self, session_id, msgs, **kw):
        sys_ = msgs[0]["content"]
        yield "（轻松地）" + ("看了档案：" if "【训练档案】" in sys_ else "没档案：") + msgs[-1]["content"][:12] + \
              ("。正在看黄色V3" if "【用户正在看的线路】黄色V3" in sys_ else "") + "。"


api = PandaApi({"panda_app": {"gym": "此山抱石馆", "name": "KK", "wall": 4.2}, "prompt": "你是小搭"})
api._llm = FakeLLM()
app = web.Application(); app.add_routes(api.routes())
web.run_app(app, host="127.0.0.1", port=int(sys.argv[1]), print=lambda *a: print("ready", flush=True))
