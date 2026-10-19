"""
攀搭 · 按键记录（设备 → 服务端）

设备发：{"type":"panda","event":"attempt","result":"fail"|"send","id":"<开机编号>-<序号>",
        "motion":{"fall_m":2.8,"impact_g":5.1,"climb_s":35,"rest_s":130,"swings":1,"smooth":72}}
      motion 是设备当场算好的这次尝试的运动数据（没测到的字段不带；没有 IMU 时整个不带）
      3 秒内改按：同一个 id 再发一次，带 "fix":true，结果改成新的（不新增次数）
服务端回：{"type":"panda","event":"attempt_ack","id":...,"ok":true,"result":...,
          "route":"黄色V3"|null,"count":4,"sends":1,"text":"黄色V3 第4次"}
id 用来去重：网络重发同一个按键只记一次。
按键记录不经过大模型，直接写数据库，所以断开语音也能很快回复。

安全 / 闲置事件（fall / sos / sos_cancel / idle）见 panda/safety.py。
"""
import json
import time
import uuid
import asyncio
from typing import Dict, Any

from core.handle.textMessageHandler import TextMessageHandler
from core.handle.textMessageType import TextMessageType
from panda import store, moments, safety
from core.utils.dialogue import Message
from core.providers.tts.dto.dto import ContentType, TTSMessageDTO, SentenceType

TAG = __name__


def ack_text(r):
    """屏幕上显示的一行确认（屏幕小，约 8 个字一行）"""
    if not r.get("ok"):
        return "没记上，再按一次"
    route = r.get("route_label")
    if r["result"] == "send":
        head = f"{route} 完攀！" if route else "完攀！"
    else:
        head = f"{route}" if route else "未知线路"
    if r.get("fixed"):
        # 改按：说清楚改成了什么
        head = "已改：" + (head if r["result"] == "send" else f"{route or '未知线路'} 失败")
    text = f"{head} 第{r['count']}次"
    h = (r.get("motion") or {}).get("fall_m")
    if r["result"] == "fail" and h is not None and h >= 0.5:
        text += f" {h:.1f}米"
    if not route:
        text += "·是哪条线？"
    return text


def _device_id(conn):
    return getattr(conn, "device_id", None) or conn.headers.get("device-id") or "unknown"


