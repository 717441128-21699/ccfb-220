"""每步截图留痕。"""
from common.db import get_conn, now_iso
from common.paths import SCREENSHOTS


def shot(page, task_id: int, step: str):
    path = SCREENSHOTS / f"task{task_id:04d}_{step}.png"
    try:
        page.screenshot(path=str(path), full_page=False)
    except Exception:
        return None
    conn = get_conn()
    conn.execute(
        "INSERT INTO screenshots(task_id,step,path,created_at) VALUES(?,?,?,?)",
        (task_id, step, str(path), now_iso()),
    )
    conn.close()
    return str(path)
