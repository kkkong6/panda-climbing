"""
攀搭数据存储（SQLite，文件：data/panda.db）

表结构
  sessions      一次训练（开机后第一次登记线路开始；闲置超过 SESSION_IDLE_HOURS 小时算新的一次）
  routes        一条线路（颜色 + 难度 + 描述），跨训练长期存在，所以能认出“上周那条”
  route_visits  某次训练里切换到某条线路的记录；每次训练里最后一条 = 当前线路
  attempts      每次尝试（按键记录）：成功 send / 失败 fail；还没登记线路时 route_id 为空（未知线路）
                带运动数据（设备当场算好上传）：下落高度 fall_m、落地冲击 impact_g、攀爬时长 climb_s、
                休息时长 rest_s、大幅晃动次数 swings、动作流畅度 smooth（0–100）；没戴设备 / 没测到时为空
  reviews       当日复盘（统计 + 语音版 + 完整版）
  route_notes   线路笔记（用户说的爬法心得原话）；routes.guide 是大模型把全部笔记整理成的一句攻略
  safety_events 安全事件：重摔 hard_fall、摔后不动报警 sos、报警取消 sos_cancel、休息过短提醒 rest_short
"""
import os
import re
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "panda.db")
SESSION_IDLE_HOURS = 3

COLORS = ["黄", "粉", "红", "蓝", "紫", "绿", "白", "黑"]
GRADES = range(0, 7)  # V0–V6

_COLOR_ALIASES = {
    "黄": "黄", "粉": "粉", "红": "红", "蓝": "蓝", "紫": "紫", "绿": "绿", "白": "白", "黑": "黑",
    "yellow": "黄", "pink": "粉", "red": "红", "blue": "蓝", "purple": "紫", "green": "绿",
    "white": "白", "black": "黑", "橙": None,
}
_CN_NUM = {"零": 0, "〇": 0, "一": 1, "幺": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6}

_lock = threading.Lock()


def _now():
    return datetime.now().replace(microsecond=0)


def _iso(dt):
    return dt.isoformat(sep=" ")


