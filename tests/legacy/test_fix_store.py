"""③ 三秒改按 · 测试：存储逻辑（临时数据库）"""
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

def press(result, cid, fix=False):
    r = store.record_attempt(DEV, result, cid, fix=fix)
    print(f"按键 {result:<4} id={cid:<3}{' 改按' if fix else '     '} → 屏幕「{ack_text(r)}」"); return r

register_route(conn, color="黄", grade="V3")
print("① 误按失败，3 秒内改成完攀")
press("fail", "a1")
r = press("send", "a1", fix=True)
check(r["fixed"] and r["count"] == 1 and r["sends"] == 1, "还是第 1 次，改成完攀")
check(ack_text(r).startswith("已改："), "屏幕说明已改")
print("② 再改回失败")
r = press("fail", "a1", fix=True); check(r["count"] == 1 and r["sends"] == 0, "还是第 1 次，失败")
print("③ 改按消息重发（网络重连）")
r = press("fail", "a1", fix=True); check(not r["fixed"] and r["count"] == 1, "结果没变，不算改动")
print("④ 原记录丢了，只收到改按")
r = press("send", "a2", fix=True); check(r["count"] == 2 and r["sends"] == 1, "直接记成第 2 次完攀")
print("⑤ 改按后，原来的非改按消息又到了（不该覆盖）")
r = press("fail", "a2"); check(r["duplicate"] and r["result"] == "send", "仍是完攀")
print("⑥ 未知线路也能改")
register_route(conn, color="粉", grade="V4")
r = press("fail", "b1"); r = press("send", "b1", fix=True)
check(r["route_label"] == "粉色V4" and r["count"] == 1 and r["sends"] == 1, "粉色V4 第1次完攀")
print("⑦ 全部记录")
allr = store.session_attempts(DEV)
print("  ", [(a["route"], a["result"]) for a in allr])
check(len(allr) == 3 and [a["result"] for a in allr] == ["fail", "send", "send"], "共 3 次：失败、完攀、完攀")
print("\n全部通过 ✅" if fails == 0 else f"\n{fails} 项失败 ❌")
