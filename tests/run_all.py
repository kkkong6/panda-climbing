"""攀搭全部本地检查（不连设备、不调用 Step；App 端到端需要本机装了 Chrome）。
运行：python tests/run_all.py
  1. 存储回归（线路登记 / 按键 / 改按，3 组）
  2. 新功能（线路笔记、运动数据、安全提醒、状态判断、旧库补列）
  3. App 数据与接口、音色复刻（登记 + 接口，不调用 Step）
  4. 固件运动算法（电脑上编译模拟）
  5. App 端到端（无头 Chrome 点一遍）
"""
import os, subprocess, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable


def run(name, *cmd, **kw):
    print(f"\n===== {name} =====")
    r = subprocess.run([PY, *cmd], cwd=ROOT, **kw)
    if r.returncode:
        print(f"❌ {name} 没通过")
    return r.returncode == 0


ok = [
    run("存储回归", "tests/run_store_checks.py"),
    run("新功能", "tests/test_new_features.py"),
    run("App 数据与接口", "tests/test_appdata.py"),
    run("音色复刻（登记）", "tests/test_voices.py"),
    run("音色复刻（接口）", "tests/test_voices_api.py"),
    run("固件运动算法", "tests/firmware_motion/run.py"),
]
app = subprocess.run(["bash", str(ROOT / "tests/app_e2e/run.sh")], cwd=ROOT, env={**os.environ, "PY": PY})
print(f"\n===== App 端到端 =====\n{'通过' if app.returncode == 0 else '❌ 没通过（需要本机装 Google Chrome）'}")
ok.append(app.returncode == 0)
print("\n" + ("全部通过 ✅" if all(ok) else "有检查没通过 ❌"))
sys.exit(0 if all(ok) else 1)
