"""
攀搭 · Step 语音识别 + 语气识别（并行）

同一段录音同时发两路：
  ① 语音识别（step-asr / stepaudio-2.5-asr 等，OpenAI transcriptions 接口）→ 文字
  ② 语气识别（stepaudio-3-chat-preview）→ {"情绪","强度","依据"}
两路并行，语气识别最多多等 emotion_timeout 秒；超时或失败就只用文字，不影响对话。
返回 {"content": 文字, "emotion": {...}}，由 startToChat 拆开：文字给屏幕，情绪给大模型和 TTS。
"""
import io
import re
import json
import time
import wave
import base64
import asyncio
import requests
from typing import Optional, Tuple, List

from config.logger import setup_logging
from core.providers.asr.dto.dto import InterfaceType
from core.providers.asr.base import ASRProviderBase

TAG = __name__
logger = setup_logging()

EMOTIONS = ("沮丧", "疲惫", "兴奋", "平静", "紧张")
# 攀岩热词：让识别模型优先认这些词（实测“仰角”不再听成“羊角”，“V5”不再听成“威武”）
DEFAULT_HOTWORDS = ["仰角", "V0", "V1", "V2", "V3", "V4", "V5", "V6", "完攀", "抱石", "屋檐", "岩点",
                    "小搭", "热身", "粉色", "紫色", "脚点", "手点", "定线", "换线",
                    "零级", "一级", "二级", "三级", "四级", "五级", "六级"]
EMOTION_PROMPT = (
    "你在听一位攀岩者刚爬完时说的话。只根据声音判断他的情绪，重点听语调、语速、音量、叹气、喘气、笑声等副语言，"
    "不要只看字面意思（比如笑着说“又掉了”是轻松，不是沮丧）。只输出一行 JSON，不要其他内容："
    '{"情绪":"沮丧|疲惫|兴奋|平静|紧张","强度":1到5的整数,"依据":"不超过15字，只写声音特征"}'
)


