"""
攀搭 · 安全提醒（事中 / 事后）和闲置提醒

分工（02_功能清单 第四节）：
  - 判断风险用固定规则，在设备上做：重摔（落地冲击超过阈值）、摔后不动（30 秒）→ 断网也能震动 + 报警
  - 提醒怎么说用大模型 + 历史档案：如「这周第二次重摔了，都在仰角线，落地记得屈膝」
  - 摔后报警用固定提示音，不依赖网络和模型；服务端只做记录，不通知他人

设备发：
  {"type":"panda","event":"fall","impact_g":9.3,"fall_m":2.6}   重摔（设备已经长震 2 下、黄屏「还好吗？」）
  {"type":"panda","event":"sos"}                                 摔后 30 秒不动，设备已经在报警
  {"type":"panda","event":"sos_cancel"}                          用户按键取消了报警
  {"type":"panda","event":"idle","minutes":30}                   训练中 30 分钟没按键，问问是不是结束了
"""
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout

from config.logger import setup_logging
from panda import store

TAG = __name__
logger = setup_logging()
_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="panda_safety")

LLM_TIMEOUT = 3


def hard_fall_facts(ev, impact_g, fall_m):
    parts = [f"用户刚才重重地摔了一下（落地冲击约{impact_g:.0f}g" + (f"，从{fall_m:.1f}米掉下来" if fall_m else "") + "）"]
    if ev.get("route_label"):
        parts.append(f"在爬{ev['route_label']}" + (f"（{ev['route_note']}）" if ev.get("route_note") else ""))
    n = ev.get("week_hard_falls", 1)
    if n >= 2:
        where = "、".join(dict.fromkeys(ev.get("week_hard_routes") or []))
        parts.append(f"这已经是最近7天第{n}次重摔了（{where}）")
    return "，".join(parts)


def template_fall(ev):
    n = ev.get("week_hard_falls", 1)
    if n >= 2:
        return f"（关心地询问）这周第{n}次重摔了，还好吗？落地记得屈膝，顺势往后滚。"
    return "（关心地询问）刚才摔得有点重，还好吗？落地记得屈膝，顺势往后滚。"


def fall_line(conn, ev, impact_g, fall_m):
    """重摔后的关心话：大模型按人设和历史写一句，LLM_TIMEOUT 秒没写好就用模板"""
    llm = getattr(conn, "llm", None)
    if not llm:
        return template_fall(ev)
    prompt = (f"[安全提醒：重摔] {hard_fall_facts(ev, impact_g, fall_m)}。"
              "请你主动关心他一句：先问还好吗，再给一个具体的落地建议（比如屈膝、顺势后滚、别用手撑）；"
              "如果是这周第二次以上，点出来，并结合是哪类线。用语气提示（关心地询问）开头，正文不超过35个字。"
              "不要诊断伤情；疼的话建议休息、必要时就医。")
    messages = [{"role": "system", "content": conn.config.get("prompt", "")}, {"role": "user", "content": prompt}]
    try:
        text = _pool.submit(lambda: "".join(
            c for c in llm.response(conn.session_id, messages) if isinstance(c, str))).result(timeout=LLM_TIMEOUT).strip()
        body = text.split("）", 1)[-1] if text.startswith("（") else text
        if 4 <= len(body) <= 60:
            return text if text.startswith("（") else "（关心地询问）" + text
        logger.bind(tag=TAG).warning(f"重摔关心话不合格，用模板：{text!r}")
    except FutureTimeout:
        logger.bind(tag=TAG).warning("重摔关心话生成超时，用模板")
    except Exception as e:
        logger.bind(tag=TAG).warning(f"重摔关心话生成失败，用模板：{e}")
    return template_fall(ev)


def on_fall(conn, device_id, impact_g, fall_m):
    """在线程里调用：记录 + 生成关心话。返回 (事件, 台词)"""
    ev = store.safety_event(device_id, "hard_fall", impact_g=impact_g, fall_m=fall_m)
    line = fall_line(conn, ev, impact_g or 0, fall_m)
    logger.bind(tag=TAG).info(f"重摔 | 设备 {device_id} | {impact_g}g {fall_m}m | 本周第{ev['week_hard_falls']}次 | {line}")
    return ev, line


def idle_line(device_id, minutes):
    """训练中很久没按键：问问是不是练完了。今天没有任何记录就不打扰，返回 None"""
    cond = store.session_condition(device_id)  # 没有进行中的训练返回 None
    if not cond or not cond.get("attempts"):
        return None
    return (f"（轻松地询问）歇了{minutes}分钟啦，今天结束了吗？"
            "说今天就到这，我帮你复盘；还要练就当我没说。")
