"""
攀搭 · 线路登记（大模型工具）

用户说「黄色V3」「粉色微四，仰角那条」「回到刚才那条」「不对，是蓝色V3」时，大模型调用 register_route。
确认语用固定模板直接播报（不再请求一次大模型），所以登记后很快就能听到回应。
"""
import asyncio
import json
import re
from datetime import datetime

from config.logger import setup_logging
from plugins_func.register import register_function, ToolType, ActionResponse, Action
from panda import store

TAG = __name__
logger = setup_logging()

register_route_desc = {
    "type": "function",
    "function": {
        "name": "register_route",
        "description": (
            "登记攀岩者现在要爬的线路（抱石）。只要用户说出了线路的颜色和难度（如“黄色V3”“这条粉的V4”“换到蓝色二级”），"
            "或者说要回到之前那条线（“回到刚才那条”“还是爬上一条”），或者纠正刚才登记错的线路（“不对，是蓝色V3”），就调用。"
            "语音识别常把 V 听成“微”“威”“为”“维”，例如“黄色微三”就是黄色V3。"
            "不要调用的情况：用户在吐槽、提问、描述自己怎么爬某条线（如“这条黄色V3好难”“我总在第三步掉”），"
            "这时他没有要换线，应直接回答他。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "color": {
                    "type": "string",
                    "description": "线路颜色，只能是：黄、粉、红、蓝、紫、绿、白、黑",
                },
                "grade": {
                    "type": "string",
                    "description": "难度，V0 到 V6，例如 V3",
                },
                "note": {
                    "type": "string",
                    "description": "线路本身的特征，用户提到才填，如“仰角”“热身”“左边那条”“屋檐下”；"
                                   "不要填用户爬的情况（如“第三步掉”）；“上周没爬完的那条”这类指代也不用填",
                },
                "back_to_previous": {
                    "type": "boolean",
                    "description": "用户要回到今天之前爬过的上一条线时为 true，此时颜色难度可以不填",
                },
                "correction": {
                    "type": "boolean",
                    "description": "用户在纠正刚才登记错的线路时为 true（如“不对”“说错了”“是蓝色不是黄色”）",
                },
                "new_route": {
                    "type": "boolean",
                    "description": "只有用户明确说岩馆把这条线重新定线了、这是刚换上去的新线时才为 true。"
                                   "“换一条”“换到蓝色V2”只是换着爬，不算，不要填",
                },
            },
            "required": [],
        },
    },
}

CUE = "（轻快地确认）"
NOT_A_NOTE = re.compile(r"新(换|定|上)?的?线|刚(换|定|上)|重新定线|上周|上次|刚才|之前")
# 语音识别常见的同音错字（实测 stepaudio-3-asr-max 偶尔把“仰角”听成“羊角”）
NOTE_FIXES = {"羊角": "仰角", "阳角": "仰角", "养角": "仰角", "乌眼": "屋檐", "无言": "屋檐"}


def _device_id(conn):
    return getattr(conn, "device_id", None) or (getattr(conn, "headers", {}) or {}).get("device-id") or "unknown"


def _push_route(conn, route_label):
    """告诉设备当前线路，屏幕马上换成这条线的色块（不等语音播完）
    发：{"type":"panda","event":"route","route":"黄色V3"}"""
    ws, loop = getattr(conn, "websocket", None), getattr(conn, "loop", None)
    if not ws or not loop:
        return
    msg = json.dumps({"type": "panda", "event": "route", "route": route_label}, ensure_ascii=False)
    try:
        asyncio.run_coroutine_threadsafe(ws.send(msg), loop)  # 不等结果，发不出去也不影响登记
    except Exception as e:
        logger.bind(tag=TAG).warning(f"线路推送到设备失败: {e}")


def _date_cn(d):
    try:
        dt = datetime.strptime(d, "%Y-%m-%d")
        return f"{dt.month}月{dt.day}日"
    except (TypeError, ValueError):
        return ""


def build_reply(r, correction=False):
    """把登记结果写成一两句确认语（屏幕小，尽量短）"""
    if not r.get("ok"):
        if r.get("reason"):
            return f"{CUE}{r['reason']}，说颜色加难度就行，比如黄色V3。"
        missing = r.get("missing", [])
        if missing == ["颜色"]:
            return f"{CUE}什么颜色的？比如黄色V3。"
        if missing == ["难度"]:
            return f"{CUE}几级的？比如V3。"
        return f"{CUE}是哪条线？说颜色加难度，比如黄色V3。"

    head = "训练开始！" if r["session_started"] else ""
    name = r["label"] + (f"{r['note']}" if r["note"] and len(r["note"]) <= 4 else "")
    if correction:
        body = f"改好了，是{name}。"
    elif r.get("already_current"):
        body = f"还是{name}，继续冲！"
    else:
        body = f"{name}，登记好啦。"

    tail = ""
    if not correction and not r.get("already_current"):
        if r["new_route"]:
            tail = "新线路，冲！"
        elif r["sessions_before"] > 0 and r["last_date"]:
            tail = f"上次{_date_cn(r['last_date'])}爬过这条。"
    if r.get("adopted"):
        tail = f"刚才{r['adopted']}次也记到这条了。" + tail
    elif correction and r.get("moved"):
        tail = f"刚才{r['moved']}次也改过来了。"
    return f"{CUE}{head}{body}{tail}"


@register_function("register_route", register_route_desc, ToolType.SYSTEM_CTL)
def register_route(conn, color=None, grade=None, note=None, back_to_previous=False,
                   correction=False, new_route=False):
    device_id = _device_id(conn)
    # “新换的线”“刚定的线”说的是换线这件事，不是线路特征，不存成描述
    if note and NOT_A_NOTE.search(note):
        note = None
    for wrong, right in NOTE_FIXES.items():
        if note and wrong in note:
            note = note.replace(wrong, right)
    try:
        if back_to_previous and not (color and grade):
            r = store.back_to_previous(device_id)
        else:
            r = store.register_route(device_id, color, grade, note=note,
                                     force_new=bool(new_route), correction=bool(correction))
    except Exception as e:
        logger.bind(tag=TAG).error(f"线路登记失败: {e}")
        return ActionResponse(Action.RESPONSE, None, f"{CUE}哎呀，没记上，再说一次线路？")

    logger.bind(tag=TAG).info(f"线路登记 | 设备 {device_id} | 参数 {color} {grade} {note or ''} "
                              f"回上一条={back_to_previous} 更正={correction} 新线={new_route} | 结果 {r}")
    if r.get("ok") and r.get("label"):
        _push_route(conn, r["label"])
    if r.get("ok") and r.get("already_current") and not correction and not r.get("adopted"):
        # 线路没变：多半是用户在聊这条线，交回大模型直接回应他说的话
        return ActionResponse(
            Action.REQLLM,
            f"当前线路没有变化，仍是{r['label']}，不需要再确认登记。请直接回应用户刚才说的话。",
            None,
        )
    return ActionResponse(Action.RESPONSE, None, build_reply(r, correction=bool(correction)))
