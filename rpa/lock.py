"""任务抢锁：SQLite 原子 UPDATE 保证同一单不会被两个机器人抢到。"""
import secrets

from common.db import get_conn, now_iso


def claim_task(worker_id: str, account: str):
    """原子领取一个排队任务。成功返回 task_id，无任务返回 None。"""
    token = secrets.token_hex(12)
    conn = get_conn()
    row = conn.execute(
        "SELECT id FROM tasks WHERE status='queued' ORDER BY id LIMIT 1"
    ).fetchone()
    if not row:
        conn.close()
        return None
    cur = conn.execute(
        "UPDATE tasks SET status='running', assigned_worker=?, lock_token=?, "
        "locked_at=?, run_count=run_count+1, updated_at=? "
        "WHERE id=? AND status='queued'",
        (worker_id, token, now_iso(), now_iso(), row["id"]),
    )
    if cur.rowcount == 0:  # 被并发 worker 抢先
        conn.close()
        return None
    conn.execute(
        "INSERT OR REPLACE INTO task_progress(task_id,stage,item_index,draft_id,sim_ref,updated_at)"
        " VALUES(?,?,0,NULL,NULL,?)",
        (row["id"], "claimed", now_iso()),
    )
    conn.close()
    return row["id"]


def release_lock(task_id: int, status: str):
    conn = get_conn()
    conn.execute(
        "UPDATE tasks SET status=?, lock_token=NULL, locked_at=NULL, updated_at=? WHERE id=?",
        (status, now_iso(), task_id),
    )
    conn.close()


def requeue(task_id: int, reason: str):
    """掉线/异常后回队，保留断点进度，等待重跑。"""
    conn = get_conn()
    conn.execute(
        "UPDATE tasks SET status='queued', assigned_worker=NULL, lock_token=NULL, "
        "locked_at=NULL, updated_at=? WHERE id=?",
        (now_iso(), task_id),
    )
    conn.execute(
        "INSERT INTO exceptions(task_id,kind,message,status,created_at) VALUES(?,?,?,'open',?)",
        (task_id, "runtime", reason, now_iso()),
    )
    conn.close()


def already_declared(ref_no: str):
    """提交前幂等检查：网站端是否已存在该业务编号的申报。"""
    conn = get_conn()
    row = conn.execute(
        "SELECT sim_ref FROM sim_declarations WHERE ref_no=?", (ref_no,)
    ).fetchone()
    conn.close()
    return row["sim_ref"] if row else None
