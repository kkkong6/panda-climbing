"""
攀搭 · 特别时刻（按键之后小搭主动说一句）

原则（02_功能清单「主动反馈的触发规则」）：
  - 跟着按键走：只在按键之后说，绝不在爬墙时说
  - 普通按键只有震动 + 提示音 + 屏幕，不说话；满足下面条件才开口
  - 几分钟内最多说一次（冷却）；苦战完攀是最值得的一刻，不受冷却限制

规则（按优先级）：
  1. 苦战完攀  这条线本次第 N 次（N ≥ hard_send_min）才完攀       → 连震 3 下 + 庆祝
               或者以前几次训练都没拿下、今天拿下了               → 连震 3 下 + 庆祝
  2. 连续完攀  连续 flash_streak_min 条线都是一次完攀               → 连震 3 下 + 夸奖
  3. 多次失败  这条线连续失败到 fail_streak_at（5、10、15…）次       → 长震 1 下 + 安慰
  （下落高度创新高 等 ⑪ 运动数据做完再加）

等 3 秒改按窗口结束再判断，避免按错又改的那次被当成真的。
"""
import time
import random
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout

from config.logger import setup_logging
from panda import store

TAG = __name__
logger = setup_logging()

DEFAULTS = {
    "enable": True,
    "delay": 3.5,            # 按键后等多久再判断（秒）：3 秒改按窗口 + 余量
    "cooldown": 180,         # 两次开口最少间隔（秒）
    "hard_send_min": 3,      # 第几次完攀算「苦战」
    "flash_streak_min": 3,   # 连续几条一次完攀
    "fail_streak_at": 5,     # 连续失败到 5、10、15… 次时安慰
    "llm_timeout": 3,        # 大模型生成台词最多等几秒，超时用模板（按完键 3.5+3 秒内一定开口）
}

_last_spoken = {}            # device_id → 上次开口时间
_lock = threading.Lock()
_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="panda_moment")


def settings(config):
    s = dict(DEFAULTS)
    s.update((config or {}).get("panda_moments") or {})
    return s


def judge(ctx, cfg):
    """看这次按键算不算特别时刻。返回 {kind, vibrate, cue, facts, bypass_cooldown} 或 None"""
    if not ctx or not ctx.get("is_latest"):
        return None  # 后面又按了，交给新的那次判断
    route = ctx["route_label"] or "这条线"
    if ctx["result"] == "send":
        first_ever = ctx["prev_attempts"] > 0 and ctx["prev_sends"] == 0 and ctx["sends_today"] == 1
        if ctx["count"] >= cfg["hard_send_min"] and ctx["sends_today"] == 1:
            facts = f"{route}今天试到第{ctx['count']}次终于完攀"
            if first_ever:
                facts += f"，而且之前几次训练试了{ctx['prev_attempts']}次都没拿下"
            return {"kind": "苦战完攀", "vibrate": "triple", "cue": "激动地欢呼", "facts": facts,
                    "bypass_cooldown": True}
        if first_ever:
            return {"kind": "苦战完攀", "vibrate": "triple", "cue": "激动地欢呼",
                    "facts": f"{route}之前几次训练试了{ctx['prev_attempts']}次都没拿下，今天拿下了",
                    "bypass_cooldown": True}
        if ctx["flash_streak"] >= cfg["flash_streak_min"]:
            return {"kind": "连续完攀", "vibrate": "triple", "cue": "兴奋地夸奖",
                    "facts": f"连续{ctx['flash_streak']}条线都是第一次就完攀，最新一条是{route}",
                    "bypass_cooldown": False}
        return None
    n, step = ctx["fail_streak"], cfg["fail_streak_at"]
    if step > 0 and n >= step and n % step == 0:
        return {"kind": "多次失败", "vibrate": "long1", "cue": "温柔地安慰",
                "facts": f"{route}已经连续失败{n}次了", "bypass_cooldown": False}
    return None


TEMPLATES = {
    "苦战完攀": ["第{count}次终于拿下！太强了！", "拿下了！第{count}次，坚持就是你！"],
    "连续完攀": ["连着{streak}条一次过，今天状态好棒！", "又是一次完攀，手感火热啊！"],
    "多次失败": ["{count}次了，先歇两分钟，你已经很接近了。", "累了就歇歇，换条线找找手感也行。"],
}


def template_line(m, ctx):
    t = random.choice(TEMPLATES[m["kind"]])
    return f"（{m['cue']}）" + t.format(count=ctx["count"], streak=ctx["flash_streak"])


def generate_line(conn, m, ctx, cfg):
    """让大模型按小搭人设写一句，超时或出错就用模板"""
    prompt = (
        f"[特别时刻：{m['kind']}] {m['facts']}。用户刚按完按键，还没说话。"
        f"请你主动说一句，用语气提示（{m['cue']}）开头，正文不超过20个字，口语，别提按键和系统。"
    )
    messages = [{"role": "system", "content": conn.config.get("prompt", "")},
                {"role": "user", "content": prompt}]

    def run():
        return "".join(c for c in conn.llm.response(conn.session_id, messages) if isinstance(c, str))

    try:
        text = _pool.submit(run).result(timeout=cfg["llm_timeout"]).strip()
        body = text.split("）", 1)[-1] if text.startswith("（") else text
        if 2 <= len(body) <= 40:
            return text if text.startswith("（") else f"（{m['cue']}）{text}"
        logger.bind(tag=TAG).warning(f"特别时刻台词不合格，用模板：{text!r}")
    except FutureTimeout:
        logger.bind(tag=TAG).warning(f"特别时刻台词生成超过 {cfg['llm_timeout']}s，用模板")
    except Exception as e:
        logger.bind(tag=TAG).warning(f"特别时刻台词生成失败，用模板：{e}")
    return template_line(m, ctx)


def check(conn, device_id, attempt_id):
    """在后台线程里调用：判断 + 冷却 + 生成台词。返回 (moment, line) 或 None"""
    cfg = settings(conn.config)
    if not cfg["enable"]:
        return None
    ctx = store.attempt_context(device_id, attempt_id)
    m = judge(ctx, cfg)
    if not m:
        return None
    with _lock:
        last = _last_spoken.get(device_id, 0)
        if not m["bypass_cooldown"] and time.time() - last < cfg["cooldown"]:
            logger.bind(tag=TAG).info(f"特别时刻「{m['kind']}」在冷却中（{time.time() - last:.0f}s），不开口")
            return None
        _last_spoken[device_id] = time.time()
    line = generate_line(conn, m, ctx, cfg)
    logger.bind(tag=TAG).info(f"特别时刻「{m['kind']}」| {m['facts']} | {line}")
    return m, line


def reset_cooldown(device_id=None):
    """测试用"""
    with _lock:
        if device_id is None:
            _last_spoken.clear()
        else:
            _last_spoken.pop(device_id, None)
