"""音色复刻（不调用 Step）：登记、切换、改名、删除、删掉在用的退回原声、复刻失败的处理、TTS 取音色。
运行：python tests/test_voices.py"""
import os, sys, tempfile
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server")); os.chdir(ROOT / "server")
from loguru import logger as _lg
import config.logger as _cl
_cl.setup_logging = lambda *a, **k: _lg
from panda import voices

fails = 0
def check(c, m):
    global fails
    print(("   ✅ " if c else "   ❌ ") + m); fails += (not c)

voices.use_dir(tempfile.mkdtemp())
cfg = {"TTS": {"StepTTS": {"api_key": "", "voice": "yuanqishaonv"}}}

print("① 默认只有原声")
d = voices.list_voices()
check(d["current"] == "buddy" and len(d["voices"]) == 1 and d["voices"][0]["sys"], "默认用小搭原声")
check(voices.current_voice(cfg) == ("yuanqishaonv", None), "TTS 用原声，模型不变（3 代）")

print("② 新录一个：先登记成 composing，没有 Key 时复刻失败并写原因")
tmp = os.path.join(tempfile.mkdtemp(), "a.webm"); open(tmp, "wb").write(b"x")
rec = voices.add(tmp, "小然", cfg)
check(rec["name"] == "小然" and rec["status"] in ("cloning", "failed"),
      f"先登记成新声音（状态 {rec['status']}；没 Key 时会很快变 failed）")
import time; time.sleep(0.5)
v = voices.list_voices()["voices"][-1]
check(v["status"] == "failed" and "API Key" in v.get("error", ""), f"失败原因写清楚：{v.get('error')}")
check(voices.current_voice(cfg) == ("yuanqishaonv", None), "没复刻好的声音不会被拿去说话")

print("③ 模拟复刻成功 → 能选中、改名、退回原声")
with voices._lock:
    d = voices.load(); d["voices"][-1].update(status="ready", voice_id="voice-abc"); voices._save(d)
r = voices.select(v["id"])
check(r["ok"] and r["current"] == v["id"], "切到这个声音")
check(voices.current_voice(cfg) == ("voice-abc", "stepaudio-2.5-tts"), "TTS 用复刻音色，模型换成能复刻的 2.5")
r = voices.rename(v["id"], "小然然")
check(r["ok"] and next(x for x in r["voices"] if x["id"] == v["id"])["name"] == "小然然", "改名")
check(not voices.rename("buddy", "x")["ok"], "原声不能改名")
voices.select("buddy")
check(voices.current_voice(cfg) == ("yuanqishaonv", None), "切回原声")

print("④ 删掉正在用的 → 自动退回原声；删除会删掉录音文件")
sample = os.path.join(voices.VOICE_DIR, "keep.txt"); os.makedirs(voices.VOICE_DIR, exist_ok=True)
with voices._lock:
    d = voices.load(); x = next(y for y in d["voices"] if y["id"] == v["id"]); x["sample"] = sample; voices._save(d)
open(sample, "w").write("x")
voices.select(v["id"])
r = voices.delete(v["id"])
check(r["ok"] and r["current"] == "buddy" and len(r["voices"]) == 1, "删掉后只剩原声，当前退回原声")
check(not os.path.exists(sample), "录音文件一并删掉")
check(not voices.delete("buddy")["ok"], "原声不能删")

print("⑤ 登记文件坏了也能用（不会把说话搞挂）")
open(voices.STORE, "w").write("{坏的")
voices._cache["mtime"] = None
check(voices.list_voices()["current"] == "buddy" and voices.current_voice(cfg)[0] == "yuanqishaonv", "读坏了退回原声")

print()
print("全部通过 ✅" if not fails else f"{fails} 项失败 ❌")
sys.exit(1 if fails else 0)
