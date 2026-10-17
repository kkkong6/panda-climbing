"""线路登记 · 测试 1：存储逻辑（用临时数据库，不碰真实数据）"""
import os, sys, tempfile
SERVER = os.path.expanduser("~/panda-server/xiaozhi-esp32-server-main/main/xiaozhi-server")
os.chdir(SERVER); sys.path.insert(0, SERVER)
from panda import store
store.DB_PATH = os.path.join(tempfile.mkdtemp(), "test.db"); store.init_db()

import types
from plugins_func.functions.panda_route import register_route, build_reply
conn = types.SimpleNamespace(device_id="test-dev", headers={})

def say(desc, **kw):
    r = register_route(conn, **kw)
    print(f"{desc:<28} → {r.response}")

fails = 0
def check(cond, msg):
    global fails
    if not cond:
        fails += 1; print("   ✗", msg)

# 解析
for s, want in [("黄", "黄"), ("黄色", "黄"), ("粉红", "粉"), ("Blue", "蓝"), ("橙色", None)]:
    check(store.normalize_color(s) == want, f"颜色 {s} → {store.normalize_color(s)}，应为 {want}")
for s, want in [("V3", 3), ("v 4", 4), ("3", 3), ("微三", 3), ("V0", 0), ("V10", None), ("七", None)]:
    check(store.normalize_grade(s) == want, f"难度 {s} → {store.normalize_grade(s)}，应为 {want}")
print("解析测试完成")

say("① 第一次登记", color="黄", grade="V3")
say("② 换线", color="粉", grade="V4", note="仰角")
say("③ 重复说同一条", color="粉", grade="V4")
say("④ 回到刚才那条", back_to_previous=True)
check([r["label"] for r in store.today_routes("test-dev") if r["current"]] == ["黄色V3"], "当前线路应为黄色V3")
say("⑤ 登记错了", color="红", grade="V2")
say("⑥ 纠正：是蓝色V2", color="蓝", grade="V2", correction=True)
labels = [r["label"] for r in store.today_routes("test-dev")]
check("红色V2" not in labels and "蓝色V2" in labels, f"纠正后应没有红色V2：{labels}")
say("⑦ 只说颜色", color="绿")
say("⑧ 只说难度", grade="V5")
say("⑨ 同色同级另一条（新描述）", color="粉", grade="V4", note="屋檐下")
say("⑩ 粉V4 仰角（应认出旧的）", color="粉", grade="V4", note="仰角")
say("⑪ 岩馆换线了", color="黄", grade="V3", new_route=True)
print("今天的线路：", store.today_routes("test-dev"))

# 模拟下一次训练：把本次训练时间往前拨 1 天
import sqlite3
c = sqlite3.connect(store.DB_PATH)
c.execute("UPDATE sessions SET started_at=datetime(started_at,'-7 day'), last_active_at=datetime(last_active_at,'-7 day')")
c.execute("UPDATE route_visits SET started_at=datetime(started_at,'-7 day')"); c.commit(); c.close()
say("⑫ 一周后再来：粉V4仰角", color="粉", grade="V4", note="仰角")
say("⑬ 一周后：回到上一条（没有）", back_to_previous=True)

print("\n全部通过 ✅" if fails == 0 else f"\n{fails} 项失败 ❌")

# 换线后旧线路应下架：再说黄V3 应该匹配到新的那条，且只剩 1 条在架
c = sqlite3.connect(store.DB_PATH)
active = c.execute("SELECT COUNT(*) FROM routes WHERE color='黄' AND grade=3 AND retired_at IS NULL").fetchone()[0]
print("在架的黄色V3：", active, "条 ✅" if active == 1 else "条 ❌")

# 同音错字纠正 + “新换的线”不当描述
r = register_route(conn, color="橙", grade="V3"); print("⑭ 不支持的颜色 →", r.response)
r = register_route(conn, color="红", grade="V2"); r = register_route(conn, color="粉", grade="V4", note="羊角")
print("⑮ 描述听成羊角 →", r.response)
check("仰角" in (r.response or ""), "羊角应纠正为仰角")
r = register_route(conn, color="绿", grade="V1", note="新换的线", new_route=True); print("⑯ 描述是“新换的线” →", r.response)
check("新换的线" not in (r.response or ""), "“新换的线”不应存成描述")
print("全部通过 ✅" if fails == 0 else f"{fails} 项失败 ❌")
