"""② 按键记录 · 测试 1：存储逻辑（临时数据库）"""
import os, sys, tempfile, types
SERVER = os.path.expanduser("~/panda-server/xiaozhi-esp32-server-main/main/xiaozhi-server")
os.chdir(SERVER); sys.path.insert(0, SERVER)
from panda import store
store.DB_PATH = os.path.join(tempfile.mkdtemp(), "t.db"); store.init_db()
from plugins_func.functions.panda_route import register_route
from core.handle.textHandler.pandaMessageHandler import ack_text

DEV = "dev"; conn = types.SimpleNamespace(device_id=DEV, headers={})
fails = 0
def check(cond, msg):
    global fails
    print(("   ✅ " if cond else "   ❌ ") + msg); fails += (not cond)

def press(result, cid):
    r = store.record_attempt(DEV, result, cid); print(f"按键 {result:<4} id={cid:<4} → 屏幕「{ack_text(r)}」"); return r

print("① 还没登记线路就按键")
r = press("fail", "a1"); check(r["route_label"] is None and r["count"] == 1 and r["session_started"], "记到未知线路，训练开始")
r = press("fail", "a2"); check(r["count"] == 2, "未知线路第 2 次")
print("② 说出线路 → 前面 2 次归进去")
say = register_route(conn, color="黄", grade="V3").response; print("   小搭：", say)
check("刚才2次也记到这条了" in say, "确认语提到收养了 2 次")
r = press("fail", "a3"); check(r["route_label"] == "黄色V3" and r["count"] == 3, "接着是黄色V3 第3次")
print("③ 网络重发同一个按键")
r = press("fail", "a3"); check(r["duplicate"] and r["count"] == 3, "重复的不再计数")
r = press("send", "a4"); check(r["result"] == "send" and r["count"] == 4 and r["sends"] == 1, "完攀 第4次")
print("④ 换线后按键")
register_route(conn, color="粉", grade="V4")
r = press("fail", "b1"); check(r["route_label"] == "粉色V4" and r["count"] == 1, "粉色V4 第1次")
print("⑤ 登记错了，按了 2 次后更正")
register_route(conn, color="红", grade="V2")
press("fail", "c1"); press("fail", "c2")
say = register_route(conn, color="蓝", grade="V2", correction=True).response; print("   小搭：", say)
check("刚才2次也改过来了" in say, "更正时提到搬过去 2 次")
r = press("fail", "c3"); check(r["route_label"] == "蓝色V2" and r["count"] == 3, "蓝色V2 第3次（含搬过来的 2 次）")
import sqlite3
db = sqlite3.connect(store.DB_PATH)
check(db.execute("SELECT COUNT(*) FROM routes WHERE color='红'").fetchone()[0] == 0, "误建的红色V2 已删除")
print("⑥ 回到黄色V3，次数接着数")
register_route(conn, back_to_previous=True)  # 回到上一条：粉V4
register_route(conn, color="黄", grade="V3")
r = press("fail", "d1"); check(r["route_label"] == "黄色V3" and r["count"] == 5, "黄色V3 第5次")
print("⑦ 不合法的结果、没有 id 的按键")
check(not store.record_attempt(DEV, "maybe")["ok"], "未知结果被拒绝")
r1 = store.record_attempt(DEV, "fail"); r2 = store.record_attempt(DEV, "fail")
check(r2["count"] == r1["count"] + 1, "没有 id 时每次都记")
print("⑧ 本次训练全部记录")
allr = store.session_attempts(DEV)
print("  ", [(a["route"], a["result"]) for a in allr])
check(len(allr) == 11 and sum(a["result"] == "send" for a in allr) == 1, "共 11 次，1 次完攀")
print("\n全部通过 ✅" if fails == 0 else f"\n{fails} 项失败 ❌")
