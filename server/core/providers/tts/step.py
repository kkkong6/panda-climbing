"""
攀搭 · Step 情感语音合成（stepaudio-3-tts / stepaudio-2.5-tts）

语气从哪来（优先级从高到低）：
  1. 大模型在回答开头写的语气提示，如「（温柔地安慰）第8次了…」
     —— 同一轮回答的后续句子沿用这个语气；括号不会显示在屏幕上，也不会被念出来
  2. 没写语气提示时，按「语气识别」听出的用户情绪自动选（emotion_instructions）
  3. 都没有时，只用人设基调（instruction）
回答中间如果出现括号（如「（轻笑）」），原样交给 TTS 当句内指令，但屏幕上不显示。
"""
import re
import requests

from config.logger import setup_logging
from core.utils.util import check_model_key
from core.providers.tts.base import TTSProviderBase

TAG = __name__
logger = setup_logging()

# 开头的语气提示：（温柔地安慰） 或半角 (温柔地安慰)
LEADING_CUE = re.compile(r"^\s*[（(]([^（）()]{1,40})[）)]\s*")
# 任意位置的括号内容（用于屏幕显示时去掉）
ANY_CUE = re.compile(r"[（(][^（）()]{0,40}[）)]")

DEFAULT_EMOTION_INSTRUCTIONS = {
    "沮丧": "温柔地安慰，语速放慢，带点心疼，像朋友拍拍肩",
    "疲惫": "温柔体贴，声音放轻，语速放慢",
    "兴奋": "跟着一起兴奋，带笑意，语速偏快，音调上扬",
    "紧张": "沉稳让人安心，语速平稳",
    "平静": "",
}


class TTSProvider(TTSProviderBase):
    TTS_PARAM_CONFIG = [
        ("ttsRate", "speed", 0.5, 2, 1, lambda v: round(float(v), 2)),
    ]

    def __init__(self, config, delete_audio_file):
        super().__init__(config, delete_audio_file)
        self.api_key = config.get("api_key")
        self.api_url = config.get("api_url", "https://api.stepfun.com/v1/audio/speech")
        self.model = config.get("model", "stepaudio-3-tts")
        self.voice = config.get("private_voice") or config.get("voice", "yuanqishaonv")
        self.audio_file_type = "wav"
        self.speed = float(config.get("speed") or 1.0)
        self.volume = float(config.get("volume") or 1.0)
        self.base_instruction = (config.get("instruction") or "").strip()
        self.emotion_instructions = dict(DEFAULT_EMOTION_INSTRUCTIONS)
        self.emotion_instructions.update(config.get("emotion_instructions") or {})
        # stepaudio-2.5-tts 最多 200 字，stepaudio-3-tts 最多 500 字
        self.max_instruction = 200 if "2.5" in self.model else 500
        self._apply_percentage_params(config)

        # 当前这一轮回答的语气（按 sentence_id 区分）
        self._cue = ""
        self._cue_sentence_id = None
        self._pending_synth_text = None

        msg = check_model_key("TTS", self.api_key)
        if msg:
            logger.bind(tag=TAG).error(msg)

    # ---------- 语气处理 ----------
    def _split_cue(self, text):
        """拆出开头语气提示，返回 (给 TTS 的文本, 给屏幕的文本)"""
        sid = getattr(self, "current_sentence_id", None)
        if sid != self._cue_sentence_id:  # 新的一轮回答，清空上一轮语气
            self._cue, self._cue_sentence_id = "", sid

        m = LEADING_CUE.match(text)
        if m:
            self._cue = m.group(1).strip()
            text = text[m.end():]

        synth = text
        display = ANY_CUE.sub("", text)
        # 被切句切断的半截括号：屏幕和 TTS 都去掉，避免把正文当成指令
        for ch in "（）()":
            display = display.replace(ch, "")
        if synth.count("（") + synth.count("(") != synth.count("）") + synth.count(")"):
            synth = display
        return synth.strip(), display.strip()

    def _build_instruction(self):
        tone = self._cue
        if not tone:
            emo = getattr(self.conn, "user_emotion", None) if self.conn else None
            if isinstance(emo, dict):
                tone = self.emotion_instructions.get(str(emo.get("情绪", "")), "")
        parts = [p for p in (self.base_instruction, f"这句话的语气：{tone}" if tone else "") if p]
        return "。".join(parts)[: self.max_instruction]

    def to_tts_stream(self, text, opus_handler=None):
        synth, display = self._split_cue(text)
        if not display:  # 只有语气提示、没有正文
            return None
        self._pending_synth_text = synth
        try:
            return super().to_tts_stream(display, opus_handler)
        finally:
            self._pending_synth_text = None

    def to_tts(self, text):
        synth, display = self._split_cue(text)
        if not display:
            return None
        self._pending_synth_text = synth
        try:
            return super().to_tts(display)
        finally:
            self._pending_synth_text = None

    # ---------- 调用 Step ----------
    async def text_to_speak(self, text, output_file):
        synth_text = self._pending_synth_text or text
        body = {
            "model": self.model,
            "input": synth_text,
            "voice": self.voice,
            "response_format": self.audio_file_type,
            "speed": self.speed,
            "volume": self.volume,
        }
        instruction = self._build_instruction()
        if instruction:
            body["instruction"] = instruction
        logger.bind(tag=TAG).info(f"Step TTS | 文本: {synth_text} | 语气: {instruction or '无'}")

        resp = requests.post(
            self.api_url,
            json=body,
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            timeout=self.tts_timeout,
        )
        if resp.status_code != 200:
            raise Exception(f"Step TTS 请求失败: {resp.status_code} - {resp.text[:300]}")
        if output_file:
            with open(output_file, "wb") as f:
                f.write(resp.content)
        else:
            return resp.content
