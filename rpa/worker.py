"""录入机器人主循环。
职责：抢任务 -> 登录(验证码OCR) -> 分页录入表体(断点续录) -> 提交(幂等)
      -> 回查逐字段核对 -> 心跳/截图/异常工单。
"""
import json
import math
import time

from common.db import (audit, dumps, get_conn, loads, now_iso)
from rpa.browser import SimBrowser
from rpa.lock import already_declared, claim_task, release_lock, requeue
from rpa.reconcile import reconcile
from rpa.screenshots import shot

PAGE_SIZE = 5
MAX_RUNS = 3


def _heartbeat(worker_id, name, account, status, current_task=None, info=""):
    conn = get_conn()
    conn.execute(
        "INSERT INTO workers(id,name,account,status,heartbeat,current_task,info) "
        "VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET status=excluded.status,"
        "heartbeat=excluded.heartbeat,current_task=excluded.current_task,info=excluded.info",
        (worker_id, name, account, status, now_iso(), current_task, info[:500]),
    )
    conn.close()


def _progress(task_id, **kw):
    conn = get_conn()
    row = conn.execute("SELECT * FROM task_progress WHERE task_id=?", (task_id,)).fetchone()
    stage = kw.get("stage", row["stage"] if row else "claimed")
    item_index = kw.get("item_index", row["item_index"] if row else 0)
    draft_id = kw.get("draft_id", row["draft_id"] if row else None)
    sim_ref = kw.get("sim_ref", row["sim_ref"] if row else None)
    conn.execute(
        "INSERT INTO task_progress(task_id,stage,item_index,draft_id,sim_ref,updated_at) "
        "VALUES(?,?,?,?,?,?) ON CONFLICT(task_id) DO UPDATE SET stage=excluded.stage,"
        "item_index=excluded.item_index,draft_id=excluded.draft_id,sim_ref=excluded.sim_ref,"
        "updated_at=excluded.updated_at",
        (task_id, stage, item_index, draft_id, sim_ref, now_iso()),
    )
    conn.close()


def _open_with_recovery(browser: SimBrowser, task_id, draft_id, page_no):
    """打开草稿页，掉线则重登后重试，硬维护则等待（最多覆盖约 30 秒维护窗口）。"""
    for attempt in range(15):
        if not browser.ensure_session(task_id):
            time.sleep(1)
            continue
        ok = browser.open_draft(task_id, draft_id, page_no)
        if not ok:
            continue
        state = browser.handle_post_action(task_id, f"open_p{page_no}_try{attempt}")
        if state == "ok":
            return True
        if state in ("relogin", "maintenance"):
            time.sleep(2)
            continue
    return False


def _fill_and_save_page(browser: SimBrowser, task_id, decl, page_no, total_pages):
    page = browser.page
    items = decl["items"]
    start = page_no * PAGE_SIZE
    rows = min(PAGE_SIZE, len(items) - start)
    # 表头每页都在，统一回填
    for k, v in decl["header"].items():
        sel = f"input[name={k}]"
        if page.locator(sel).count():
            page.fill(sel, "" if v is None else str(v))
    for i in range(rows):
        it = items[start + i]
        for k, v in it.items():
            sel = f"input[name=item_{k}_{i}]"
            if page.locator(sel).count():
                page.fill(sel, "" if v is None else str(v))
    shot(page, task_id, f"filled_p{page_no}")
    is_last = page_no >= total_pages - 1
    if is_last:
        page.click("button[name=action][value=next]")  # 保存本页
    else:
        page.click("button[name=action][value=next]")  # 下一页（自动保存）
    page.wait_for_load_state("domcontentloaded")
    state = browser.handle_post_action(task_id, f"saved_p{page_no}")
    if state != "ok":
        return state, None
    # 从跳转后的 URL 取回 draft_id
    did = page.url.split("draft_id=")[-1].split("&")[0] if "draft_id=" in page.url else None
    return "ok", int(did) if did and did.isdigit() else None


