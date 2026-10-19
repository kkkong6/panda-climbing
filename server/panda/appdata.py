"""
攀搭 · App 数据（⑩ App 接真实数据）

把数据库整理成 App（app/攀搭App.html）每个页面要的样子，App 只负责画：
  今天（状态、建议、训练中的记录）、复盘、线路（每次多高、笔记、修改记录）、成长（完攀率、金字塔、雷达、身体）、我的。
数字全部由规则算；文字结论优先用当日复盘里大模型写好的完整版（reviews.full），没有就用规则拼。

App 里的约定（和原型的演示数据结构一致）：
  线路 {id, rid, hold, color:'黄', grade:'V3', tag, wall, status:'proj'|'sent', reset,
        history:[{d:'9/29', h:[2.1, 4.2], ok:[false, true]}], today:[{id, t:'19:12', ok, h, imp:'轻'|'中'|'重'}],
        note, noteDate, notes:[{id, text, date}], sentDate, advice}
  h 是这次爬到 / 掉下来的高度（米），完攀 = 墙高；没戴设备测不到时为 null。
"""
import json
from datetime import datetime, timedelta

from panda import store
from panda.store import _db, _lock, label, impact_level, IMPACT_HARD

SHAPES = ["jug", "sloper", "crimp", "pinch", "pocket", "volume"]  # App 里岩点图片的造型，按线路编号轮换
WEEKDAY = "一二三四五六日"
RADAR_DIMS = ["指力", "耐力", "爆发", "脚法", "核心", "落地"]


def settings(config):
    s = {"gym": "我的岩馆", "wall": 4.2, "name": "我", "goal_grade": None}
    s.update((config or {}).get("panda_app") or {})
    return s


def _dt(s):
    return datetime.fromisoformat(s)


def _md(s):
    d = _dt(s)
    return f"{d.month}/{d.day}"


def _hm(s):
    return _dt(s).strftime("%H:%M")


def _r1(x):
    return None if x is None else round(float(x), 1)


def devices():
    """有记录的设备，最近活动的在前"""
    with _lock, _db() as c:
        rows = c.execute("SELECT device_id, MAX(last_active_at) t FROM sessions GROUP BY device_id ORDER BY t DESC").fetchall()
        return [{"device": r["device_id"], "last_active": r["t"]} for r in rows]


def default_device():
    d = devices()
    return d[0]["device"] if d else None


# ---------------------------------------------------------------------------
def state(device_id, config=None, online=None):
    cfg = settings(config)
    wall = float(cfg["wall"])
    now = store._now()
    with _lock, _db() as c:
        sess = _pick_session(c, device_id, now)
        routes, current = _routes(c, device_id, sess, wall)
        out = {
            "live": True, "device": device_id, "now": now.isoformat(sep=" "),
            "online": online,
            "gym": {"name": cfg["gym"], "wall": wall}, "user": {"name": cfg["name"]},
            "session": _session_info(c, sess, now),
            "routes": routes, "current": current,
        }
        out["home"] = _home(c, device_id, sess, routes, now, wall)
        out["review"] = _review(c, device_id, sess, routes, wall) if sess else None
        out["growth"] = _growth(c, device_id, routes, now, wall)
        out["me"] = _me(c, device_id, cfg, now)
    return out


def _pick_session(c, device_id, now):
    """「今天」这一页显示哪次训练：进行中的那次；没有就显示最近一次有记录的（已结束）"""
    s = c.execute("SELECT * FROM sessions WHERE device_id=? AND ended_at IS NULL ORDER BY id DESC LIMIT 1",
                  (device_id,)).fetchone()
    if s and now - _dt(s["last_active_at"]) <= timedelta(hours=store.SESSION_IDLE_HOURS):
        return s
    return c.execute(
        "SELECT s.* FROM sessions s WHERE device_id=? AND EXISTS (SELECT 1 FROM attempts a WHERE a.session_id=s.id AND a.deleted=0) "
        "ORDER BY id DESC LIMIT 1", (device_id,)).fetchone()


def _active(sess, now):
    return bool(sess) and sess["ended_at"] is None and now - _dt(sess["last_active_at"]) <= timedelta(hours=store.SESSION_IDLE_HOURS)


