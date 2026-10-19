"""
攀搭 · 小搭的声音（Step 音色复刻）

02_功能清单「小搭的声音」：可以录一个人 5–10 秒的声音，小搭以后用这个音色说话；可切换、改名、删除。

流程（Step 官方接口）：
  1. POST /v1/files（purpose=storage）上传录音            → file_id
  2. POST /v1/audio/voices（model + file_id + text）复刻   → 音色 ID
  3. TTS 请求里把 voice 换成这个音色 ID

登记存在 data/panda_voices.json（只有一条记录：有哪些声音、当前用哪个；录音和试听音频放在 data/voices/）。
复刻失败不会影响说话：Step TTS 读不到可用音色时自动用原声（元气少女）。
注意：能复刻的模型是 stepaudio-2.5-tts / step-tts-2 / step-tts-mini，所以选中复刻音色时
TTS 模型自动换成 stepaudio-2.5-tts（语气仍按 instruction 走，只是不是 3 代的音色）。
"""
import json
import os
import re
import subprocess
import threading
import time
import uuid

import requests

from config.logger import setup_logging

TAG = __name__
logger = setup_logging()

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # server/
DATA_DIR = os.path.join(BASE, "data")
VOICE_DIR = os.path.join(DATA_DIR, "voices")
STORE = os.path.join(DATA_DIR, "panda_voices.json")

DEFAULT_NAME = "小搭原声"
DEFAULT_ID = "buddy"
MIN_SECONDS, MAX_SECONDS = 5, 10
CLONE_MODEL = "stepaudio-2.5-tts"
PREVIEW_TEXT = "嗨，我是小搭。今天想爬哪条线？"

_lock = threading.Lock()
_cache = {"mtime": None, "data": None}


# ---------- 登记 ----------
def _empty():
    return {"current": DEFAULT_ID, "voices": [{"id": DEFAULT_ID, "name": DEFAULT_NAME, "sys": True}]}


def load():
    """读登记（带缓存，文件变了才重读）"""
    try:
        mtime = os.path.getmtime(STORE)
    except OSError:
        return _empty()
    if _cache["mtime"] == mtime:
        return _cache["data"]
    try:
        with open(STORE, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return _empty()
    if not isinstance(data, dict) or not isinstance(data.get("voices"), list):
        return _empty()
    _cache["mtime"], _cache["data"] = mtime, data
    return data


def _save(data):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = STORE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STORE)
    _cache["mtime"], _cache["data"] = os.path.getmtime(STORE), data


def use_dir(data_dir, keep_store=False):
    """测试用：把登记和录音放到别的目录。
    keep_store=True 只换录音目录（复刻任务在别的线程里读登记文件，别换文件）"""
    global DATA_DIR, VOICE_DIR, STORE
    DATA_DIR = data_dir
    VOICE_DIR = os.path.join(data_dir, "voices")
    if not keep_store:
        STORE = os.path.join(data_dir, "panda_voices.json")
    _cache["mtime"] = None


def list_voices():
    """给 App 用：有哪些声音、当前哪个（不暴露内部路径）"""
    d = load()
    return {"current": d.get("current", DEFAULT_ID),
            "voices": [{k: v.get(k) for k in ("id", "name", "sys", "status", "date", "error") if v.get(k) is not None}
                       for v in d["voices"]]}


def current_voice(config=None):
    """TTS 用：当前音色（没有复刻音色 / 复刻失败 → (原声, None)）"""
    d = load()
    cur = next((v for v in d["voices"] if v["id"] == d.get("current")), None)
    if not cur or cur.get("sys") or cur.get("status") != "ready" or not cur.get("voice_id"):
        base = (config or {}).get("voice") or "yuanqishaonv"
        return base, None
    return cur["voice_id"], CLONE_MODEL


def select(voice_id):
    with _lock:
        d = load()
        if not any(v["id"] == voice_id for v in d["voices"]):
            return {"ok": False, "reason": "没有这个声音"}
        d["current"] = voice_id
        _save(d)
    return {"ok": True, **list_voices()}


