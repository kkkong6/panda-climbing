"""10/19 新功能检查：⑨ 线路笔记、⑪ 运动数据、⑫ 安全提醒、状态判断、旧数据库补列。
临时数据库，不调用大模型、不连设备。运行：python tests/test_new_features.py"""
import os
import sys
import sqlite3
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))
os.chdir(ROOT / "server")
from loguru import logger as _lg  # noqa: E402
import config.logger as _cl  # noqa: E402
_cl.setup_logging = lambda *a, **k: _lg  # 不读真实配置

from panda import store, moments, safety  # noqa: E402

fails = 0


def check(cond, msg):
    global fails
    print(("   ✅ " if cond else "   ❌ ") + msg)
    fails += (not cond)


def fresh_db():
    store.DB_PATH = os.path.join(tempfile.mkdtemp(), "t.db")
    store.init_db()
    moments.reset_cooldown()


def shift(minutes, table="attempts"):
    """把已有记录整体往前挪（模拟过了一段时间）"""
    c = sqlite3.connect(store.DB_PATH)
    for t, cols in (("attempts", ["created_at"]), ("sessions", ["started_at", "last_active_at"]),
                    ("route_visits", ["started_at"]), ("route_notes", ["created_at"]), ("safety_events", ["created_at"])):
        for col in cols:
            rows = c.execute(f"SELECT id, {col} FROM {t}").fetchall()
            for i, v in rows:
                if v:
                    c.execute(f"UPDATE {t} SET {col}=? WHERE id=?",
                              ((datetime.fromisoformat(v) - timedelta(minutes=minutes)).isoformat(sep=" "), i))
    c.commit()
    c.close()


cfg = moments.settings({})
DEV = "dev"
n = [0]


last = [None]


def press(result, **motion):
    n[0] += 1
    r = store.record_attempt(DEV, result, f"k{n[0]}", motion=motion or None)
    last[0] = r["attempt_id"]
    return r, moments.judge(store.attempt_context(DEV, r["attempt_id"]), cfg)


print("① 运动数据：清洗、存储、改按不覆盖")
fresh_db()
check(store.clean_motion({"fall_m": "2.84", "impact_g": 40, "swings": 2.6, "smooth": True, "x": 1}) ==
      {"fall_m": 2.84, "swings": 3}, "超范围 / 布尔 / 不认识的字段被丢掉")
store.register_route(DEV, "黄", "V3")
r, _ = press("fail", fall_m=2.1, impact_g=5.2, climb_s=31, rest_s=150, swings=1, smooth=70)
check(r["motion"]["fall_m"] == 2.1 and r["motion"]["climb_s"] == 31, "按键带的运动数据存下来了")
r2 = store.record_attempt(DEV, "send", r and f"k{n[0]}", fix=True)
check(r2["fixed"] and r2["motion"]["fall_m"] == 2.1, "改按后运动数据还在")
from core.handle.textHandler.pandaMessageHandler import ack_text  # noqa: E402
r3, _ = press("fail", fall_m=2.6)
check(ack_text(r3).endswith("2.6米"), f"屏幕确认带高度：{ack_text(r3)}")
check(store.impact_level(3) == "轻" and store.impact_level(5) == "中" and store.impact_level(9) == "重", "冲击分级 轻/中/重")

print("② 高度新高：比这条线之前最好高 0.3 米以上才说")
fresh_db()
store.register_route(DEV, "紫", "V4")
_, m = press("fail", fall_m=1.8)
check(m is None, "第一次没有「之前最好」，不说")
_, m = press("fail", fall_m=2.0)
check(m is None, "只高了 0.2 米，不说")
_, m = press("fail", fall_m=2.5)
check(m and m["kind"] == "高度新高", "高了 0.5 米 → 高度新高")
check(m and "2.5米" in m["facts"] and "2.0米" in m["facts"], f"事实里说清楚比较对象：{m and m['facts']}")
line = moments.template_line(m, store.attempt_context(DEV, last[0]))
check("0.5米" in line, f"模板台词带高度差：{line}")
_, m = press("fail", fall_m=0.5)
check(m is None, "掉得低不说")