def _session_info(c, sess, now):
    if not sess:
        return None
    last = c.execute("SELECT MAX(created_at) FROM attempts WHERE session_id=? AND deleted=0", (sess["id"],)).fetchone()[0]
    start = _dt(sess["started_at"])
    active = _active(sess, now)
    end = now if active else _dt(last or sess["last_active_at"])
    return {
        "id": sess["id"], "active": active, "ended": not active,
        "start": start.strftime("%H:%M"), "end": end.strftime("%H:%M"),
        "minutes": max(1, int((end - start).total_seconds() // 60)),
        "date": f"{start.month} 月 {start.day} 日", "weekday": "周" + WEEKDAY[start.weekday()],
        "is_today": start.date() == now.date(),
    }


def _attempt_view(a, wall):
    ok = a["result"] == "send"
    h = wall if ok else _r1(a["fall_m"])
    return {"id": a["id"], "t": _hm(a["created_at"]), "ok": ok, "h": h,
            "imp": impact_level(a["impact_g"]), "impact_g": _r1(a["impact_g"]),
            "rest_s": a["rest_s"], "climb_s": a["climb_s"]}


def _routes(c, device_id, sess, wall):
    sid = sess["id"] if sess else -1
    rows = c.execute(
        "SELECT r.* FROM routes r WHERE r.device_id=? AND (r.retired_at IS NULL OR EXISTS "
        "(SELECT 1 FROM attempts a WHERE a.route_id=r.id AND a.session_id=?)) AND (EXISTS "
        "(SELECT 1 FROM attempts a WHERE a.route_id=r.id AND a.deleted=0) OR EXISTS "
        "(SELECT 1 FROM route_visits v WHERE v.route_id=r.id AND v.session_id=?)) ORDER BY r.id",
        (device_id, sid, sid)).fetchall()
    cur = store._current_visit(c, sid) if sess else None
    out = []
    for r in rows:
        atts = c.execute("SELECT a.*, s.started_at s_start FROM attempts a JOIN sessions s ON s.id=a.session_id "
                         "WHERE a.route_id=? AND a.deleted=0 ORDER BY a.id", (r["id"],)).fetchall()
        history, today = [], []
        for a in atts:
            if a["session_id"] == sid:
                today.append(_attempt_view(a, wall))
            else:
                if not history or history[-1]["sid"] != a["session_id"]:
                    history.append({"sid": a["session_id"], "d": _md(a["s_start"]), "h": [], "ok": [], "t": []})
                v = _attempt_view(a, wall)
                history[-1]["h"].append(v["h"])
                history[-1]["ok"].append(v["ok"])
                history[-1]["t"].append(v["t"])
        for hst in history:
            hst.pop("sid")
        sends = [a for a in atts if a["result"] == "send"]
        notes = c.execute("SELECT id, text, created_at FROM route_notes WHERE route_id=? AND deleted=0 ORDER BY id",
                          (r["id"],)).fetchall()
        latest = notes[-1] if notes else None
        first_send = sends[0] if sends else None
        x = {
            "id": f"r{r['id']}", "rid": r["id"], "hold": SHAPES[r["id"] % len(SHAPES)],
            "color": r["color"], "grade": f"V{r['grade']}", "label": label(r["color"], r["grade"]),
            "tag": r["note"] or "", "wall": r["note"] or "",
            "status": "sent" if sends else "proj", "reset": None, "retired": bool(r["retired_at"]),
            "history": history, "today": today,
            "note": (r["guide"] or (latest["text"] if latest else "")),
            "noteDate": (("今天" if sess and latest["created_at"][:10] == store._now().isoformat()[:10]
                          else _md(latest["created_at"])) + " 记") if latest else "",
            "notes": [{"id": n["id"], "text": n["text"], "date": _md(n["created_at"])} for n in notes],
            "guide": r["guide"] or "",
            "sentDate": (_md(first_send["s_start"]) if first_send and first_send["session_id"] != sid else
                         ("今天" if first_send else "")),
            "last": _md(atts[-1]["created_at"]) if atts else None,
            "last_days": (store._now().date() - _dt(atts[-1]["created_at"]).date()).days if atts else None,
        }
        x["advice"] = _advice(x, wall)
        out.append(x)
    return out, (f"r{cur['route_id']}" if cur else None)


def _falls(r, today_only=False):
    hs = [a["h"] for a in r["today"] if not a["ok"] and a["h"] is not None]
    if not today_only:
        for s in r["history"]:
            hs += [h for h, ok in zip(s["h"], s["ok"]) if not ok and h is not None]
    return hs


def _advice(r, wall):
    """线路详情「怎么爬」里小搭的一段话：用记录说事，不编新技巧"""
    parts = []
    today = _falls(r, today_only=True)
    allf = _falls(r)
    if r["status"] == "sent":
        n_before = sum(len(s["h"]) for s in r["history"])
        first_today = next((i for i, a in enumerate(r["today"]) if a["ok"]), None)
        if r["sentDate"] == "今天" and first_today is not None:
            parts.append(f"今天第 {n_before + first_today + 1} 次拿下了这条。")
        else:
            parts.append(f"{r['sentDate']} 就拿下了这条。")
        parts.append("下次可以试试一次完攀，或者找一条同类型、高一级的线。")
    elif allf:
        best = max(allf)
        if len(today) >= 3:
            lo, hi = min(today), max(today)
            parts.append(f"今天 {len(r['today'])} 次里掉下来的位置在 {lo:.1f} 到 {hi:.1f} 米。")
            if hi - lo <= 0.8:
                parts.append("位置很集中，说明卡在同一步，可以专门想想那一步怎么做。")
        parts.append(f"最高一次到了 {best:.1f} 米，离顶还差 {max(0, wall - best):.1f} 米。")
        if len(allf) >= 2 and r["history"]:
            first = [h for h, ok in zip(r["history"][0]["h"], r["history"][0]["ok"]) if not ok and h is not None]
            if first and best - max(first) >= 0.3:
                parts.append(f"比第一次来时高了 {best - max(first):.1f} 米，在变近。")
    if r["guide"]:
        parts.append(f"你记过：{r['guide']}")
    return "".join(parts) or None


# ---------------------------------------------------------------------------
def _home(c, device_id, sess, routes, now, wall):
    sessions = c.execute(
        "SELECT s.*, (SELECT COUNT(*) FROM attempts a WHERE a.session_id=s.id AND a.deleted=0) n FROM sessions s "
        "WHERE device_id=? ORDER BY id DESC", (device_id,)).fetchall()
    sessions = [s for s in sessions if s["n"]]
    active = _active(sess, now)
    prev = [s for s in sessions if not (active and s["id"] == sess["id"])]
    kw = []
    rest_days = (now.date() - _dt(prev[0]["started_at"]).date()).days if prev else None
    if rest_days is None:
        kw.append("第一次训练")
    elif rest_days == 0:
        kw.append("今天练过了" if not active else "今天第 2 练")
    else:
        kw.append(f"已休息 {rest_days} 天")
    week = sum(1 for s in sessions if now - _dt(s["started_at"]) <= timedelta(days=7))
    kw.append(f"近 7 天爬了 {week} 次")
    load = _load(c, device_id, now)
    kw.append("手指偏累" if load["level"] == "high" else "手指状态正常")
    hard = c.execute("SELECT COUNT(*) FROM safety_events WHERE device_id=? AND kind='hard_fall' AND created_at>=?",
                     (device_id, store._iso(now - timedelta(days=7)))).fetchone()[0]
    kw.append(f"本周 {hard} 次重摔")

    cond = None
    if active:
        rows = c.execute("SELECT id, route_id, result, created_at, fall_m, impact_g, climb_s, rest_s FROM attempts "
                         "WHERE session_id=? AND deleted=0 ORDER BY id", (sess["id"],)).fetchall()
        cond = store._condition(rows, sess)
    if cond and cond["tired"]:
        big = {"收工": "练得挺足了，可以收工了", "换线": "有点累了，换条线找找手感", "歇一歇": "有点累了，先歇五分钟"}[cond["advice"]]
    elif active:
        big = "训练中，状态在线"
    elif load["level"] == "high":
        big = "手指偏累，今天以技术为主"
    elif rest_days == 0:
        big = "今天已经练过，好好恢复"
    elif rest_days is not None and rest_days >= 2:
        big = "状态不错，可以冲一冲难度"
    else:
        big = "状态正常，按计划练"

    tips = []
    rv = c.execute("SELECT r.full FROM reviews r JOIN sessions s ON s.id=r.session_id WHERE s.device_id=? AND r.full<>'' "
                   "ORDER BY r.session_id DESC LIMIT 1", (device_id,)).fetchone()
    if rv:
        try:
            for t in (json.loads(rv["full"]).get("下次练") or [])[:2]:
                tips.append([t, "来自上次复盘"])
        except ValueError:
            pass
    proj = [r for r in routes if r["status"] == "proj" and not r["retired"] and _falls(r)]
    proj.sort(key=lambda r: wall - max(_falls(r)))
    if proj:
        r = proj[0]
        tips.append([f"去试试{r['color']}色 {r['grade']}", f"离顶只差 {max(0, wall - max(_falls(r))):.1f} 米"])
    if load["level"] == "high":
        tips.append(["今天少上小点线", f"近 7 天尝试 {load['now']} 次，比上周多 {load['pct']}%"])
    if len(tips) < 2:
        tips.append(["先热身 10 分钟", "从简单线开始，一次完攀找手感"])
    if len(tips) < 3:
        tips.append(["两次之间歇够 2 到 3 分钟", "歇太短手指容易劳损"])
    return {"big": big, "kw": kw, "tips": tips[:3], "condition": cond}


def _load(c, device_id, now):
    """手指负荷：近 7 天尝试次数和再往前 7 天比"""
    def n(a, b):
        return c.execute("SELECT COUNT(*) FROM attempts a JOIN sessions s ON s.id=a.session_id WHERE s.device_id=? "
                         "AND a.deleted=0 AND a.created_at>=? AND a.created_at<?",
                         (device_id, store._iso(now - timedelta(days=a)), store._iso(now - timedelta(days=b)))).fetchone()[0]
    cur, prev = n(7, -1), n(14, 7)
    pct = round((cur - prev) / prev * 100) if prev else (100 if cur else 0)
    level = "high" if (prev and pct >= 30 and cur >= 20) or (not prev and cur >= 40) else ("mid" if pct >= 15 and cur >= 15 else "ok")
    return {"now": cur, "prev": prev, "pct": pct, "level": level}


# ---------------------------------------------------------------------------
def _cat(text):
    if any(k in text for k in ("摔", "落地", "手指", "膝", "肩", "疼", "伤")):
        return "身体"
    if any(k in text for k in ("累", "休息", "歇", "体能", "疲")):
        return "体能"
    if any(k in text for k in ("急", "心态", "情绪", "烦")):
        return "情绪"
    return "技术"


def _review(c, device_id, sess, routes, wall):
    st = store._session_stats(c, sess)
    st["usual"] = store._usual(c, device_id, sess["id"])
    row = c.execute("SELECT * FROM reviews WHERE session_id=?", (sess["id"],)).fetchone()
    full = {}
    if row and row["full"]:
        try:
            full = json.loads(row["full"])
        except ValueError:
            full = {}
    u = st["usual"]
    sub = f"完攀 {st['routes_sent']} 条，完攀率 {st['send_rate']}%"
    if u and u["send_rate"] != st["send_rate"]:
        sub += f"，平时 {u['send_rate']}%"
    title = full.get("结论") or _auto_title(st)

    # 亮点：高度刷新、一次完攀、以前没拿下今天拿下
    hl = []
    for r in routes:
        today = _falls(r, today_only=True)
        before = [h for s in r["history"] for h, ok in zip(s["h"], s["ok"]) if not ok and h is not None]
        if today and before and max(today) - max(before) >= 0.3:
            hl.append([f"+{max(today) - max(before):.1f}", "m", f"{r['color']}色 {r['grade']} 刷新最高点"])
    if st["first_ever_sends"]:
        hl.append([str(len(st["first_ever_sends"])), "条", f"{'、'.join(st['first_ever_sends'][:2])} 第一次拿下"])
    if st["flashes"]:
        hl.append([str(len(st["flashes"])), "条", "一次就完攀"])
    if not hl and st["sends"]:
        hl.append([str(st["sends"]), "次", "完攀"])
    remind = [[_cat(t), t] for t in (full.get("提醒") or [])]
    if not remind:
        remind = _auto_remind(st)
    nxt = [[t, ""] for t in (full.get("下次练") or [])] or [[t, ""] for t in _auto_next(st)]
    return {
        "has_full": bool(full), "spoken": (row["spoken"] if row else "") or "",
        "concl": {"title": title, "sub": sub}, "highlights": hl[:2],
        "good": full.get("亮点") or [], "remind": remind[:4],
        "next": {"minutes": 25 if len(nxt) <= 2 else 40, "items": nxt[:3]},
        "stats": {k: st[k] for k in ("minutes", "route_count", "attempts", "sends", "send_rate", "routes_sent", "hardest_send")},
        "timeline": _timeline(c, sess, st),
    }


def _auto_title(st):
    cond = st.get("condition") or {}
    u = st.get("usual")
    if cond.get("tired"):
        return "前半程不错，后程累了"
    if st["first_ever_sends"]:
        return "拿下了以前没拿下的线"
    if u and st["send_rate"] >= u["send_rate"] + 10:
        return "比平时爬得更顺"
    if st["sends"] == 0:
        return "今天都在攻克，没有白爬"
    return "稳稳地练了一场"


def _auto_remind(st):
    out = []
    left = [r for r in st["routes"] if not r["sends"] and r["label"] != "未知线路"]
    if left:
        r = max(left, key=lambda r: r["attempts"])
        out.append(["技术", f"{r['label']}试了 {r['attempts']} 次还没拿下" +
                    (f"，最高掉在 {r['best_fall']:.1f} 米。" if r.get("best_fall") else "。")])
    cond = st.get("condition") or {}
    if cond.get("signals"):
        out.append(["体能", "；".join(cond["signals"]) + "。"])
    m = st.get("motion") or {}
    if m.get("short_rests"):
        out.append(["体能", f"有 {m['short_rests']} 次两次之间歇了不到 1 分钟，歇够 2 到 3 分钟再上。"])
    if m.get("hard_falls"):
        out.append(["身体", f"今天重摔了 {m['hard_falls']} 次，落地记得屈膝、顺势往后滚。"])
    return out or [["技术", "继续保持，下次挑一条高一级的线试试。"]]


def _auto_next(st):
    left = [r for r in st["routes"] if not r["sends"] and r["label"] != "未知线路"]
    out = ["先热身 10 分钟，挑简单线找手感"]
    if left:
        out.append(f"手还新鲜的时候再试{max(left, key=lambda r: r['attempts'])['label']}")
    elif st["hardest_send"]:
        out.append(f"今天拿下了{st['hardest_send']}，下次试试高一级的线")
    return out


def _timeline(c, sess, st):
    start = _dt(sess["started_at"])
    rows = c.execute("SELECT id, route_id, result, created_at, fall_m, impact_g, climb_s, rest_s FROM attempts "
                     "WHERE session_id=? AND deleted=0 ORDER BY id", (sess["id"],)).fetchall()
    minutes = max(1, st["minutes"])
    tired_at = None
    cond = st.get("condition") or {}
    if cond.get("tired") and len(rows) >= 4:
        tired_at = int((_dt(rows[-4]["created_at"]) - start).total_seconds() // 60)
    ev = c.execute("SELECT kind, created_at FROM safety_events WHERE session_id=? ORDER BY id", (sess["id"],)).fetchall()
    words = {"rest_short": "歇一歇", "hard_fall": "屈膝落地", "sos": "摔后报警"}
    reminders = [[_hm(e["created_at"]), words[e["kind"]]] for e in ev if e["kind"] in words][:3]
    ticks = list(dict.fromkeys((start + timedelta(minutes=minutes * i / 3)).strftime("%H:%M") for i in range(4)))
    if len(ticks) < 4:  # 训练太短，刻度会重叠：只标开始和结束
        ticks = list(dict.fromkeys([ticks[0], (start + timedelta(minutes=minutes)).strftime("%H:%M")]))
    return {"start": start.strftime("%H:%M"), "minutes": minutes, "tired_at": tired_at,
            "reminders": reminders, "ticks": ticks}


# ---------------------------------------------------------------------------
def _window(c, device_id, now, days_from, days_to):
    return c.execute(
        "SELECT a.*, r.grade, r.id rid FROM attempts a JOIN sessions s ON s.id=a.session_id LEFT JOIN routes r ON r.id=a.route_id "
        "WHERE s.device_id=? AND a.deleted=0 AND a.created_at>=? AND a.created_at<?",
        (device_id, store._iso(now - timedelta(days=days_from)), store._iso(now - timedelta(days=days_to)))).fetchall()


def _radar(rows):
    """能力雷达：每一项都只用设备和按键真的记下的数据估算（没有数据的项给 None，App 显示「数据不够」）"""
    if not rows:
        return [None] * 6
    sends = [r for r in rows if r["result"] == "send"]
    graded = [r["grade"] for r in sends if r["grade"] is not None]
    finger = min(95, 25 + 12 * max(graded)) if graded else None
    climbs = [r["climb_s"] for r in sends if r["climb_s"]]
    endure = min(95, round(20 + 1.2 * sum(climbs) / len(climbs))) if climbs else None
    burst = round(30 + 60 * len(sends) / len(rows))
    smooth = [r["smooth"] for r in rows if r["smooth"] is not None]
    foot = round(15 + 0.8 * sum(smooth) / len(smooth)) if smooth else None
    sw = [r["swings"] for r in rows if r["swings"] is not None]
    core = max(20, round(90 - 20 * sum(sw) / len(sw))) if sw else None
    imp = [r["impact_g"] for r in rows if r["impact_g"] is not None]
    if imp:
        heavy = sum(g >= IMPACT_HARD for g in imp) / len(imp)
        mid = sum(store.IMPACT_MID <= g < IMPACT_HARD for g in imp) / len(imp)
        land = max(20, round(92 - 150 * heavy - 40 * mid))
    else:
        land = None
    return [finger, endure, burst, foot, core, land]


RADAR_SRC = [
    "最难完攀的难度（V0–V6）",
    "完攀那几次在墙上待了多久（设备测的攀爬时长）",
    "所有尝试里完攀的比例",
    "动作流畅度（设备测的加速度变化，越平稳越高）",
    "身体大幅晃动的次数（越少越稳）",
    "落地冲击轻、中、重的比例",
]
RADAR_TIP = [
    "多试高一级的线，先把动作拆开练",
    "练一些长线或连续上下的耐力循环",
    "攻克中的线歇够再上，每次都全力",
    "上墙前看清脚点，脚放稳了再移手",
    "仰角线收紧核心，脚别轻易离墙",
    "落地屈膝、顺势后滚，别用手撑",
]


def _growth(c, device_id, routes, now, wall):
    recent = _window(c, device_id, now, 14, -1)
    then = _window(c, device_id, now, 49, 35)  # 6 周前那两周
    def rate(rows):
        return round(sum(r["result"] == "send" for r in rows) / len(rows) * 100) if rows else None
    r_now, r_then = rate(recent), rate(then)
    keys = []
    keys.append(["完攀率", r_now if r_now is not None else "—", "%" if r_now is not None else "",
                 (f"提高了 {r_now - r_then}%" if r_then is not None and r_now is not None and r_now > r_then else
                  (f"少了 {r_then - r_now}%" if r_then is not None and r_now is not None and r_now < r_then else "近两周")),
                 "up" if (r_then is not None and r_now is not None and r_now > r_then) else ("dn" if r_then is not None and r_now is not None and r_now < r_then else "")])
    proj = [r for r in routes if r["status"] == "proj" and not r["retired"] and _falls(r)]
    if proj:
        p = max(proj, key=lambda r: (int(r["grade"][1:]), max(_falls(r))))
        best = max(_falls(p))
        first = []
        if p["history"]:
            first = [h for h, ok in zip(p["history"][0]["h"], p["history"][0]["ok"]) if not ok and h is not None]
        closer = best - max(first) if first else 0
        keys.append([f"{p['grade']} 离顶", f"{max(0, wall - best):.1f}", "m",
                     f"近了 {closer:.1f}m" if closer >= 0.1 else f"{p['color']}色 {p['grade']}", "up" if closer >= 0.1 else ""])
    def avg_tries(rows):
        by = {}
        for r in sorted(rows, key=lambda r: r["id"]):
            if r["rid"] is None:
                continue
            x = by.setdefault(r["rid"], {"n": 0, "sent": None})
            if x["sent"] is None:
                x["n"] += 1
                if r["result"] == "send":
                    x["sent"] = x["n"]
        sent = [x["sent"] for x in by.values() if x["sent"]]
        return round(sum(sent) / len(sent), 1) if sent else None
    t_now, t_then = avg_tries(recent), avg_tries(then)
    keys.append(["平均几次完攀", t_now if t_now is not None else "—", "次" if t_now is not None else "",
                 f"原来 {t_then} 次" if t_then is not None else "近两周",
                 "up" if (t_now is not None and t_then is not None and t_now < t_then) else ""])

    pyr = []
    for g in range(6, -1, -1):
        n = c.execute("SELECT COUNT(DISTINCT a.route_id) FROM attempts a JOIN routes r ON r.id=a.route_id "
                      "WHERE r.device_id=? AND r.grade=? AND a.result='send' AND a.deleted=0", (device_id, g)).fetchone()[0]
        pyr.append({"g": f"V{g}", "n": n})

    rad_now, rad_then = _radar(recent), _radar(then)
    dims = []
    for i, k in enumerate(RADAR_DIMS):
        a, b = rad_now[i], rad_then[i]
        dims.append({"k": k, "d": (a - b) if (a is not None and b is not None) else 0, "src": RADAR_SRC[i],
                     "why": ("近两周的数据不够，多戴着攀搭爬几次就有了" if a is None else
                             f"近两周 {a} 分" + (f"，6 周前 {b} 分" if b is not None else "，6 周前没有记录")),
                     "tip": RADAR_TIP[i], "missing": a is None})
    known = [(v, i) for i, v in enumerate(rad_now) if v is not None]
    weakest = min(known)[1] if known else 3

    load = _load(c, device_id, now)
    week = store._iso(now - timedelta(days=7))
    hard = c.execute("SELECT COUNT(*) FROM safety_events WHERE device_id=? AND kind='hard_fall' AND created_at>=?",
                     (device_id, week)).fetchone()[0]
    wk = _window(c, device_id, now, 7, -1)
    short = sum(1 for r in wk if r["rest_s"] is not None and r["rest_s"] < 60)
    heavy = sum(1 for r in wk if r["impact_g"] is not None and r["impact_g"] >= IMPACT_HARD)
    swings = sum(r["swings"] or 0 for r in wk)
    zones = _zones(load, max(hard, heavy), swings, short, len(wk))

    summary = []
    if r_now is not None:
        summary.append(f"近两周完攀率 {r_now}%" + (f"，6 周前是 {r_then}%" if r_then is not None else ""))
    top = next((p["g"] for p in pyr if p["n"]), None)
    if top:
        summary.append(f"完攀过最难的是 {top}")
    if known:
        summary.append(f"现在最该补的是{RADAR_DIMS[weakest]}")
    return {
        "summary": "，".join(summary) + "。" if summary else "多爬几次，小搭就能看出你的变化。",
        "keys": keys, "pyr": pyr,
        "radar": {"dims": RADAR_DIMS, "now": [v if v is not None else 0 for v in rad_now],
                  "then": [v if v is not None else 0 for v in rad_then], "detail": dims, "weakest": weakest,
                  "has_then": any(v is not None for v in rad_then)},
        # 上周没练过时没法比，load_pct 为空，App 改成显示本周尝试次数
        "body": {"load_pct": load["pct"] if load["prev"] else None, "load_now": load["now"],
                 "hard_falls": hard, "short_rests": short, "zones": zones},
    }


RED, ORANGE, GREEN = "#DD3A2E", "#F07F1E", "#2EB872"


def _zones(load, hard, swings, short, n):
    """身体状态：只按记录到的负荷和冲击判断，不做诊断"""
    def z(name, col, level, pct, why, tip):
        return {"name": name, "col": col, "level": level, "pct": pct, "why": why, "tip": tip}
    f_col = RED if load["level"] == "high" else (ORANGE if load["level"] == "mid" or short >= 3 else GREEN)
    f_lv = {RED: "负荷偏高", ORANGE: "留意", GREEN: "正常"}[f_col]
    out = {
        "finger": z("手指", f_col, f_lv, min(95, 30 + load["now"]),
                    f"近 7 天尝试 {load['now']} 次，上周 {load['prev']} 次" + (f"；有 {short} 次歇了不到 1 分钟" if short else ""),
                    "今天少上小点线，两次之间歇够" if f_col != GREEN else "保持现在的节奏"),
        "forearm": z("前臂", ORANGE if load["level"] != "ok" else GREEN, "留意" if load["level"] != "ok" else "正常",
                     min(90, 25 + load["now"]), f"近 7 天尝试 {load['now']} 次", "爬完拉伸前臂，热身别省"),
        "shoulder": z("肩膀", ORANGE if swings >= 8 else GREEN, "留意" if swings >= 8 else "正常", min(90, 20 + swings * 5),
                      f"近 7 天身体大幅晃动 {swings} 次", "仰角线收紧核心，别甩"),
        "core": z("核心", GREEN, "正常", 35, f"近 7 天 {n} 次尝试", "保持"),
        "knee": z("膝盖", RED if hard >= 2 else (ORANGE if hard else GREEN), "偏高" if hard >= 2 else ("留意" if hard else "正常"),
                  min(95, 20 + hard * 25), f"近 7 天重摔 {hard} 次", "落地屈膝，顺势往后滚"),
        "ankle": z("脚踝", ORANGE if hard else GREEN, "留意" if hard else "正常", min(90, 15 + hard * 20),
                   f"近 7 天重摔 {hard} 次", "别直腿落地"),
    }
    return out


def _me(c, device_id, cfg, now):
    rows = c.execute(
        "SELECT s.started_at, MAX(a.created_at) last, COUNT(a.id) n, SUM(a.result='send') k FROM sessions s "
        "JOIN attempts a ON a.session_id=s.id AND a.deleted=0 WHERE s.device_id=? GROUP BY s.id ORDER BY s.id", (device_id,)).fetchall()
    minutes = sum(max(0, (_dt(r["last"]) - _dt(r["started_at"])).total_seconds() / 60) for r in rows)
    top = c.execute("SELECT MAX(r.grade) FROM attempts a JOIN routes r ON r.id=a.route_id WHERE r.device_id=? AND a.result='send' "
                    "AND a.deleted=0", (device_id,)).fetchone()[0]
    weeks = max(1, (now - _dt(rows[0]["started_at"])).days // 7 + 1) if rows else 0
    goal_g = cfg.get("goal_grade")
    if goal_g is None:
        goal_g = min(6, (top if top is not None else 1) + 1)
    # 目标进度：这个难度的线最高爬到哪（完攀 = 100%）
    best = c.execute("SELECT MAX(CASE WHEN a.result='send' THEN 999 ELSE a.fall_m END) FROM attempts a JOIN routes r ON r.id=a.route_id "
                     "WHERE r.device_id=? AND r.grade=? AND a.deleted=0", (device_id, goal_g)).fetchone()[0]
    wall = float(cfg["wall"])
    pct = 100 if best == 999 else (round(min(99, best / wall * 100)) if best else 0)
    return {"name": cfg["name"], "sessions": len(rows), "sends": sum(r["k"] or 0 for r in rows),
            "hours": round(minutes / 60, 1), "weeks": weeks, "top": f"V{top}" if top is not None else "—",
            "gym": cfg["gym"], "wall": wall,
            "goal": {"text": f"完攀一条 V{goal_g}", "pct": pct,
                     "sub": "已经拿下了！" if pct == 100 else (f"V{goal_g} 最高爬到 {best:.1f} 米" if best else f"还没试过 V{goal_g}")}}


# ---------------------------------------------------------------------------
# 修改记录（App「修改这次记录」）
def edit_attempt(device_id, attempt_id, action):
    """action: send / fail / delete。只能改自己设备的记录。返回 True/False"""
    with _lock, _db() as c:
        row = c.execute("SELECT a.id FROM attempts a JOIN sessions s ON s.id=a.session_id WHERE a.id=? AND s.device_id=?",
                        (attempt_id, device_id)).fetchone()
        if not row:
            return False
        if action == "delete":
            c.execute("UPDATE attempts SET deleted=1 WHERE id=?", (attempt_id,))
        elif action in ("send", "fail"):
            c.execute("UPDATE attempts SET result=? WHERE id=?", (action, attempt_id))
        else:
            return False
        return True


def route_of(device_id, rid):
    with _lock, _db() as c:
        r = c.execute("SELECT * FROM routes WHERE id=? AND device_id=?", (rid, device_id)).fetchone()
        return dict(r) if r else None
