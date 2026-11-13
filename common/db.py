"""SQLite 连接与初始化（WAL，供多进程并发抢锁）。"""
import json
import sqlite3
from datetime import datetime

from common.paths import DB_PATH, ROOT

SCHEMA = ROOT / "common" / "schema.sql"


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def get_conn(db_path=None) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path or DB_PATH), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    conn = get_conn()
    conn.executescript(SCHEMA.read_text(encoding="utf-8"))
    conn.close()


def audit(conn, actor: str, action: str, detail: str = ""):
    conn.execute(
        "INSERT INTO audit_log(ts, actor, action, detail) VALUES(?,?,?,?)",
        (now_iso(), actor, action, detail[:1800]),
    )


def get_setting(conn, key, default=None):
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn, key, value):
    conn.execute(
        "INSERT INTO settings(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )


def dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


def loads(text, default):
    if not text:
        return default
    try:
        return json.loads(text)
    except Exception:
        return default
