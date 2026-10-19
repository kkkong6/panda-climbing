"""
攀搭 · 线路笔记（大模型工具）

用户说「记一下，第三步要先换脚」「这条起步右手先抓那个大点」「笔记：最后一步要跳」时，大模型调用 save_route_note：
  1. 原话存进 route_notes（默认记到当前线路；说了颜色难度就记到那条）
  2. 马上用固定模板确认（不再请求大模型，开口快）
  3. 后台让大模型把这条线的全部笔记整理成一句攻略，存进 routes.guide
下次登记同一条线时，确认语里会提醒「你上次记了：……」（panda_route.build_reply）；
训练档案里也有这条线的笔记，问「这条线怎么爬来着」时小搭能答。
"""
import re
import threading

from config.logger import setup_logging
from plugins_func.register import register_function, ToolType, ActionResponse, Action
from panda import store

TAG = __name__
logger = setup_logging()

save_route_note_desc = {
    "type": "function",
    "function": {
        "name": "save_route_note",
        "description": (
            "把攀岩者说的爬法心得记成线路笔记。用户说“记一下……”“帮我记住……”“笔记……”，"
            "或者明确总结了一个下次能用的爬法技巧（如“第三步要先换脚”“起步右手先抓大点”“最后一步得跳”“这条要贴墙走”）时调用。"
            "不要调用的情况：只是抱怨或描述失败（“我总在第三步掉”“好难”）、在提问（“第三步怎么过”）、在登记线路。"
            "content 写心得本身，保留用户的意思，去掉“记一下”这类口头语。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "心得内容，如“第三步要先换脚”"},
                "color": {"type": "string", "description": "用户明确说了是哪条线时才填颜色，否则不填（默认记到现在这条）"},
                "grade": {"type": "string", "description": "用户明确说了是哪条线时才填难度，如 V3"},
            },
            "required": ["content"],
        },
    },
}

CUE = "（轻快地确认）"
FILLER = re.compile(r"^(帮我|给我)?(记一下|记下来|记住|记个笔记|笔记|备注)[，,：:\s]*")
# 语音识别常见错字（同 panda_route）
FIXES = {"羊角": "仰角", "阳角": "仰角", "换角": "换脚", "挂角": "挂脚", "勾角": "勾脚"}


def _device_id(conn):
    return getattr(conn, "device_id", None) or (getattr(conn, "headers", {}) or {}).get("device-id") or "unknown"


def clean(text):
    text = FILLER.sub("", (text or "").strip())
    for wrong, right in FIXES.items():
        text = text.replace(wrong, right)
    return text.strip("。，,. ")


def short(text, n=18):
    return text if len(text) <= n else text[:n] + "…"


def make_guide_prompt(label, notes):
    return (
        f"下面是攀岩者在{label}这条抱石线上陆续记下的爬法心得（按时间顺序，后面的更新）：\n"
        + "\n".join(f"{i + 1}. {n}" for i, n in enumerate(notes))
        + "\n请整理成一句给他自己看的攻略，按爬的顺序写，最多 40 个字。前后矛盾时以后记的为准。"
          "只能用上面写到的内容，不要补充新技巧。只输出攻略本身，不要括号、不要引号。"
    )


def organize_guide(llm, route_id, label, notes, session_id="panda_note"):
    """把这条线的全部笔记整理成一句攻略（后台跑，失败就保留原话，不影响记录）"""
    if len(notes) == 1:
        store.set_guide(route_id, notes[0])
        return notes[0]
    if not llm:
        return None
    for _ in range(2):
        try:
            text = "".join(c for c in llm.response(session_id, [{"role": "user", "content": make_guide_prompt(label, notes)}],
                                                    max_tokens=1500) if isinstance(c, str))
            text = re.sub(r"[（(][^）)]*[）)]", "", text).strip().strip("“”\"' ")
            if 4 <= len(text) <= 60:
                store.set_guide(route_id, text)
                logger.bind(tag=TAG).info(f"线路攻略已整理 | {label} | {text}")
                return text
            logger.bind(tag=TAG).warning(f"线路攻略不合格：{text!r}")
        except Exception as e:
            logger.bind(tag=TAG).warning(f"线路攻略整理失败：{e}")
    return None


@register_function("save_route_note", save_route_note_desc, ToolType.SYSTEM_CTL)
def save_route_note(conn, content=None, color=None, grade=None):
    device_id = _device_id(conn)
    text = clean(content)
    if not text:
        return ActionResponse(Action.RESPONSE, None, f"{CUE}要记什么？比如“第三步要先换脚”。")
    try:
        r = store.add_note(device_id, text, color=color, grade=grade)
    except Exception as e:
        logger.bind(tag=TAG).error(f"线路笔记保存失败: {e}")
        return ActionResponse(Action.RESPONSE, None, f"{CUE}哎呀，没记上，再说一次？")
    logger.bind(tag=TAG).info(f"线路笔记 | 设备 {device_id} | {color or ''}{grade or ''} | {text} | 结果 {r}")
    if not r.get("ok"):
        return ActionResponse(Action.RESPONSE, None, f"{CUE}先告诉我是哪条线，比如黄色V3，我再帮你记。")
    threading.Thread(target=organize_guide, args=(getattr(conn, "llm", None), r["route_id"], r["label"], r["notes"]),
                     daemon=True).start()
    more = f"这条线已经记了{r['count']}条，" if r["count"] > 1 else ""
    return ActionResponse(Action.RESPONSE, None, f"{CUE}记下了，{r['label']}：{short(text)}。{more}下次来我提醒你。")
