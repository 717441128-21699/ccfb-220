"""单任务端到端：机器人录完一张队列任务，验证提交成功且回查零差异。"""
import json

from common.db import get_conn
from rpa.browser import SimBrowser
from rpa.lock import claim_task
from rpa.worker import _finalize, run_task


def main():
    conn = get_conn()
    acc = conn.execute("SELECT * FROM accounts WHERE username='rpa01'").fetchone()
    conn.close()
    b = SimBrowser("e2e-one", acc["username"], acc["password"], headless=True)
    b.start()
    try:
        task_id = claim_task("e2e-one", acc["username"])
        assert task_id, "队列中没有任务"
        print("claimed task:", task_id)
        outcome = run_task(b, "e2e-one", task_id)
        print("outcome:", outcome)
        _finalize(task_id, outcome, "e2e-one")
        conn = get_conn()
        t = conn.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
        rep = conn.execute("SELECT * FROM reconcile_reports WHERE task_id=?", (task_id,)).fetchone()
        shots = conn.execute("SELECT COUNT(*) c FROM screenshots WHERE task_id=?", (task_id,)).fetchone()["c"]
        conn.close()
        print("task status:", t["status"], "| reconcile:", rep["result"],
              "| diffs:", len(json.loads(rep["diffs_json"])), "| screenshots:", shots)
        assert t["status"] == "verified"
        assert rep["result"] == "match" and json.loads(rep["diffs_json"]) == []
        print("SINGLE TASK E2E OK")
    finally:
        b.close()


if __name__ == "__main__":
    main()