class ASRProvider(ASRProviderBase):
    def __init__(self, config: dict, delete_audio_file: bool):
        super().__init__()
        self.interface_type = InterfaceType.NON_STREAM
        self.api_key = config.get("api_key")
        self.api_url = config.get("base_url", "https://api.stepfun.com/v1/audio/transcriptions")
        self.model = config.get("model_name", "step-asr")
        self.sse_url = config.get("sse_url", "https://api.stepfun.com/v1/audio/asr/sse")
        self.hotwords = list(config.get("hotwords") or DEFAULT_HOTWORDS)
        self.output_dir = config.get("output_dir", "tmp/")
        self.delete_audio_file = delete_audio_file

        self.emotion_enable = str(config.get("emotion_enable", True)).lower() in ("true", "1", "yes")
        self.emotion_model = config.get("emotion_model", "stepaudio-3-chat-preview")
        self.emotion_url = config.get("emotion_url", "https://api.stepfun.com/v1/chat/completions")
        self.emotion_timeout = float(config.get("emotion_timeout", 3))
        # 太短的录音（如「嗯」）不值得判断语气，单位秒
        self.emotion_min_seconds = float(config.get("emotion_min_seconds", 0.8))

    def requires_file(self) -> bool:
        return True

    # ---------- 两路请求（同步，放到线程里跑）----------
    def _transcribe(self, file_path: str) -> str:
        if self.model.startswith("stepaudio"):
            return self._transcribe_sse(file_path)
        t0 = time.time()
        with open(file_path, "rb") as f:
            resp = requests.post(
                self.api_url,
                files={"file": f},
                data={"model": self.model},
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=15,
            )
        if resp.status_code != 200:
            raise Exception(f"语音识别失败: {resp.status_code} - {resp.text[:200]}")
        text = resp.json().get("text", "")
        logger.bind(tag=TAG).info(f"语音识别 {time.time() - t0:.2f}s | {text}")
        return text

    def _transcribe_sse(self, file_path: str) -> str:
        """stepaudio-3-asr-max / stepaudio-2.5-asr：支持热词，攀岩用语（仰角、V5）识别更准"""
        t0 = time.time()
        with open(file_path, "rb") as f:
            data = base64.b64encode(f.read()).decode()
        body = {"audio": {"data": data, "input": {
            "transcription": {"language": "zh", "model": self.model, "hotwords": self.hotwords},
            "format": {"type": "wav"}}}}
        resp = requests.post(
            self.sse_url, json=body, timeout=15,
            headers={"Authorization": f"Bearer {self.api_key}", "Accept": "text/event-stream"},
        )
        if resp.status_code != 200:
            raise Exception(f"语音识别失败: {resp.status_code} - {resp.text[:200]}")
        text = ""
        for line in resp.content.decode("utf-8", "ignore").splitlines():
            if not line.startswith("data:"):
                continue
            try:
                ev = json.loads(line[5:].strip())
            except json.JSONDecodeError:
                continue
            if ev.get("type") == "transcript.text.done":
                text = ev.get("text") or ""
            elif ev.get("type") == "error":
                raise Exception(f"语音识别失败: {ev.get('message')}")
        logger.bind(tag=TAG).info(f"语音识别 {time.time() - t0:.2f}s | {text}")
        return text

    def _detect_emotion(self, wav_bytes: bytes) -> Optional[dict]:
        t0 = time.time()
        body = {
            "model": self.emotion_model,
            "messages": [
                {"role": "system", "content": EMOTION_PROMPT},
                {"role": "user", "content": [
                    {"type": "text", "text": "判断这段话的情绪"},
                    {"type": "input_audio", "input_audio": {
                        "data": "data:audio/wav;base64," + base64.b64encode(wav_bytes).decode()}},
                ]},
            ],
            "max_tokens": 600,  # 模型会先思考（实测 60–300 token），300 偶尔不够，回答被截断
            "temperature": 0.2,
        }
        resp = requests.post(
            self.emotion_url, json=body,
            headers={"Authorization": f"Bearer {self.api_key}"},
            timeout=self.emotion_timeout + 2,
        )
        if resp.status_code != 200:
            raise Exception(f"{resp.status_code} - {resp.text[:200]}")
        content = (resp.json()["choices"][0]["message"].get("content") or "").strip()
        emo = self._parse_emotion(content)
        logger.bind(tag=TAG).info(f"语气识别 {time.time() - t0:.2f}s | {emo or ('无法解析: ' + content[:80])}")
        return emo

    @staticmethod
    def _parse_emotion(content: str) -> Optional[dict]:
        d = None
        m = re.search(r"\{.*?\}", content, re.S)
        if m:
            try:
                d = json.loads(m.group(0))
            except json.JSONDecodeError:
                d = None
        if d is None:
            # 回复被截断（如 {"情绪":"平静","强度":2,"依据":"…）时，逐个字段捞出来
            emo_m = re.search(r'"情绪"\s*:\s*"([^"|]+)', content)
            if not emo_m:
                return None
            lvl_m = re.search(r'"强度"\s*:\s*(\d)', content)
            why_m = re.search(r'"依据"\s*:\s*"([^"]*)', content)
            d = {"情绪": emo_m.group(1), "强度": lvl_m.group(1) if lvl_m else 3,
                 "依据": why_m.group(1) if why_m else ""}
        emo = str(d.get("情绪", "")).split("|")[0].strip()
        if emo not in EMOTIONS:
            return None
        try:
            level = max(1, min(5, int(d.get("强度", 3))))
        except (TypeError, ValueError):
            level = 3
        return {"情绪": emo, "强度": level, "依据": str(d.get("依据", ""))[:20]}

    @staticmethod
    def _to_wav(pcm: bytes) -> bytes:
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(pcm[: len(pcm) // 2 * 2])
        return buf.getvalue()

    # ---------- 入口 ----------
    async def speech_to_text(self, opus_data: List[bytes], session_id: str, artifacts=None) -> Tuple[Optional[object], Optional[str]]:
        if artifacts is None or not artifacts.file_path:
            return "", None
        loop = asyncio.get_running_loop()
        seconds = len(artifacts.pcm_bytes) / 32000  # 16kHz * 16bit
        asr_future = loop.run_in_executor(None, self._transcribe, artifacts.file_path)
        emo_future = None
        if self.emotion_enable and seconds >= self.emotion_min_seconds:
            emo_future = loop.run_in_executor(None, self._detect_emotion, self._to_wav(artifacts.pcm_bytes))

        try:
            text = await asr_future
        except Exception as e:
            logger.bind(tag=TAG).error(str(e))
            return "", None

        emotion = None
        if emo_future is not None:
            try:
                emotion = await asyncio.wait_for(emo_future, timeout=self.emotion_timeout)
            except asyncio.TimeoutError:
                logger.bind(tag=TAG).warning(f"语气识别超过 {self.emotion_timeout}s，本轮跳过")
            except Exception as e:
                logger.bind(tag=TAG).warning(f"语气识别失败，本轮跳过: {e}")

        if text and emotion:
            return {"content": text, "emotion": emotion}, artifacts.file_path
        return text, artifacts.file_path