def rename(voice_id, name):
    name = (name or "").strip()[:8]
    if not name:
        return {"ok": False, "reason": "名字不能为空"}
    with _lock:
        d = load()
        v = next((v for v in d["voices"] if v["id"] == voice_id), None)
        if not v or v.get("sys"):
            return {"ok": False, "reason": "这个声音不能改名"}
        v["name"] = name
        _save(d)
    return {"ok": True, **list_voices()}


def delete(voice_id):
    with _lock:
        d = load()
        v = next((v for v in d["voices"] if v["id"] == voice_id), None)
        if not v or v.get("sys"):
            return {"ok": False, "reason": "这个声音不能删除"}
        d["voices"] = [x for x in d["voices"] if x["id"] != voice_id]
        if d.get("current") == voice_id:
            d["current"] = DEFAULT_ID  # 删掉正在用的，退回原声
        _save(d)
        for f in (v.get("sample"), v.get("preview")):
            if f and os.path.exists(f):
                try:
                    os.remove(f)
                except OSError:
                    pass
    return {"ok": True, **list_voices()}


# ---------- 复刻 ----------
def _ffmpeg():
    for p in (os.path.join(os.path.dirname(os.__file__), "..", "..", "bin", "ffmpeg"), "ffmpeg"):
        try:
            subprocess.run([p, "-version"], capture_output=True, timeout=10, check=True)
            return p
        except (OSError, subprocess.SubprocessError):
            continue
    return None


def to_wav(src, dst):
    """手机录的是 webm/ogg/mp4，统一转成 16kHz 单声道 wav（Step 只收 mp3 / wav）"""
    ff = _ffmpeg()
    if not ff:
        raise RuntimeError("没找到 ffmpeg，无法转换录音；在 conda 环境里装 ffmpeg")
    r = subprocess.run([ff, "-y", "-i", src, "-ar", "16000", "-ac", "1", "-f", "wav", dst],
                       capture_output=True, timeout=60)
    if r.returncode != 0 or not os.path.exists(dst):
        raise RuntimeError(f"录音转换失败：{r.stderr.decode(errors='ignore')[-200:]}")
    return dst


def duration_of(path):
    """读音频时长（秒）；读不出来返回 None"""
    ff = _ffmpeg()
    if not ff:
        return None
    try:
        r = subprocess.run([ff, "-i", path, "-hide_banner"], capture_output=True, timeout=30)
        m = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", r.stderr.decode(errors="ignore"))
        return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)) if m else None
    except (OSError, subprocess.SubprocessError):
        return None


def api_key_of(config):
    return ((config or {}).get("TTS") or {}).get("StepTTS", {}).get("api_key") or \
           ((config or {}).get("LLM") or {}).get("StepLLM", {}).get("api_key")


def _upload(api_key, path):
    with open(path, "rb") as f:
        r = requests.post("https://api.stepfun.com/v1/files",
                          headers={"Authorization": f"Bearer {api_key}"},
                          files={"file": (os.path.basename(path), f, "audio/wav")},
                          data={"purpose": "storage"}, timeout=60)
    if r.status_code != 200:
        raise RuntimeError(f"上传录音失败：{r.status_code} {r.text[:200]}")
    return r.json()["id"]


def _clone(api_key, file_id, text=None):
    body = {"model": CLONE_MODEL, "file_id": file_id}
    if text:
        body["text"] = text
    r = requests.post("https://api.stepfun.com/v1/audio/voices", json=body,
                      headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}, timeout=120)
    if r.status_code != 200:
        raise RuntimeError(f"复刻失败：{r.status_code} {r.text[:200]}")
    return r.json()["id"]


def _preview(api_key, voice_id, path):
    """复刻完马上合成一句试听，App 上能直接点开听"""
    r = requests.post("https://api.stepfun.com/v1/audio/speech",
                      json={"model": CLONE_MODEL, "input": PREVIEW_TEXT, "voice": voice_id,
                            "response_format": "mp3", "speed": 1.0},
                      headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}, timeout=90)
    if r.status_code == 200:
        with open(path, "wb") as f:
            f.write(r.content)
        return True
    logger.bind(tag=TAG).warning(f"试听合成失败：{r.status_code} {r.text[:200]}")
    return False


