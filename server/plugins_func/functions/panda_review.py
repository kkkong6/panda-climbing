"""
攀搭 · 当日复盘（大模型工具）

用户说「今天就到这」「帮我复盘」「结束训练」时，大模型调用 end_session_review：
  1. 规则算出今天的统计（数字准确），结束这次训练
  2. 单独请大模型按小搭人设写约 30 秒的语音复盘（只给人设 + 数据，不带对话历史，避免把没登记上的线编进去），
     检查合格才播；不合格重写一次，还不行用统计数字拼模板
  3. 后台再让大模型写一份完整版（结论 / 亮点 / 提醒 / 下次练），存进数据库给 App 看
"""
import re
import json
import time
import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeout

from config.logger import setup_logging
from plugins_func.register import register_function, ToolType, ActionResponse, Action
from panda import store

TAG = __name__
logger = setup_logging()

end_session_review_desc = {
    "type": "function",
    "function": {
        "name": "end_session_review",
        "description": (
            "结束今天的攀岩训练并做当日复盘。用户说“今天就到这”“今天先这样”“练完了”“结束训练”“帮我复盘”"
            "“今天练得怎么样”“总结一下今天”时调用。只是累了想歇一会儿、或者问某一条线的情况时不要调用。"
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}


def _device_id(conn):
    return getattr(conn, "device_id", None) or (getattr(conn, "headers", {}) or {}).get("device-id") or "unknown"


def facts_text(st):
    """把统计写成大模型读得懂的事实清单（不让大模型自己算数字）"""
    lines = [f"今天练了约{st['minutes']}分钟，爬了{st['route_count']}条线，共尝试{st['attempts']}次，"
             f"完攀{st['sends']}次，完攀率{st['send_rate']}%，完攀了{st['routes_sent']}条线。"]
    for r in st["routes"]:
        if r["sends"]:
            how = "一次完攀" if r["first_send_at"] == 1 else f"第{r['first_send_at']}次完攀"
            if r.get("first_ever_send"):
                how += f"（之前几次训练试了{r['prev_attempts']}次都没拿下，今天第一次拿下）"
        else:
            how = "没完攀"
        lines.append(f"- {r['label']}{('（' + r['note'] + '）') if r['note'] else ''}：试了{r['attempts']}次，{how}")
    if st["hardest_send"]:
        lines.append(f"今天完攀的最难的线：{st['hardest_send']}。")
    if st["hardest_tried"] and st["hardest_tried"] != st["hardest_send"]:
        lines.append(f"尝试过的最难的线：{st['hardest_tried']}。")
    u = st.get("usual")
    if u:
        lines.append(f"平时（之前{u['sessions']}次训练平均）：每次尝试{u['attempts']}次，完攀率{u['send_rate']}%。")
    else:
        lines.append("这是第一次有记录的训练，没有平时可以比较。")
    return "\n".join(lines)


SPOKEN_RULE = (
    "这是当日复盘，这一次不受“一两句话”的限制：用小搭的口吻说一段约 100 到 140 个字的语音复盘（大约 30 秒），"
    "先一句话说今天的结论，再说一两个亮点（具体到哪条线），最后给一条下次练的建议。"
    "只用下面给的数字，不要自己编数字；和平时比较只说一次；口语化，不要列表、不要分点符号。"
    "只能提到下面列出的线和它们的结果：对话里说过、但下面没有列出的线，说明没有记录下来，一律不要提，"
    "更不能说它完攀了。"
)


COLORS = "黄粉红蓝紫绿白黑"
_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="panda_review")


def check_spoken(text, st):
    """语音复盘合格吗：不空、长度合适、只提有记录的线、没完攀的线没被说成完攀。返回 (合格, 原因)"""
    body = re.sub(r"[（(][^）)]*[）)]", "", text or "").strip()
    if len(body) < 50:
        return False, f"太短（{len(body)}字）"
    if len(body) > 220:
        return False, f"太长（{len(body)}字）"
    recorded = {r["label"][0]: r for r in st["routes"] if r["label"] != "未知线路"}
    said = {ch for ch in COLORS if re.search(ch + r"(色|V\d)", body)}
    if not said <= set(recorded):
        return False, f"提到了没有记录的线：{sorted(said - set(recorded))}"
    for ch, r in recorded.items():
        if r["sends"] == 0:
            for m in re.finditer(ch + r"[色]?V\d[^，。！？]*", body):
                # 「还没拿下」「没完攀」「差一点过」是否定，不算
                if re.search(r"(?<![没未差])(拿下|完攀了|搞定|过了|登顶)", m.group(0)) and \
                        not re.search(r"(没|未|还差|差一点|不)[^，。！？]{0,3}(拿下|完攀|搞定|过|登顶)", m.group(0)):
                    return False, f"把没完攀的线说成完攀：{m.group(0)}"
    return True, ""


def template_spoken(st):
    """大模型两次都不合格时的兜底：只用统计数字"""
    parts = [f"（温暖地总结）今天爬了{st['route_count']}条线，尝试{st['attempts']}次，完攀{st['sends']}次，完攀率{st['send_rate']}%。"]
    flash = [r["label"] for r in st["routes"] if r["first_send_at"] == 1]
    hard = [r for r in st["routes"] if r["sends"] and r["first_send_at"] and r["first_send_at"] >= 3]
    if hard:
        parts.append(f"{hard[0]['label']}试到第{hard[0]['first_send_at']}次拿下，很能坚持！")
    elif flash:
        parts.append(f"{flash[0]}一次就过，状态在线！")
    left = [r["label"] for r in st["routes"] if not r["sends"] and r["label"] != "未知线路"]
    if left:
        parts.append(f"{left[0]}还没拿下，下次歇足了再来冲。")
    u = st.get("usual")
    if u and st["send_rate"] != u["send_rate"]:
        parts.append(f"平时完攀率{u['send_rate']}%，今天{'更好' if st['send_rate'] > u['send_rate'] else '稍低一点，可能有点累'}。")
    parts.append("今天辛苦啦，好好休息！")
    return "".join(parts)


def spoken_review(conn, st, budget=10):
    """生成语音复盘：同时发两个请求，用先合格的那个（大模型偶尔会想很久不出字，两个都卡住的概率很小）；
    budget 秒内都没有合格的就用模板"""
    msgs = [{"role": "system", "content": conn.config.get("prompt", "")},
            {"role": "user", "content": "[系统：用户说今天就到这，请做当日复盘]\n" + SPOKEN_RULE + "\n今天的数据：\n" + facts_text(st)}]

    def one():
        text = "".join(c for c in conn.llm.response(conn.session_id, msgs, max_tokens=1500) if isinstance(c, str))
        return re.sub(r"\s+", "", text)

    t0 = time.time()
    futures = [_POOL.submit(one) for _ in range(2)]
    try:
        for f in as_completed(futures, timeout=budget):
            try:
                text = f.result()
            except Exception as e:
                text, why = "", f"出错：{e}"
            else:
                ok, why = check_spoken(text, st)
                if ok:
                    return text if text.startswith("（") else "（温暖地总结）" + text
            logger.bind(tag=TAG).warning(f"语音复盘有一份不合格（{why}），{time.time() - t0:.1f}s")
    except FuturesTimeout:
        logger.bind(tag=TAG).warning(f"语音复盘 {budget}s 内没有合格的")
    return template_spoken(st)


def _full_review(conn, st):
    """后台生成完整版，给 App 的「今日复盘」页用；大模型偶尔空回答，最多试 3 次"""
    prompt = (
        "根据下面的攀岩训练数据写一份当日复盘，只输出 JSON："
        '{"结论":"一句话","亮点":["…"],"提醒":["…"],"下次练":["…"]}，'
        "每条不超过 30 字，只用给出的数字，只提给出的线。\n" + facts_text(st)
    )
    last = None
    for attempt in range(1, 4):
        try:
            text = "".join(c for c in conn.llm.response(conn.session_id, [{"role": "user", "content": prompt}],
                                                         max_tokens=3000, reasoning_effort=None) if isinstance(c, str))
            start, end = text.find("{"), text.rfind("}")
            full = json.loads(text[start:end + 1])
            if not {"结论", "亮点", "提醒", "下次练"} <= set(full):
                raise ValueError(f"缺少部分：{list(full)}")
            store.save_review(st["session_id"], st, full=json.dumps(full, ensure_ascii=False))
            logger.bind(tag=TAG).info(f"完整复盘已保存（训练 {st['session_id']}，第 {attempt} 次）")
            return
        except Exception as e:
            last = e
            logger.bind(tag=TAG).warning(f"完整复盘第 {attempt} 次没生成好：{e}")
    logger.bind(tag=TAG).warning(f"完整复盘生成失败（App 里会只显示数字）：{last}")


def _vibrate(conn, pattern):
    ws, loop = getattr(conn, "websocket", None), getattr(conn, "loop", None)
    if ws and loop:
        try:
            asyncio.run_coroutine_threadsafe(
                ws.send(json.dumps({"type": "panda", "event": "vibrate", "pattern": pattern})), loop)
        except Exception:
            pass


def _say_now(conn, text):
    """马上先说一句垫话，复盘生成期间不冷场（和后面的复盘在同一轮回答里，按顺序播）"""
    try:
        from core.providers.tts.dto.dto import ContentType
        conn.tts.tts_one_sentence(conn, ContentType.TEXT, content_detail=text)
    except Exception as e:
        logger.bind(tag=TAG).warning(f"垫话没播出来：{e}")


def _review_sync(conn, device_id):
    """耗时部分（统计 + 生成，约 5–10 秒），在线程里跑，不卡服务端主循环"""
    st = store.session_summary(device_id, end=True)
    store.save_review(st["session_id"], st)
    threading.Thread(target=_full_review, args=(conn, st), daemon=True).start()
    text = spoken_review(conn, st)
    store.save_review(st["session_id"], st, spoken=text)
    logger.bind(tag=TAG).info(f"语音复盘：{text}")
    return text


@register_function("end_session_review", end_session_review_desc, ToolType.SYSTEM_CTL)
async def end_session_review(conn):
    device_id = _device_id(conn)
    try:
        st = store.session_summary(device_id)
    except Exception as e:
        logger.bind(tag=TAG).error(f"复盘统计失败: {e}")
        return ActionResponse(Action.RESPONSE, None, "（抱歉地）复盘没算出来，回家在 App 里看吧。")
    if not st or st["attempts"] == 0:
        return ActionResponse(Action.RESPONSE, None, "（轻松地）今天还没记录尝试呢，爬完一条按一下键，我就帮你记着。")

    logger.bind(tag=TAG).info(f"当日复盘 | 设备 {device_id} | 训练 {st['session_id']} | "
                              f"{st['route_count']}条线 {st['attempts']}次 完攀率{st['send_rate']}%")
    _vibrate(conn, "long1")  # 长震 1 下：我要说话，请注意听
    if not getattr(conn, "llm", None):
        st = store.session_summary(device_id, end=True)
        text = template_spoken(st)
        store.save_review(st["session_id"], st, spoken=text)
        return ActionResponse(Action.RESPONSE, None, text)
    if getattr(conn, "tts", None):
        _say_now(conn, "（轻快地）好嘞，我来盘一盘今天。")
    try:
        text = await asyncio.get_running_loop().run_in_executor(_POOL, _review_sync, conn, device_id)
    except Exception as e:
        logger.bind(tag=TAG).error(f"复盘生成失败: {e}")
        text = template_spoken(st)
    return ActionResponse(Action.RESPONSE, None, text)