def _num(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


class PandaMessageHandler(TextMessageHandler):
    @property
    def message_type(self) -> TextMessageType:
        return TextMessageType.PANDA

    async def handle(self, conn, msg_json: Dict[str, Any]) -> None:
        event = msg_json.get("event")
        conn.last_activity_time = time.time() * 1000  # 按键也算活动，避免连接被当成闲置关掉
        try:
            from panda.api import seen
            seen(_device_id(conn))  # App 上显示设备在线
        except Exception:
            pass
        if event in ("fall", "sos", "sos_cancel", "idle"):
            await handle_safety(conn, event, msg_json)
            return
        if event != "attempt":
            conn.logger.bind(tag=TAG).warning(f"攀搭：未知事件 {event}")
            return
        device_id = _device_id(conn)
        fix = bool(msg_json.get("fix"))
        try:
            r = store.record_attempt(device_id, msg_json.get("result"), msg_json.get("id"), fix=fix,
                                     motion=msg_json.get("motion"))
        except Exception as e:
            conn.logger.bind(tag=TAG).error(f"攀搭：按键记录失败 {e}")
            r = {"ok": False}
        reply = {
            "type": "panda", "event": "attempt_ack", "id": msg_json.get("id"),
            "ok": bool(r.get("ok")), "result": r.get("result"), "route": r.get("route_label"),
            "count": r.get("count"), "sends": r.get("sends"), "duplicate": r.get("duplicate", False),
            "fix": fix,
            "text": ack_text(r),
        }
        conn.logger.bind(tag=TAG).info(f"攀搭：按键记录 | 设备 {device_id} | {msg_json.get('result')}"
                                       f"{'（改按）' if fix else ''} → {reply['text']}"
                                       f"{' | 运动 ' + json.dumps(r['motion'], ensure_ascii=False) if r.get('motion') else ''}"
                                       f"{' (重复，已忽略)' if r.get('duplicate') else ''}")
        await conn.websocket.send(json.dumps(reply, ensure_ascii=False))

        # 特别时刻：等 3 秒改按窗口过去再判断；又按了就取消上一次的判断
        if r.get("ok") and not r.get("duplicate"):
            old = getattr(conn, "panda_moment_task", None)
            if old and not old.done():
                old.cancel()
            conn.panda_moment_task = asyncio.create_task(
                _moment_after_delay(conn, device_id, r["attempt_id"]))


async def _moment_after_delay(conn, device_id, attempt_id):
    try:
        await asyncio.sleep(moments.settings(conn.config)["delay"])
        loop = asyncio.get_running_loop()
        found = await loop.run_in_executor(None, moments.check, conn, device_id, attempt_id)
        if not found:
            return
        if conn.client_is_speaking:
            conn.logger.bind(tag=TAG).info("攀搭：小搭正在说话，特别时刻这次不插嘴")
            return
        m, line = found
        await speak(conn, line, vibrate=m["vibrate"], context=f"{m['kind']}：{m['facts']}")
    except asyncio.CancelledError:
        pass
    except Exception as e:
        conn.logger.bind(tag=TAG).error(f"攀搭：特别时刻出错 {e}")


async def handle_safety(conn, event, msg):
    """重摔 → 记录 + 关心一句（设备已经本地震动、黄屏）；sos / sos_cancel 只记录；idle → 问问是不是练完了"""
    device_id = _device_id(conn)
    loop = asyncio.get_running_loop()
    try:
        if event == "fall":
            impact, fall_m = _num(msg.get("impact_g")), _num(msg.get("fall_m"))
            ev, line = await loop.run_in_executor(None, safety.on_fall, conn, device_id, impact, fall_m)
            for _ in range(40):  # 小搭正在说话：等它说完（最多 8 秒）再关心
                if not conn.client_is_speaking:
                    break
                await asyncio.sleep(0.2)
            # 设备收到重摔时已经长震 2 下，这里不再震
            await speak(conn, line, context=f"安全提醒：重摔（落地冲击约{impact or 0:.0f}g），设备已显示「还好吗？」")
        elif event in ("sos", "sos_cancel"):
            await loop.run_in_executor(None, store.safety_event, device_id, event)
            conn.logger.bind(tag=TAG).warning(f"攀搭：{'摔后不动，设备在报警' if event == 'sos' else '报警已被用户取消'} | 设备 {device_id}")
            if event == "sos_cancel":
                await speak(conn, "（松了口气地）没事就好，慢慢起来，不舒服就先歇着。")
        elif event == "idle":
            minutes = int(_num(msg.get("minutes")) or 30)
            line = await loop.run_in_executor(None, safety.idle_line, device_id, minutes)
            if line and not conn.client_is_speaking:
                await speak(conn, line, vibrate="long1", context=f"用户{minutes}分钟没有按键记录")
    except Exception as e:
        conn.logger.bind(tag=TAG).error(f"攀搭：安全事件 {event} 处理出错 {e}")


async def speak(conn, line, vibrate=None, context=None):
    """小搭主动说一句：先震动提醒注意听，再播语音（屏幕同步显示字幕）"""
    if vibrate:
        await conn.websocket.send(json.dumps(
            {"type": "panda", "event": "vibrate", "pattern": vibrate}))
        await asyncio.sleep(0.6)  # 震完再开口
    if not conn.tts:
        return
    conn.client_abort = False
    conn.sentence_id = uuid.uuid4().hex
    conn.tts.tts_text_queue.put(TTSMessageDTO(
        sentence_id=conn.sentence_id, sentence_type=SentenceType.FIRST, content_type=ContentType.ACTION))
    conn.tts.tts_one_sentence(conn, ContentType.TEXT, content_detail=line)
    conn.tts.tts_text_queue.put(TTSMessageDTO(
        sentence_id=conn.sentence_id, sentence_type=SentenceType.LAST, content_type=ContentType.ACTION))
    # 记进对话历史，之后用户接话（"谢谢""再说一遍"）小搭知道刚才说了什么。
    # 先补一条说明再记小搭的话：只有一句没头没尾的助手发言，大模型会理不清上下文、想很久
    if context:
        conn.dialogue.put(Message(role="user", content=f"[系统记录：用户刚按了记录键。{context}。你主动对他说了下面这句]"))
    conn.dialogue.put(Message(role="assistant", content=line))