def run_task(browser: SimBrowser, worker_id: str, task_id: int) -> str:
    conn = get_conn()
    trow = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    drow = conn.execute("SELECT * FROM declarations WHERE task_id=?", (task_id,)).fetchone()
    prow = conn.execute("SELECT * FROM task_progress WHERE task_id=?", (task_id,)).fetchone()
    conn.close()
    decl = {"header": loads(drow["header_json"], {}), "items": loads(drow["items_json"], [])}
    ref = decl["header"]["ref_no"]
    n_pages = max(1, math.ceil(len(decl["items"]) / PAGE_SIZE))

    # 幂等：网站端已有该单（上次提交成功但回查前崩溃）-> 直接回查
    existing = already_declared(ref)
    if existing:
        _progress(task_id, stage="submit", sim_ref=existing)
        if not browser.ensure_session(task_id):
            return "retry"
        res = reconcile(browser.page, task_id, existing, decl)
        return "verified" if res["result"] == "match" else "mismatch"

    draft_id = prow["draft_id"] if prow else None
    start_page = 0
    if draft_id:
        # 断点续录：已保存到第几页，从下一页继续
        start_page = min((prow["item_index"] or 0) // PAGE_SIZE, n_pages - 1)
    _progress(task_id, stage="header", draft_id=draft_id)
    _heartbeat(worker_id, worker_id, browser.account, "running", task_id, f"录入 {ref}")

    if not _open_with_recovery(browser, task_id, draft_id, start_page):
        return "retry"

    page_no = start_page
    while page_no < n_pages:
        state, new_draft = _fill_and_save_page(browser, task_id, decl, page_no, n_pages)
        if state == "relogin":
            if not browser.login(task_id):
                return "retry"
            continue  # 本页重来（草稿已保存则从下页开始）
        if state == "maintenance":
            time.sleep(3)
            continue
        if state != "ok":
            return "retry"
        if new_draft:
            draft_id = new_draft
        _progress(task_id, stage="items", item_index=(page_no + 1) * PAGE_SIZE,
                  draft_id=draft_id)
        page_no += 1
        if page_no < n_pages and not _open_with_recovery(browser, task_id, draft_id, page_no):
            return "retry"

    # 提交
    _progress(task_id, stage="submit", draft_id=draft_id)
    for attempt in range(4):
        if not _open_with_recovery(browser, task_id, draft_id, n_pages - 1):
            return "retry"
        page = browser.page
        shot(page, task_id, "before_submit")
        page.on("dialog", lambda d: d.accept())
        page.click("form[action$='/declaration/submit'] button[type=submit]")
        page.wait_for_load_state("domcontentloaded")
        state = browser.handle_post_action(task_id, f"submit_try{attempt}")
        if state == "relogin":
            browser.login(task_id)
            continue
        if state == "maintenance":
            time.sleep(3)
            continue
        # 成功页或校验错误页
        content = page.content()
        if "申报回执号" in content:
            import re
            m = re.search(r"SIM\d+", content)
            sim_ref = m.group(0) if m else None
            _progress(task_id, stage="verify", sim_ref=sim_ref, draft_id=draft_id)
            shot(page, task_id, "receipt")
            res = reconcile(page, task_id, sim_ref, decl)
            return "verified" if res["result"] == "match" else "mismatch"
        if "服务端字段联动校验未通过" in content:
            errs = page.locator("table tbody tr").all_inner_texts()
            conn = get_conn()
            conn.execute(
                "INSERT INTO exceptions(task_id,kind,message,status,created_at) "
                "VALUES(?,?,?,'open',?)",
                (task_id, "submit_rejected",
                 "网站端校验驳回：" + " | ".join(errs)[:1500], now_iso()),
            )
            conn.close()
            return "failed"
        time.sleep(1)
    return "retry"


def run_worker(worker_id: str, account: str, password: str, headless: bool = True,
               idle_exit: int = 120):
    """机器人进程入口：循环抢任务直到连续 idle_exit 秒无任务。"""
    browser = SimBrowser(worker_id, account, password, headless=headless)
    browser.start()
    idle_since = None
    try:
        while True:
            task_id = claim_task(worker_id, account)
            if task_id is None:
                _heartbeat(worker_id, worker_id, account, "idle", info="等待任务")
                if idle_since is None:
                    idle_since = time.time()
                elif time.time() - idle_since > idle_exit:
                    break
                time.sleep(2)
                continue
            idle_since = None
            try:
                outcome = run_task(browser, worker_id, task_id)
            except Exception as e:  # 浏览器崩溃等未预期异常
                audit(get_conn(), worker_id, "WORKER_EXC", f"task {task_id}: {e!r}")
                outcome = "retry"
            _finalize(task_id, outcome, worker_id)
    finally:
        _heartbeat(worker_id, worker_id, account, "stopped", info="进程退出")
        browser.close()


def _finalize(task_id: int, outcome: str, worker_id: str):
    conn = get_conn()
    row = conn.execute("SELECT run_count FROM tasks WHERE id=?", (task_id,)).fetchone()
    runs = row["run_count"] if row else 1
    if outcome == "verified":
        conn.execute(
            "UPDATE tasks SET status='verified', updated_at=? WHERE id=?",
            (now_iso(), task_id),
        )
        conn.execute(
            "UPDATE task_progress SET stage='done', updated_at=? WHERE task_id=?",
            (now_iso(), task_id),
        )
        conn.execute(
            "UPDATE exceptions SET status='resolved', resolution='核对通过自动关闭',"
            "resolved_at=? WHERE task_id=? AND status='open'", (now_iso(), task_id),
        )
        audit(conn, worker_id, "TASK_DONE", f"task {task_id} 录入并核对零差异")
    elif outcome == "mismatch":
        release_lock(task_id, "mismatch")
        conn.execute(
            "INSERT INTO exceptions(task_id,kind,message,status,created_at) "
            "VALUES(?,?,?,'open',?)",
            (task_id, "mismatch", "回查发现字段差异，详见核对报告", now_iso()),
        )
    elif outcome == "failed":
        release_lock(task_id, "failed")
    else:  # retry：回队保留断点；超过最大次数转 failed 等人处理
        if runs >= MAX_RUNS:
            release_lock(task_id, "failed")
            conn.execute(
                "INSERT INTO exceptions(task_id,kind,message,status,created_at) "
                "VALUES(?,?,?,'open',?)",
                (task_id, "retry_exhausted",
                 f"连续 {MAX_RUNS} 次运行未完成，转人工", now_iso()),
            )
        else:
            requeue(task_id, f"{worker_id} 运行中断（掉线/维护/浏览器异常），断点保留待重跑")
    conn.close()
