"""初始化：建库、灌代码表、建机器人账号。"""
from common.codes import seed_codes
from common.db import audit, get_conn, get_setting, set_setting

ACCOUNTS = [
    ("rpa01", "Rpa@2026a"),
    ("rpa02", "Rpa@2026b"),
    ("rpa03", "Rpa@2026c"),
]


def bootstrap(reset: bool = False):
    if reset:
        from common.paths import DB_PATH
        for suffix in ("", "-wal", "-shm"):
            p = DB_PATH.with_name(DB_PATH.name + suffix)
            if p.exists():
                p.unlink()
    seed_codes()
    conn = get_conn()
    for u, pw in ACCOUNTS:
        conn.execute(
            "INSERT OR IGNORE INTO accounts(username,password,enabled) VALUES(?,?,1)", (u, pw))
    if get_setting(conn, "session_timeout_sec") is None:
        set_setting(conn, "session_timeout_sec", "180")
    audit(conn, "system", "BOOTSTRAP", "数据库/代码表/账号初始化完成")
    conn.close()
    print("bootstrap done:", len(ACCOUNTS), "accounts ready")


if __name__ == "__main__":
    import sys
    bootstrap(reset="--reset" in sys.argv)