def add(path, name, config, text=None):
    """从一段录音新建一个声音。返回登记里的那条记录（status=cloning）。
    后台线程做：转 wav → 上传 → 复刻 → 试听；失败就把 status 改成 failed 并写原因"""
    name = (name or "").strip()[:8] or "新的声音"
    os.makedirs(VOICE_DIR, exist_ok=True)
    rid = uuid.uuid4().hex[:8]
    with _lock:
        d = load()
        rec = {"id": rid, "name": name, "status": "cloning", "date": time.strftime("%m/%d"),
               "sample": os.path.join(VOICE_DIR, f"{rid}.wav"), "src": path}
        d["voices"].append(rec)
        _save(d)
    threading.Thread(target=_clone_job, args=(rid, config, text), daemon=True).start()
    return next(v for v in load()["voices"] if v["id"] == rid)


def _update(rid, **kw):
    with _lock:
        d = load()
        v = next((v for v in d["voices"] if v["id"] == rid), None)
        if not v:
            return False
        v.update(kw)
        _save(d)
        return True


def _clone_job(rid, config, text):
    d = load()
    rec = next((v for v in d["voices"] if v["id"] == rid), None)
    if not rec:
        return
    api_key = api_key_of(config)
    try:
        if not api_key:
            raise RuntimeError("没配置 Step API Key")
        to_wav(rec["src"], rec["sample"])
        secs = duration_of(rec["sample"])
        if secs and not (MIN_SECONDS - 1 <= secs <= MAX_SECONDS + 2):
            raise RuntimeError(f"录音 {secs:.0f} 秒，要求 {MIN_SECONDS}–{MAX_SECONDS} 秒")
        file_id = _upload(api_key, rec["sample"])
        voice_id = _clone(api_key, file_id, text)
        preview = os.path.join(VOICE_DIR, f"{rid}_preview.mp3")
        _preview(api_key, voice_id, preview)
        _update(rid, status="ready", voice_id=voice_id, preview=preview if os.path.exists(preview) else None,
                cloned_at=time.strftime("%Y-%m-%d %H:%M"))
        logger.bind(tag=TAG).info(f"音色复刻完成 | {rec['name']} | 音色 ID {voice_id}")
    except Exception as e:
        logger.bind(tag=TAG).warning(f"音色复刻失败（{rec['name']}）：{e}")
        _update(rid, status="failed", error=str(e)[:120])
    finally:
        src = rec.get("src")
        if src and os.path.exists(src):
            try:
                os.remove(src)
            except OSError:
                pass


def audio_path(voice_id):
    """试听文件路径：复刻音色用自己的试听；原声没有试听文件（返回 None，App 用系统朗读或提示）。"""
    d = load()
    v = next((v for v in d["voices"] if v["id"] == voice_id), None)
    if not v or not v.get("preview") or not os.path.exists(v["preview"]):
        return None
    return v["preview"]


def preview_now(config, voice_id):
    """试听：复刻音色直接用存好的试听；原声现场合成一句（失败返回 None）"""
    p = audio_path(voice_id)
    if p:
        return p
    d = load()
    v = next((x for x in d["voices"] if x["id"] == voice_id), None)
    if not v:
        return None
    api_key = api_key_of(config)
    if not api_key:
        return None
    os.makedirs(VOICE_DIR, exist_ok=True)
    out = os.path.join(VOICE_DIR, "default_preview.mp3")
    voice = (config or {}).get("TTS", {}).get("StepTTS", {}).get("voice") or "yuanqishaonv"
    try:
        r = requests.post("https://api.stepfun.com/v1/audio/speech",
                          json={"model": "stepaudio-3-tts", "input": PREVIEW_TEXT, "voice": voice,
                                "response_format": "mp3", "speed": 1.0},
                          headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                          timeout=60)
        if r.status_code == 200:
            with open(out, "wb") as f:
                f.write(r.content)
            return out
    except Exception as e:
        logger.bind(tag=TAG).warning(f"原声试听合成失败：{e}")
    return None