print("③ 休息过短：连续 3 次都歇不到 1 分钟（只认设备测的）")
fresh_db()
store.register_route(DEV, "蓝", "V2")
press("fail", rest_s=200)
_, m = press("fail", rest_s=40)
_, m = press("fail", rest_s=30)
check(m is None, "只有 2 次短，不提醒")
_, m = press("fail", rest_s=45)
check(m and m["kind"] == "休息过短" and m["vibrate"] == "long2", "连续 3 次短 → 长震 2 下提醒")


class FakeConn:
    config = {}
    llm = None
    session_id = "t"


found = moments.check(FakeConn(), DEV, last[0])
check(found is not None, "check() 开口")
check(store.safety_summary(DEV).get("rest_short") == 1, "记了一条安全事件 rest_short")
_, m = press("fail", rest_s=20)
check(moments.check(FakeConn(), DEV, last[0]) is None, "10 分钟内不重复提醒")
fresh_db()
store.register_route(DEV, "蓝", "V2")
for _ in range(5):
    _, m = press("fail")
check(m is None or m["kind"] != "休息过短", "没有设备数据时不会误报")

print("④ 状态判断：下落变低 / 休息变长 / 失败变多 中两项")
fresh_db()
store.register_route(DEV, "红", "V3")
for h, res, rest in [(3.0, "fail", 120), (3.2, "send", 110), (3.1, "fail", 130), (3.3, "send", 120),
                     (2.0, "fail", 300), (1.8, "fail", 320), (1.9, "fail", 310), (1.7, "fail", 330)]:
    _, m = press(res, fall_m=h, rest_s=rest)
cond = store.session_condition(DEV)
check(cond["tired"] and len(cond["signals"]) == 3, f"三项都中：{cond['signals']}")
check(cond["advice"] in ("收工", "换线"), f"建议：{cond['advice']}")
check(m and m["kind"] == "状态下滑", "触发「状态下滑」特别时刻")
moments.check(FakeConn(), DEV, last[0])
_, m = press("fail", fall_m=1.5, rest_s=340)
moments.reset_cooldown(DEV)  # 只清冷却，不清「本次训练说过」
check(m is None or m["kind"] != "状态下滑" or moments.check(FakeConn(), DEV, last[0]) is None, "同一次训练只提醒一次")
check("【状态】看起来累了" in store.archive(DEV), "档案里写了状态")
fresh_db()
store.register_route(DEV, "红", "V3")
for _ in range(8):
    press("fail", fall_m=2.0, rest_s=120)
check(not store.session_condition(DEV)["tired"], "一直稳定失败不算累")

print("⑤ 线路笔记：记录、找线、登记时提醒、档案")
fresh_db()
check(store.add_note(DEV, "第三步要换脚")["reason"] == "no_route", "没登记线路时不知道记到哪")
store.register_route(DEV, "黄", "V3", note="仰角")
r = store.add_note(DEV, "第三步要先换脚。")
check(r["ok"] and r["label"] == "黄色V3" and r["text"] == "第三步要先换脚", "默认记到当前线路，去掉句号")
store.register_route(DEV, "粉", "V4")
r = store.add_note(DEV, "起步右手先抓大点", color="黄色", grade="V3")
check(r["ok"] and r["label"] == "黄色V3" and r["count"] == 2, "说了颜色难度就记到那条")
from plugins_func.functions import panda_note, panda_route  # noqa: E402
check(panda_note.clean("记一下，第三步要先换角") == "第三步要先换脚", "去掉「记一下」并纠正错字")
check(panda_note.organize_guide(None, r["route_id"], "黄色V3", ["只有一条"]) == "只有一条", "只有一条笔记时直接当攻略")
store.set_guide(r["route_id"], "起步右手抓大点，第三步先换脚")
shift(60 * 24 * 3)  # 三天后
reg = store.register_route(DEV, "黄", "V3")
reply = panda_route.build_reply(reg)
check("起步右手抓大点，第三步先换脚" in reply, f"登记同一条线时提醒：{reply}")
arc = store.archive(DEV)
check("【这条线的笔记】攻略：起步右手抓大点" in arc and "第三步要先换脚" in arc, "档案里有攻略和原话")
reg2 = store.register_route(DEV, "绿", "V1")
check("提醒一下" not in panda_route.build_reply(reg2), "没笔记的线不提醒")
store.set_guide(r["route_id"], "")
reg = store.back_to_previous(DEV)
check("你10月" in panda_route.build_reply(reg) or "你" in panda_route.build_reply(reg), "没有攻略时用最近一条原话")

