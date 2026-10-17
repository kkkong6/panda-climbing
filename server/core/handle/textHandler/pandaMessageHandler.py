"""
攀搭 · 按键记录（设备 → 服务端）

设备发：{"type":"panda","event":"attempt","result":"fail"|"send","id":"<开机编号>-<序号>"}
      3 秒内改按：同一个 id 再发一次，带 "fix":true，结果改成新的（不新增次数）
服务端回：{"type":"panda","event":"attempt_ack","id":...,"ok":true,"result":...,
          "route":"黄色V3"|null,"count":4,"sends":1,"text":"黄色V3 第4次"}
id 用来去重：网络重发同一个按键只记一次。
按键记录不经过大模型，直接写数据库，所以断开语音也能很快回复。
"""
import json
import time
from typing import Dict, Any

from core.handle.textMessageHandler import TextMessageHandler
from core.handle.textMessageType import TextMessageType
from panda import store

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
    if not route:
        text += "·是哪条线？"
    return text


class PandaMessageHandler(TextMessageHandler):
    @property
    def message_type(self) -> TextMessageType:
        return TextMessageType.PANDA

    async def handle(self, conn, msg_json: Dict[str, Any]) -> None:
        event = msg_json.get("event")
        if event != "attempt":
            conn.logger.bind(tag=TAG).warning(f"攀搭：未知事件 {event}")
            return
        conn.last_activity_time = time.time() * 1000  # 按键也算活动，避免连接被当成闲置关掉
        device_id = getattr(conn, "device_id", None) or conn.headers.get("device-id") or "unknown"
        fix = bool(msg_json.get("fix"))
        try:
            r = store.record_attempt(device_id, msg_json.get("result"), msg_json.get("id"), fix=fix)
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
                                       f"{' (重复，已忽略)' if r.get('duplicate') else ''}")
        await conn.websocket.send(json.dumps(reply, ensure_ascii=False))