@contextmanager
def _db():
    """打开数据库，成功就提交、出错就回滚，最后关闭"""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with _db() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id TEXT NOT NULL,
                started_at TEXT NOT NULL,
                last_active_at TEXT NOT NULL,
                ended_at TEXT
            );
            CREATE TABLE IF NOT EXISTS routes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id TEXT NOT NULL,
                color TEXT NOT NULL,
                grade INTEGER NOT NULL,
                note TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                retired_at TEXT
            );
            CREATE TABLE IF NOT EXISTS route_visits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL REFERENCES sessions(id),
                route_id INTEGER NOT NULL REFERENCES routes(id),
                started_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL REFERENCES sessions(id),
                route_id INTEGER REFERENCES routes(id),
                result TEXT NOT NULL CHECK (result IN ('send', 'fail')),
                created_at TEXT NOT NULL,
                client_id TEXT,
                deleted INTEGER NOT NULL DEFAULT 0
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_attempts_client ON attempts(session_id, client_id);
            CREATE INDEX IF NOT EXISTS idx_attempts_route ON attempts(session_id, route_id);
            CREATE TABLE IF NOT EXISTS reviews (
                session_id INTEGER PRIMARY KEY REFERENCES sessions(id),
                stats TEXT NOT NULL,
                spoken TEXT DEFAULT '',
                full TEXT DEFAULT '',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_routes_dev ON routes(device_id, color, grade);
            CREATE INDEX IF NOT EXISTS idx_visits_session ON route_visits(session_id);
            CREATE TABLE IF NOT EXISTS route_notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                route_id INTEGER NOT NULL REFERENCES routes(id),
                session_id INTEGER REFERENCES sessions(id),
                text TEXT NOT NULL,
                created_at TEXT NOT NULL,
                deleted INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_notes_route ON route_notes(route_id);
            CREATE TABLE IF NOT EXISTS safety_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id TEXT NOT NULL,
                session_id INTEGER REFERENCES sessions(id),
                route_id INTEGER REFERENCES routes(id),
                kind TEXT NOT NULL,
                impact_g REAL,
                fall_m REAL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_safety_dev ON safety_events(device_id, created_at);
            """
        )
        # 旧数据库补列（10/19 加运动数据和线路攻略）
        for table, col, decl in MIGRATIONS:
            cols = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
            if col not in cols:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


MIGRATIONS = [
    ("attempts", "fall_m", "REAL"),
    ("attempts", "impact_g", "REAL"),
    ("attempts", "climb_s", "REAL"),
    ("attempts", "rest_s", "REAL"),
    ("attempts", "swings", "INTEGER"),
    ("attempts", "smooth", "INTEGER"),
    ("routes", "guide", "TEXT DEFAULT ''"),
    ("routes", "guide_at", "TEXT"),
]

# 运动数据字段：(名字, 最小, 最大)。超出范围的当成测错，不存
MOTION_FIELDS = {
    "fall_m": (0.0, 6.0),
    "impact_g": (0.0, 32.0),
    "climb_s": (0.0, 900.0),
    "rest_s": (0.0, 7200.0),
    "swings": (0, 50),
    "smooth": (0, 100),
}
# 落地冲击分级（初始数值，岩馆实测后再调）：< IMPACT_MID 轻，< IMPACT_HARD 中，否则重
IMPACT_MID = 4.0
IMPACT_HARD = 8.0


def clean_motion(motion):
    """设备上传的运动数据 → 只留认识的字段、数值在合理范围内的；都没有返回 {}"""
    out = {}
    if not isinstance(motion, dict):
        return out
    for k, (lo, hi) in MOTION_FIELDS.items():
        v = motion.get(k)
        if v is None or isinstance(v, bool):
            continue
        try:
            v = float(v)
        except (TypeError, ValueError):
            continue
        if lo <= v <= hi:
            out[k] = int(round(v)) if isinstance(lo, int) else round(v, 2)
    return out


def impact_level(g):
    if g is None:
        return None
    return "轻" if g < IMPACT_MID else ("中" if g < IMPACT_HARD else "重")


# ---------- 解析 ----------
def normalize_color(s):
    if not s:
        return None
    s = str(s).strip().lower().replace("色", "")
    for k, v in _COLOR_ALIASES.items():
        if k in s:
            return v
    return None


def normalize_grade(s):
    """'V3' / 'v 3' / '3' / '三' / '微三' → 3；不合法返回 None"""
    if s is None:
        return None
    s = str(s).strip().upper()
    m = re.search(r"\d+", s)
    if m:
        n = int(m.group(0))
    else:
        n = next((v for k, v in _CN_NUM.items() if k in s), None)
    return n if n is not None and n in GRADES else None


def label(color, grade):
    return f"{color}色V{grade}"


# ---------- 训练 ----------
def _current_session(c, device_id, create, touch=True):
    """当前进行中的训练。touch=False：只读查询（App 看数据、判断规则），不刷新最后活动时间"""
    row = c.execute(
        "SELECT * FROM sessions WHERE device_id=? AND ended_at IS NULL ORDER BY id DESC LIMIT 1",
        (device_id,),
    ).fetchone()
    now = _now()
    if row and now - datetime.fromisoformat(row["last_active_at"]) > timedelta(hours=SESSION_IDLE_HOURS):
        c.execute("UPDATE sessions SET ended_at=? WHERE id=?", (row["last_active_at"], row["id"]))
        row = None
    if row:
        if touch:
            c.execute("UPDATE sessions SET last_active_at=? WHERE id=?", (_iso(now), row["id"]))
        return row, False
    if not create:
        return None, False
    cur = c.execute(
        "INSERT INTO sessions(device_id, started_at, last_active_at) VALUES (?,?,?)",
        (device_id, _iso(now), _iso(now)),
    )
    return c.execute("SELECT * FROM sessions WHERE id=?", (cur.lastrowid,)).fetchone(), True


def _current_visit(c, session_id):
    return c.execute(
        "SELECT v.*, r.color, r.grade, r.note FROM route_visits v JOIN routes r ON r.id=v.route_id "
        "WHERE v.session_id=? ORDER BY v.id DESC LIMIT 1",
        (session_id,),
    ).fetchone()


def _route_history(c, route_id, exclude_session):
    """这条线以前（不含本次训练）来过几次、最近一次是哪天"""
    row = c.execute(
        "SELECT COUNT(DISTINCT session_id) n, MAX(started_at) last FROM route_visits "
        "WHERE route_id=? AND session_id<>?",
        (route_id, exclude_session),
    ).fetchone()
    return row["n"], row["last"]


def _pick_route(c, device_id, color, grade, note, force_new):
    """找已有的同色同难度线路；找不到或要求新建就新建。返回 (route_row, is_new)"""
    cands = c.execute(
        "SELECT r.*, (SELECT MAX(started_at) FROM route_visits v WHERE v.route_id=r.id) last_visit "
        "FROM routes r WHERE device_id=? AND color=? AND grade=? AND retired_at IS NULL "
        "ORDER BY COALESCE(last_visit, r.created_at) DESC",
        (device_id, color, grade),
    ).fetchall()
    note = (note or "").strip()
    chosen = None
    if cands and not force_new:
        if note:
            chosen = next((r for r in cands if r["note"] and (note in r["note"] or r["note"] in note)), None)
            if chosen is None:
                # 有描述但对不上：如果已有线路都写了别的描述，说明是另一条；否则当作补充描述
                no_note = [r for r in cands if not r["note"]]
                chosen = no_note[0] if no_note else None
        else:
            chosen = cands[0]
    if chosen:
        if note and not chosen["note"]:
            c.execute("UPDATE routes SET note=? WHERE id=?", (note, chosen["id"]))
        return c.execute("SELECT * FROM routes WHERE id=?", (chosen["id"],)).fetchone(), False
    if force_new and cands:
        # 岩馆换线：旧的同色同级线路下架（有描述时只下架描述对得上的）
        old = [r for r in cands if not note or not r["note"] or note in r["note"] or r["note"] in note]
        c.executemany("UPDATE routes SET retired_at=? WHERE id=?", [(_iso(_now()), r["id"]) for r in old])
    cur = c.execute(
        "INSERT INTO routes(device_id, color, grade, note, created_at) VALUES (?,?,?,?,?)",
        (device_id, color, grade, note, _iso(_now())),
    )
    return c.execute("SELECT * FROM routes WHERE id=?", (cur.lastrowid,)).fetchone(), True


def _result(c, session, session_new, route, route_new, **extra):
    n_before, last = _route_history(c, route["id"], session["id"])
    note = c.execute("SELECT text, created_at FROM route_notes WHERE route_id=? AND deleted=0 ORDER BY id DESC LIMIT 1",
                     (route["id"],)).fetchone()
    return {
        "ok": True,
        "label": label(route["color"], route["grade"]),
        "color": route["color"],
        "grade": route["grade"],
        "note": route["note"] or "",
        "route_id": route["id"],
        "session_id": session["id"],
        "session_started": session_new,
        "new_route": route_new,
        "sessions_before": n_before,
        "last_date": last[:10] if last else None,
        # 线路笔记：整理好的攻略 + 最近一条原话（登记同一条线时提醒用）
        "guide": route["guide"] or "",
        "last_tip": note["text"] if note else "",
        "last_tip_date": note["created_at"][:10] if note else None,
        **extra,
    }


# ---------- 对外接口 ----------
def register_route(device_id, color, grade, note=None, force_new=False, correction=False):
    color_n, grade_n = normalize_color(color), normalize_grade(grade)
    if not color_n or grade_n is None:
        return {"ok": False, "missing": [k for k, v in (("颜色", color_n), ("难度", grade_n)) if v in (None, "")]}
    with _lock, _db() as c:
        session, session_new = _current_session(c, device_id, create=True)
        replaced, wrong = None, None
        if correction:
            cur = _current_visit(c, session["id"])
            if cur:
                replaced = label(cur["color"], cur["grade"])
                wrong = (cur["route_id"], cur["started_at"])
                c.execute("DELETE FROM route_visits WHERE id=?", (cur["id"],))
        route, route_new = _pick_route(c, device_id, color_n, grade_n, note, force_new)
        moved = 0
        if wrong and wrong[0] != route["id"]:
            # 登记错线路之后按的键，都搬到更正后的线路
            moved = c.execute(
                "UPDATE attempts SET route_id=? WHERE session_id=? AND route_id=? AND created_at>=?",
                (route["id"], session["id"], wrong[0], wrong[1]),
            ).rowcount
            # 刚才误建的线路，如果没有别的记录就一起删掉
            left = c.execute(
                "SELECT (SELECT COUNT(*) FROM route_visits WHERE route_id=?) + (SELECT COUNT(*) FROM attempts WHERE route_id=?)",
                (wrong[0], wrong[0]),
            ).fetchone()[0]
            if left == 0:
                c.execute("DELETE FROM routes WHERE id=?", (wrong[0],))
        cur = _current_visit(c, session["id"])
        same = cur is not None and cur["route_id"] == route["id"]
        if not same:
            c.execute(
                "INSERT INTO route_visits(session_id, route_id, started_at) VALUES (?,?,?)",
                (session["id"], route["id"], _iso(_now())),
            )
        # 还没登记线路时按的键（未知线路），归到这条
        adopted = c.execute(
            "UPDATE attempts SET route_id=? WHERE session_id=? AND route_id IS NULL AND deleted=0",
            (route["id"], session["id"]),
        ).rowcount
        return _result(c, session, session_new, route, route_new, replaced=replaced, already_current=same,
                       adopted=adopted, moved=moved)


def back_to_previous(device_id):
    """回到本次训练里上一条（和当前不同的）线路"""
    with _lock, _db() as c:
        session, _ = _current_session(c, device_id, create=False)
        if not session:
            return {"ok": False, "reason": "今天还没登记过线路"}
        visits = c.execute(
            "SELECT route_id FROM route_visits WHERE session_id=? ORDER BY id DESC", (session["id"],)
        ).fetchall()
        if not visits:
            return {"ok": False, "reason": "今天还没登记过线路"}
        current = visits[0]["route_id"]
        prev = next((v["route_id"] for v in visits if v["route_id"] != current), None)
        if prev is None:
            return {"ok": False, "reason": "今天只爬了这一条"}
        c.execute(
            "INSERT INTO route_visits(session_id, route_id, started_at) VALUES (?,?,?)",
            (session["id"], prev, _iso(_now())),
        )
        route = c.execute("SELECT * FROM routes WHERE id=?", (prev,)).fetchone()
        return _result(c, session, False, route, False)


def today_routes(device_id):
    """本次训练爬过的线路（按第一次登记顺序），给大模型和 App 用"""
    with _lock, _db() as c:
        session, _ = _current_session(c, device_id, create=False, touch=False)
        if not session:
            return []
        rows = c.execute(
            "SELECT r.id, r.color, r.grade, r.note, MIN(v.id) first FROM route_visits v JOIN routes r ON r.id=v.route_id "
            "WHERE v.session_id=? GROUP BY r.id ORDER BY first",
            (session["id"],),
        ).fetchall()
        cur = _current_visit(c, session["id"])
        return [
            {"label": label(r["color"], r["grade"]), "note": r["note"] or "",
             "current": bool(cur and cur["route_id"] == r["id"])}
            for r in rows
        ]


def _attempt_stats(c, session_id, route_id):
    """这条线（或未知线路）本次训练的尝试次数、完攀次数"""
    cond = "route_id IS NULL" if route_id is None else "route_id=?"
    args = (session_id,) if route_id is None else (session_id, route_id)
    row = c.execute(
        f"SELECT COUNT(*) n, SUM(result='send') s FROM attempts WHERE session_id=? AND {cond} AND deleted=0", args
    ).fetchone()
    return row["n"] or 0, row["s"] or 0


def record_attempt(device_id, result, client_id=None, fix=False, motion=None):
    """记录一次尝试（按键）。result: 'send' 成功 / 'fail' 失败
    fix=True：设备 3 秒内改按，同一个 client_id 的结果改成新的 result（重复发送也只改不加）
    motion：设备算好的运动数据（见 MOTION_FIELDS），改按时不带；原记录没有运动数据时才补上
    返回 {ok, duplicate, fixed, attempt_id, result, route_label|None, count, sends, session_started, motion}"""
    result = {"success": "send", "ok": "send", "top": "send"}.get(result, result)
    if result not in ("send", "fail"):
        return {"ok": False, "reason": f"未知结果 {result}"}
    motion = clean_motion(motion)
    with _lock, _db() as c:
        session, session_new = _current_session(c, device_id, create=True)
        dup = fixed = False
        row = None
        if client_id:
            row = c.execute("SELECT * FROM attempts WHERE session_id=? AND client_id=?",
                            (session["id"], client_id)).fetchone()
        if row:
            attempt_id, route_id = row["id"], row["route_id"]
            if fix:
                # 改按：结果相同说明是重发，不算改动
                fixed = row["result"] != result
                if fixed:
                    c.execute("UPDATE attempts SET result=? WHERE id=?", (result, attempt_id))
            else:
                dup, result = True, row["result"]
            if motion and all(row[k] is None for k in MOTION_FIELDS):
                _set_motion(c, attempt_id, motion)
        else:
            # 新记录；改按但原记录没到（比如原记录丢了）也直接按新结果记一次
            cur = _current_visit(c, session["id"])
            route_id = cur["route_id"] if cur else None
            attempt_id = c.execute(
                "INSERT INTO attempts(session_id, route_id, result, created_at, client_id) VALUES (?,?,?,?,?)",
                (session["id"], route_id, result, _iso(_now()), client_id),
            ).lastrowid
            if motion:
                _set_motion(c, attempt_id, motion)
        n, sends = _attempt_stats(c, session["id"], route_id)
        route = c.execute("SELECT * FROM routes WHERE id=?", (route_id,)).fetchone() if route_id else None
        saved = c.execute("SELECT * FROM attempts WHERE id=?", (attempt_id,)).fetchone()
        return {
            "ok": True, "duplicate": dup, "fixed": fixed, "attempt_id": attempt_id, "result": result,
            "route_label": label(route["color"], route["grade"]) if route else None,
            "route_note": (route["note"] or "") if route else "",
            "count": n, "sends": sends, "session_started": session_new,
            "motion": {k: saved[k] for k in MOTION_FIELDS if saved[k] is not None},
        }


def _set_motion(c, attempt_id, motion):
    cols = [k for k in MOTION_FIELDS if k in motion]
    c.execute(f"UPDATE attempts SET {', '.join(k + '=?' for k in cols)} WHERE id=?",
              [motion[k] for k in cols] + [attempt_id])


def session_attempts(device_id):
    """本次训练的全部尝试（时间顺序），给复盘和 App 用"""
    with _lock, _db() as c:
        session, _ = _current_session(c, device_id, create=False, touch=False)
        if not session:
            return []
        rows = c.execute(
            "SELECT a.id, a.result, a.created_at, r.color, r.grade FROM attempts a LEFT JOIN routes r ON r.id=a.route_id "
            "WHERE a.session_id=? AND a.deleted=0 ORDER BY a.id",
            (session["id"],),
        ).fetchall()
        return [{"id": r["id"], "result": r["result"], "time": r["created_at"],
                 "route": label(r["color"], r["grade"]) if r["color"] else None} for r in rows]


def attempt_context(device_id, attempt_id):
    """一次按键的上下文，给「特别时刻」判断用（特别时刻规则在 panda/moments.py）
    返回 None 表示这条记录已不存在 / 不属于本次训练"""
    with _lock, _db() as c:
        session, _ = _current_session(c, device_id, create=False, touch=False)
        if not session:
            return None
        rows = c.execute(
            "SELECT id, route_id, result, created_at, fall_m, impact_g, climb_s, rest_s FROM attempts "
            "WHERE session_id=? AND deleted=0 ORDER BY id",
            (session["id"],),
        ).fetchall()
        idx = next((i for i, r in enumerate(rows) if r["id"] == attempt_id), None)
        if idx is None:
            return None
        me = rows[idx]
        route_id = me["route_id"]
        same = [r for r in rows[: idx + 1] if r["route_id"] == route_id]
        # 这条线从上次完攀之后连续失败了几次（含这一次）
        fail_streak = 0
        for r in reversed(same):
            if r["result"] != "fail":
                break
            fail_streak += 1
        # 连续「一次完攀」：从这次往前，每次都是完攀，而且都是那条线本次训练的第一次尝试
        first_on_route = {}
        for r in rows:
            first_on_route.setdefault(r["route_id"], r["id"])
        flash_streak = 0
        for r in reversed(rows[: idx + 1]):
            if r["result"] != "send" or r["route_id"] is None or first_on_route[r["route_id"]] != r["id"]:
                break
            flash_streak += 1
        route = c.execute("SELECT * FROM routes WHERE id=?", (route_id,)).fetchone() if route_id else None
        prev = {"attempts": 0, "sends": 0}
        if route_id:
            row = c.execute(
                "SELECT COUNT(*) n, SUM(result='send') s FROM attempts WHERE route_id=? AND session_id<>? AND deleted=0",
                (route_id, session["id"]),
            ).fetchone()
            prev = {"attempts": row["n"] or 0, "sends": row["s"] or 0}
        # 下落高度：这条线（失败时）之前最好的一次，不含这次；「高度比之前最好」
        prev_best_fall = None
        if route_id and me["fall_m"] is not None:
            b = c.execute("SELECT MAX(fall_m) FROM attempts WHERE route_id=? AND id<>? AND result='fail' "
                          "AND deleted=0 AND fall_m IS NOT NULL", (route_id, attempt_id)).fetchone()[0]
            prev_best_fall = b
        return {
            "attempt_id": attempt_id, "result": me["result"], "is_latest": idx == len(rows) - 1,
            "route_label": label(route["color"], route["grade"]) if route else None,
            "route_note": (route["note"] or "") if route else "",
            "count": len(same), "fail_streak": fail_streak, "flash_streak": flash_streak,
            "sends_today": sum(r["result"] == "send" for r in same),
            "prev_attempts": prev["attempts"], "prev_sends": prev["sends"],
            "session_attempts": len(rows), "session_id": session["id"],
            "fall_m": me["fall_m"], "impact_g": me["impact_g"], "prev_best_fall": prev_best_fall,
            "recent_rest": [effective_rest(rows, i) for i in range(max(0, idx - 2), idx + 1)],
            "condition": _condition(rows, session),
        }


def effective_rest(rows, i):
    """第 i 次尝试前歇了多久（秒），只用设备测的 rest_s。
    不用按键间隔估算：间隔里混着攀爬、穿鞋、看别人爬，估出来太不准，会误报「休息过短」"""
    return rows[i]["rest_s"]


# 状态判断（初始数值，实测再调）
TIRED_MIN_ATTEMPTS = 8


def _avg(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def _condition(rows, session):
    """看当天趋势判断是不是累了（02 功能清单「状态判断」）：
    下落高度变低、休息变长、失败变多，三项里中两项算累了。返回 {tired, signals[], advice, minutes}"""
    out = {"tired": False, "signals": [], "advice": None, "attempts": len(rows)}
    if not rows:
        return out
    minutes = (datetime.fromisoformat(rows[-1]["created_at"]) - datetime.fromisoformat(session["started_at"])).total_seconds() / 60
    out["minutes"] = int(minutes)
    if len(rows) < TIRED_MIN_ATTEMPTS:
        return out
    half = len(rows) // 2
    early, recent = rows[:half], rows[-4:]
    sig = []
    e_fall = _avg([r["fall_m"] for r in early if r["result"] == "fail"])
    r_fall = _avg([r["fall_m"] for r in recent if r["result"] == "fail"])
    if e_fall and r_fall is not None and e_fall >= 1.0 and r_fall <= e_fall * 0.75:
        sig.append(f"最近掉下来的位置变低了（平均{r_fall:.1f}米，前半段{e_fall:.1f}米）")
    e_rest = _avg([effective_rest(rows, i) for i in range(1, half)])
    r_rest = _avg([effective_rest(rows, i) for i in range(len(rows) - 4, len(rows))])
    if e_rest and r_rest and r_rest >= e_rest * 1.5 and r_rest - e_rest >= 60:
        sig.append(f"休息越来越长（最近平均{r_rest / 60:.1f}分钟，前半段{e_rest / 60:.1f}分钟）")
    e_rate = sum(r["result"] == "send" for r in early) / len(early)
    r_rate = sum(r["result"] == "send" for r in recent) / len(recent)
    if e_rate >= 0.3 and r_rate <= e_rate / 2:
        sig.append(f"最近{len(recent)}次完攀率{round(r_rate * 100)}%，前半段{round(e_rate * 100)}%")
    out["signals"] = sig
    out["tired"] = len(sig) >= 2
    if out["tired"]:
        # 一条线死磕就建议换线；练得久了或者三项都中就建议收工
        stuck = len({r["route_id"] for r in recent}) == 1 and all(r["result"] == "fail" for r in recent)
        out["advice"] = "收工" if (len(sig) >= 3 or minutes >= 90) else ("换线" if stuck else "歇一歇")
    return out


def session_condition(device_id):
    """当前训练的状态判断（给档案 / App 用）；没有进行中的训练返回 None"""
    with _lock, _db() as c:
        session, _ = _current_session(c, device_id, create=False, touch=False)
        if not session:
            return None
        rows = c.execute("SELECT id, route_id, result, created_at, fall_m, impact_g, climb_s, rest_s FROM attempts "
                         "WHERE session_id=? AND deleted=0 ORDER BY id", (session["id"],)).fetchall()
        return _condition(rows, session)


# ---------- 线路笔记 ----------
def find_route(c, device_id, session_id, color=None, grade=None):
    """找笔记要记到哪条线：说了颜色难度就找那条（最近爬过的），没说就用当前线路"""
    color_n, grade_n = normalize_color(color), normalize_grade(grade)
    if color_n and grade_n is not None:
        return c.execute(
            "SELECT r.* FROM routes r WHERE device_id=? AND color=? AND grade=? AND retired_at IS NULL "
            "ORDER BY COALESCE((SELECT MAX(started_at) FROM route_visits v WHERE v.route_id=r.id), r.created_at) DESC LIMIT 1",
            (device_id, color_n, grade_n)).fetchone()
    if session_id:
        cur = _current_visit(c, session_id)
        if cur:
            return c.execute("SELECT * FROM routes WHERE id=?", (cur["route_id"],)).fetchone()
    return None


def add_note(device_id, text, color=None, grade=None):
    """记一条线路笔记。返回 {ok, route_id, label, count(这条线共几条笔记), notes[全部原话]} 或 {ok:False, reason}"""
    text = (text or "").strip().strip("。，,. ")
    if not text:
        return {"ok": False, "reason": "empty"}
    with _lock, _db() as c:
        session, _ = _current_session(c, device_id, create=False)
        route = find_route(c, device_id, session["id"] if session else None, color, grade)
        if not route:
            return {"ok": False, "reason": "no_route"}
        c.execute("INSERT INTO route_notes(route_id, session_id, text, created_at) VALUES (?,?,?,?)",
                  (route["id"], session["id"] if session else None, text, _iso(_now())))
        notes = [r["text"] for r in c.execute(
            "SELECT text FROM route_notes WHERE route_id=? AND deleted=0 ORDER BY id", (route["id"],))]
        return {"ok": True, "route_id": route["id"], "label": label(route["color"], route["grade"]),
                "count": len(notes), "notes": notes, "text": text}


def route_notes(route_id):
    with _lock, _db() as c:
        return [dict(r) for r in c.execute(
            "SELECT id, text, created_at FROM route_notes WHERE route_id=? AND deleted=0 ORDER BY id", (route_id,))]


def set_guide(route_id, guide):
    with _lock, _db() as c:
        c.execute("UPDATE routes SET guide=?, guide_at=? WHERE id=?", (guide or "", _iso(_now()), route_id))


def delete_note(note_id):
    with _lock, _db() as c:
        return c.execute("UPDATE route_notes SET deleted=1 WHERE id=?", (note_id,)).rowcount > 0


# ---------- 安全事件 ----------
def safety_event(device_id, kind, impact_g=None, fall_m=None):
    """记一次安全事件，返回 {id, kind, week_hard_falls(含这次), route_label, route_note, week_hard_routes[]}"""
    with _lock, _db() as c:
        session, _ = _current_session(c, device_id, create=False)
        cur = _current_visit(c, session["id"]) if session else None
        eid = c.execute(
            "INSERT INTO safety_events(device_id, session_id, route_id, kind, impact_g, fall_m, created_at) VALUES (?,?,?,?,?,?,?)",
            (device_id, session["id"] if session else None, cur["route_id"] if cur else None, kind,
             impact_g, fall_m, _iso(_now()))).lastrowid
        week_ago = _iso(_now() - timedelta(days=7))
        hard = c.execute(
            "SELECT e.*, r.color, r.grade, r.note FROM safety_events e LEFT JOIN routes r ON r.id=e.route_id "
            "WHERE e.device_id=? AND e.kind='hard_fall' AND e.created_at>=? ORDER BY e.id", (device_id, week_ago)).fetchall()
        return {
            "id": eid, "kind": kind, "week_hard_falls": len(hard),
            "route_label": label(cur["color"], cur["grade"]) if cur else None,
            "route_note": (cur["note"] or "") if cur else "",
            "week_hard_routes": [(label(h["color"], h["grade"]) if h["color"] else "未知线路") +
                                 (f"（{h['note']}）" if h["note"] else "") for h in hard],
        }


def safety_summary(device_id, days=7):
    """最近几天的安全情况：重摔次数、摔后报警次数、休息过短提醒次数"""
    since = _iso(_now() - timedelta(days=days))
    with _lock, _db() as c:
        rows = c.execute("SELECT kind, COUNT(*) n FROM safety_events WHERE device_id=? AND created_at>=? GROUP BY kind",
                         (device_id, since)).fetchall()
        return {r["kind"]: r["n"] for r in rows}



# ---------- 当日复盘 ----------
def _session_stats(c, session):
    """一次训练的统计（数字全部由规则算出，大模型只负责解读和表达）"""
    rows = c.execute(
        "SELECT a.id, a.route_id, a.result, a.created_at, a.fall_m, a.impact_g, a.climb_s, a.rest_s, a.swings, "
        "r.color, r.grade, r.note "
        "FROM attempts a LEFT JOIN routes r ON r.id=a.route_id "
        "WHERE a.session_id=? AND a.deleted=0 ORDER BY a.id",
        (session["id"],),
    ).fetchall()
    routes, order = {}, []
    for r in rows:
        key = r["route_id"]
        if key not in routes:
            order.append(key)
            routes[key] = {"label": label(r["color"], r["grade"]) if r["color"] else "未知线路",
                           "grade": r["grade"], "note": r["note"] or "", "attempts": 0, "sends": 0,
                           "first_send_at": None, "first_ever_send": False, "best_fall": None}
        x = routes[key]
        x["attempts"] += 1
        if r["result"] == "send":
            x["sends"] += 1
            if x["first_send_at"] is None:
                x["first_send_at"] = x["attempts"]
        elif r["fall_m"] is not None:
            x["best_fall"] = max(x["best_fall"] or 0, r["fall_m"])
    # 以前训练都没完攀过、今天拿下的线
    for key, x in routes.items():
        if key and x["sends"]:
            prev = c.execute("SELECT COUNT(*) n, SUM(result='send') s FROM attempts WHERE route_id=? AND session_id<>? AND deleted=0",
                             (key, session["id"])).fetchone()
            x["prev_attempts"] = prev["n"] or 0
            x["first_ever_send"] = (prev["n"] or 0) > 0 and not (prev["s"] or 0)
    total = len(rows)
    sends = sum(r["result"] == "send" for r in rows)
    sent_routes = [x for x in routes.values() if x["sends"] and x["grade"] is not None]
    tried = [x for x in routes.values() if x["grade"] is not None]
    start = datetime.fromisoformat(session["started_at"])
    end = datetime.fromisoformat(rows[-1]["created_at"]) if rows else datetime.fromisoformat(session["last_active_at"])
    impacts = [r["impact_g"] for r in rows if r["impact_g"] is not None]
    rests = [effective_rest(rows, i) for i in range(1, len(rows))]
    rests = [x for x in rests if x is not None]
    hard_falls = c.execute("SELECT COUNT(*) FROM safety_events WHERE session_id=? AND kind='hard_fall'",
                           (session["id"],)).fetchone()[0]
    motion = None
    if impacts or any(r["fall_m"] is not None for r in rows):
        motion = {
            "measured": sum(1 for r in rows if r["fall_m"] is not None or r["impact_g"] is not None),
            "impact": {lv: sum(1 for g in impacts if impact_level(g) == lv) for lv in ("轻", "中", "重")},
            "hard_falls": max(hard_falls, sum(1 for g in impacts if g >= IMPACT_HARD)),
            "avg_rest_s": round(sum(rests) / len(rests)) if rests else None,
            "short_rests": sum(1 for x in rests if x < 60),
            "avg_climb_s": round(_avg([r["climb_s"] for r in rows]) or 0) or None,
            "swings": sum(r["swings"] or 0 for r in rows),
        }
    return {
        "date": start.strftime("%Y-%m-%d"),
        "started_at": session["started_at"],
        "minutes": max(1, int((end - start).total_seconds() // 60)) if rows else 0,
        "routes": [routes[k] for k in order],
        "route_count": len(routes),
        "attempts": total, "sends": sends,
        "send_rate": round(sends / total * 100) if total else 0,
        "routes_sent": len([x for x in routes.values() if x["sends"]]),
        "flashes": [x["label"] for x in routes.values() if x["first_send_at"] == 1],
        "hardest_send": max(sent_routes, key=lambda x: x["grade"])["label"] if sent_routes else None,
        "hardest_tried": max(tried, key=lambda x: x["grade"])["label"] if tried else None,
        "first_ever_sends": [x["label"] for x in routes.values() if x["first_ever_send"]],
        "longest_battle": max(routes.values(), key=lambda x: x["attempts"])["label"] if routes else None,
        "motion": motion,
        "condition": _condition(rows, session),
    }


def _usual(c, device_id, before_session_id, n=5):
    """「平时」= 之前最近 n 次有记录的训练的平均（03 文档：单次训练比平时）"""
    rows = c.execute(
        "SELECT s.id, COUNT(a.id) n, SUM(a.result='send') k FROM sessions s JOIN attempts a ON a.session_id=s.id AND a.deleted=0 "
        "WHERE s.device_id=? AND s.id<? GROUP BY s.id ORDER BY s.id DESC LIMIT ?",
        (device_id, before_session_id, n),
    ).fetchall()
    if not rows:
        return None
    return {"sessions": len(rows),
            "attempts": round(sum(r["n"] for r in rows) / len(rows), 1),
            "send_rate": round(sum((r["k"] or 0) / r["n"] for r in rows) / len(rows) * 100)}


def session_summary(device_id, end=False):
    """本次训练（或刚结束的那次）的复盘统计。end=True 时同时结束这次训练。没有训练返回 None"""
    with _lock, _db() as c:
        session, _ = _current_session(c, device_id, create=False, touch=False)
        if not session:
            session = c.execute("SELECT * FROM sessions WHERE device_id=? ORDER BY id DESC LIMIT 1", (device_id,)).fetchone()
            if not session:
                return None
        stats = _session_stats(c, session)
        stats["session_id"] = session["id"]
        stats["usual"] = _usual(c, device_id, session["id"])
        if end and not session["ended_at"]:
            c.execute("UPDATE sessions SET ended_at=? WHERE id=?", (_iso(_now()), session["id"]))
        return stats


def save_review(session_id, stats, spoken=None, full=None):
    import json as _json
    with _lock, _db() as c:
        c.execute("INSERT INTO reviews(session_id, stats, created_at) VALUES (?,?,?) "
                  "ON CONFLICT(session_id) DO UPDATE SET stats=excluded.stats",
                  (session_id, _json.dumps(stats, ensure_ascii=False), _iso(_now())))
        if spoken is not None:
            c.execute("UPDATE reviews SET spoken=? WHERE session_id=?", (spoken, session_id))
        if full is not None:
            c.execute("UPDATE reviews SET full=? WHERE session_id=?", (full, session_id))


def get_review(session_id):
    with _lock, _db() as c:
        r = c.execute("SELECT * FROM reviews WHERE session_id=?", (session_id,)).fetchone()
        return dict(r) if r else None



# ---------- 训练记忆（每次对话前交给大模型的档案） ----------
def _date_cn(iso):
    d = datetime.fromisoformat(iso)
    return f"{d.month}月{d.day}日"


def _rate(n, k):
    return f"{round((k or 0) / n * 100)}%" if n else "-"


def archive(device_id, now=None):
    """整理一份简短的训练档案（纯文字，几百字以内）。只读，不改任何数据。没有任何记录返回空串"""
    now = now or _now()
    with _lock, _db() as c:
        sessions = c.execute(
            "SELECT s.*, COUNT(a.id) n, SUM(a.result='send') k FROM sessions s "
            "LEFT JOIN attempts a ON a.session_id=s.id AND a.deleted=0 "
            "WHERE s.device_id=? GROUP BY s.id HAVING n>0 OR s.ended_at IS NULL ORDER BY s.id DESC LIMIT 12",
            (device_id,),
        ).fetchall()
        if not sessions:
            return ""
        cur = sessions[0]
        active = (cur["ended_at"] is None and
                  now - datetime.fromisoformat(cur["last_active_at"]) <= timedelta(hours=SESSION_IDLE_HOURS))
        lines = []

        # 今天（进行中的训练）
        current_route = None
        if active:
            visit = _current_visit(c, cur["id"])
            rows = c.execute(
                "SELECT r.id, r.color, r.grade, r.note, COUNT(a.id) n, SUM(a.result='send') k FROM attempts a "
                "LEFT JOIN routes r ON r.id=a.route_id WHERE a.session_id=? AND a.deleted=0 "
                "GROUP BY a.route_id ORDER BY MIN(a.id)", (cur["id"],)).fetchall()
            mins = int((now - datetime.fromisoformat(cur["started_at"])).total_seconds() // 60)
            head = f"【今天】训练进行中，已练约{mins}分钟"
            if visit:
                current_route = visit["route_id"]
                head += f"，现在在爬{label(visit['color'], visit['grade'])}{('（' + visit['note'] + '）') if visit['note'] else ''}"
            lines.append(head + "。")
            for r in rows:
                name = label(r["color"], r["grade"]) if r["color"] else "未知线路"
                seq = c.execute(
                    "SELECT result, fall_m FROM attempts WHERE session_id=? AND deleted=0 AND route_id IS ? ORDER BY id",
                    (cur["id"], r["id"])).fetchall()
                res = [x[0] for x in seq]
                first = res.index("send") + 1 if "send" in res else None
                how = ("一次完攀" if first == 1 else f"第{first}次完攀") if first else "还没完攀"
                falls = [x[1] for x in seq if x[0] == "fail" and x[1] is not None]
                extra = f"，掉下来的高度{'、'.join(f'{h:.1f}' for h in falls[-4:])}米" if falls else ""
                lines.append(f"- {name}：试{r['n']}次，{how}" + (f"（共完攀{r['k']}次）" if (r["k"] or 0) > 1 else "") + extra)
            att = c.execute("SELECT id, route_id, result, created_at, fall_m, impact_g, climb_s, rest_s FROM attempts "
                            "WHERE session_id=? AND deleted=0 ORDER BY id", (cur["id"],)).fetchall()
            cond = _condition(att, cur)
            if cond["tired"]:
                lines.append("【状态】看起来累了：" + "；".join(cond["signals"]) + f"。建议{cond['advice']}。")
            elif cond["signals"]:
                lines.append("【状态】" + "；".join(cond["signals"]) + "，还不算累。")
            rests = [effective_rest(att, i) for i in range(max(1, len(att) - 3), len(att))]
            if len(rests) >= 3 and all(x is not None and x < 60 for x in rests):
                lines.append("【休息】最近3次之间都歇了不到1分钟，手指和肩膀容易劳损。")
        else:
            lines.append("【今天】还没开始训练。")

        # 当前这条线以前的记录
        if current_route:
            hist = c.execute(
                "SELECT s.started_at, COUNT(a.id) n, SUM(a.result='send') k, MAX(CASE WHEN a.result='fail' THEN a.fall_m END) h "
                "FROM attempts a JOIN sessions s ON s.id=a.session_id "
                "WHERE a.route_id=? AND a.session_id<>? AND a.deleted=0 GROUP BY s.id ORDER BY s.id",
                (current_route, cur["id"])).fetchall()
            if hist:
                parts = [f"{_date_cn(h['started_at'])}试{h['n']}次{'完攀' if h['k'] else '没完攀'}"
                         + (f"，最高掉在{h['h']:.1f}米" if h["h"] is not None else "") for h in hist]
                lines.append("【这条线以前】" + "；".join(parts) + "。")
            else:
                lines.append("【这条线以前】没爬过，是新线。")
            r = c.execute("SELECT guide FROM routes WHERE id=?", (current_route,)).fetchone()
            notes = c.execute("SELECT text, created_at FROM route_notes WHERE route_id=? AND deleted=0 ORDER BY id DESC LIMIT 3",
                              (current_route,)).fetchall()
            if r["guide"] or notes:
                lines.append("【这条线的笔记】" + (f"攻略：{r['guide']}。" if r["guide"] else "") +
                             ("原话：" + "；".join(f"{_date_cn(n['created_at'])}记“{n['text']}”" for n in reversed(notes)) + "。"
                              if notes else ""))

        # 最近几次训练（不含进行中的这次）
        past = [s for s in sessions if not (active and s["id"] == cur["id"]) and s["n"]][:5]
        if past:
            lines.append("【最近训练】")
            for s in past:
                best = c.execute(
                    "SELECT r.color, r.grade FROM attempts a JOIN routes r ON r.id=a.route_id "
                    "WHERE a.session_id=? AND a.result='send' AND a.deleted=0 ORDER BY r.grade DESC LIMIT 1",
                    (s["id"],)).fetchone()
                n_routes = c.execute("SELECT COUNT(DISTINCT route_id) FROM attempts WHERE session_id=? AND deleted=0",
                                     (s["id"],)).fetchone()[0]
                lines.append(f"- {_date_cn(s['started_at'])}：{n_routes}条线，试{s['n']}次，完攀率{_rate(s['n'], s['k'])}"
                             f"{'，最难完攀' + label(best['color'], best['grade']) if best else '，没有完攀'}")

            # 卡住的线：以前试过、一直没完攀、还没下架
            stuck = c.execute(
                "SELECT r.color, r.grade, r.note, COUNT(a.id) n, COUNT(DISTINCT a.session_id) d FROM routes r "
                "JOIN attempts a ON a.route_id=r.id AND a.deleted=0 WHERE r.device_id=? AND r.retired_at IS NULL "
                "GROUP BY r.id HAVING SUM(a.result='send')=0 AND n>=3 ORDER BY MAX(a.id) DESC LIMIT 3",
                (device_id,)).fetchall()
            if stuck:
                lines.append("【还没拿下的线】" + "；".join(
                    f"{label(r['color'], r['grade'])}{('（' + r['note'] + '）') if r['note'] else ''}（{r['d']}次训练共试{r['n']}次）"
                    for r in stuck) + "。")

        # 个人最好
        best = c.execute(
            "SELECT r.color, r.grade, MIN(a.created_at) t FROM attempts a JOIN routes r ON r.id=a.route_id "
            "JOIN sessions s ON s.id=a.session_id WHERE s.device_id=? AND a.result='send' AND a.deleted=0 "
            "GROUP BY r.grade ORDER BY r.grade DESC LIMIT 1", (device_id,)).fetchone()
        total = c.execute("SELECT COUNT(*) FROM sessions s WHERE device_id=? AND EXISTS "
                          "(SELECT 1 FROM attempts a WHERE a.session_id=s.id AND a.deleted=0)", (device_id,)).fetchone()[0]
        tail = f"【累计】有记录的训练{total}次"
        if best:
            tail += f"；完攀过最难的是V{best['grade']}（第一次是{_date_cn(best['t'])}，{label(best['color'], best['grade'])}）"
        lines.append(tail + "。")
        week = _iso(now - timedelta(days=7))
        hard = c.execute(
            "SELECT r.color, r.grade, r.note FROM safety_events e LEFT JOIN routes r ON r.id=e.route_id "
            "WHERE e.device_id=? AND e.kind='hard_fall' AND e.created_at>=?", (device_id, week)).fetchall()
        if hard:
            where = "、".join(dict.fromkeys(
                (label(h["color"], h["grade"]) + (f"（{h['note']}）" if h["note"] else "")) if h["color"] else "未知线路"
                for h in hard))
            lines.append(f"【安全】最近7天重摔{len(hard)}次（{where}）。")
        return "\n".join(lines)


init_db()
