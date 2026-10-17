"""
攀搭数据存储（SQLite，文件：data/panda.db）

表结构
  sessions      一次训练（开机后第一次登记线路开始；闲置超过 SESSION_IDLE_HOURS 小时算新的一次）
  routes        一条线路（颜色 + 难度 + 描述），跨训练长期存在，所以能认出“上周那条”
  route_visits  某次训练里切换到某条线路的记录；每次训练里最后一条 = 当前线路
  attempts      每次尝试（按键记录）：成功 send / 失败 fail；还没登记线路时 route_id 为空（未知线路）
后续功能（笔记、复盘）在这里加表。
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
            CREATE INDEX IF NOT EXISTS idx_routes_dev ON routes(device_id, color, grade);
            CREATE INDEX IF NOT EXISTS idx_visits_session ON route_visits(session_id);
            """
        )


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
def _current_session(c, device_id, create):
    row = c.execute(
        "SELECT * FROM sessions WHERE device_id=? AND ended_at IS NULL ORDER BY id DESC LIMIT 1",
        (device_id,),
    ).fetchone()
    now = _now()
    if row and now - datetime.fromisoformat(row["last_active_at"]) > timedelta(hours=SESSION_IDLE_HOURS):
        c.execute("UPDATE sessions SET ended_at=? WHERE id=?", (row["last_active_at"], row["id"]))
        row = None
    if row:
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
        session, _ = _current_session(c, device_id, create=False)
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


def record_attempt(device_id, result, client_id=None, fix=False):
    """记录一次尝试（按键）。result: 'send' 成功 / 'fail' 失败
    fix=True：设备 3 秒内改按，同一个 client_id 的结果改成新的 result（重复发送也只改不加）
    返回 {ok, duplicate, fixed, attempt_id, result, route_label|None, count, sends, session_started}"""
    result = {"success": "send", "ok": "send", "top": "send"}.get(result, result)
    if result not in ("send", "fail"):
        return {"ok": False, "reason": f"未知结果 {result}"}
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
        else:
            # 新记录；改按但原记录没到（比如原记录丢了）也直接按新结果记一次
            cur = _current_visit(c, session["id"])
            route_id = cur["route_id"] if cur else None
            attempt_id = c.execute(
                "INSERT INTO attempts(session_id, route_id, result, created_at, client_id) VALUES (?,?,?,?,?)",
                (session["id"], route_id, result, _iso(_now()), client_id),
            ).lastrowid
        n, sends = _attempt_stats(c, session["id"], route_id)
        route = c.execute("SELECT * FROM routes WHERE id=?", (route_id,)).fetchone() if route_id else None
        return {
            "ok": True, "duplicate": dup, "fixed": fixed, "attempt_id": attempt_id, "result": result,
            "route_label": label(route["color"], route["grade"]) if route else None,
            "route_note": (route["note"] or "") if route else "",
            "count": n, "sends": sends, "session_started": session_new,
        }


def session_attempts(device_id):
    """本次训练的全部尝试（时间顺序），给复盘和 App 用"""
    with _lock, _db() as c:
        session, _ = _current_session(c, device_id, create=False)
        if not session:
            return []
        rows = c.execute(
            "SELECT a.id, a.result, a.created_at, r.color, r.grade FROM attempts a LEFT JOIN routes r ON r.id=a.route_id "
            "WHERE a.session_id=? AND a.deleted=0 ORDER BY a.id",
            (session["id"],),
        ).fetchall()
        return [{"id": r["id"], "result": r["result"], "time": r["created_at"],
                 "route": label(r["color"], r["grade"]) if r["color"] else None} for r in rows]


init_db()