print("⑥ 安全事件：重摔记录、本周次数、关心话模板、闲置提醒")
fresh_db()
store.register_route(DEV, "黄", "V3", note="仰角")
ev = store.safety_event(DEV, "hard_fall", impact_g=9.5, fall_m=2.8)
check(ev["week_hard_falls"] == 1 and ev["route_label"] == "黄色V3", "第一次重摔")
check("还好吗" in safety.template_fall(ev), "模板关心话")
ev = store.safety_event(DEV, "hard_fall", impact_g=10.1)
check(ev["week_hard_falls"] == 2 and "仰角" in ev["week_hard_routes"][0], "本周第二次，记得是仰角线")
check("这周第2次" in safety.template_fall(ev), safety.template_fall(ev))
check("最近7天第2次重摔" in safety.hard_fall_facts(ev, 10.1, None), "给大模型的事实里有本周次数")
ev, line = safety.on_fall(FakeConn(), DEV, 9.0, 2.0)
check(ev["week_hard_falls"] == 3 and "还好吗" in line, "on_fall 没有大模型时用模板")
check("【安全】最近7天重摔3次" in store.archive(DEV), "档案里有安全情况")
check(safety.idle_line(DEV, 30) is None, "今天还没按过键，不打扰")
press("fail")
check("今天结束了吗" in safety.idle_line(DEV, 30), "有记录 → 问是不是结束了")
store.session_summary(DEV, end=True)
check(safety.idle_line(DEV, 30) is None, "训练已结束，不再问")

print("⑦ 复盘统计里的运动数据")
fresh_db()
store.register_route(DEV, "黄", "V3")
press("fail", fall_m=2.0, impact_g=3.0, rest_s=100)
press("fail", fall_m=2.6, impact_g=6.0, rest_s=40)
press("send", fall_m=4.2, impact_g=9.0, rest_s=200)
st = store.session_summary(DEV)
check(st["routes"][0]["best_fall"] == 2.6, "最高掉落 2.6 米（只算失败）")
check(st["motion"]["impact"] == {"轻": 1, "中": 1, "重": 1} and st["motion"]["short_rests"] == 1, f"冲击分级和休息：{st['motion']}")
from plugins_func.functions.panda_review import facts_text  # noqa: E402
ft = facts_text(st)
check("最高掉在" not in ft and "落地冲击：轻1次、中1次、重1次" in ft, "完攀的线不写掉落高度；写了冲击")

print("⑧ 旧数据库自动补列")
p = os.path.join(tempfile.mkdtemp(), "old.db")
c = sqlite3.connect(p)
c.executescript("""CREATE TABLE attempts (id INTEGER PRIMARY KEY, session_id INTEGER, route_id INTEGER, result TEXT,
  created_at TEXT, client_id TEXT, deleted INTEGER DEFAULT 0);
  CREATE TABLE routes (id INTEGER PRIMARY KEY, device_id TEXT, color TEXT, grade INTEGER, note TEXT, created_at TEXT, retired_at TEXT);""")
c.close()
store.DB_PATH = p
store.init_db()
cols = {r[1] for r in sqlite3.connect(p).execute("PRAGMA table_info(attempts)")}
check({"fall_m", "impact_g", "rest_s"} <= cols, "attempts 补上运动数据列")
check("guide" in {r[1] for r in sqlite3.connect(p).execute("PRAGMA table_info(routes)")}, "routes 补上 guide 列")

print()
print("全部通过 ✅" if not fails else f"{fails} 项失败 ❌")
sys.exit(1 if fails else 0)
