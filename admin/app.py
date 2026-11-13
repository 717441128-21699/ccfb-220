"""RPA 管理后台（Flask，端口 5051）。
五个页面：任务列表 / 单据预览 / 运行监控 / 异常处理 / 设置；
支持启停机器人、人工改单重跑、补正件重解析、维护公告与故障注入。
"""
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from flask import Flask, jsonify, redirect, render_template, request, send_file, url_for

from common.db import audit, dumps, get_conn, get_setting, loads, now_iso, set_setting
from common.paths import (ADMIN_PORT, LOGS, REISSUE, ROOT, SCREENSHOTS)
from parser_service.ingest import reissue_task

app = Flask(__name__, template_folder="templates")
app.secret_key = "rpa-admin-local-only"
app.jinja_env.filters["fromjson"] = lambda s: loads(s, [])

WORKER_PROCS = {}       # worker_id -> subprocess.Popen（仅本进程内存）
DEFAULT_SETTINGS = {
    "session_timeout_sec": "180",
    "maintenance_announcement": "0",
    "maintenance_hard": "0",
    "maintenance_title": "系统维护公告",
    "maintenance_body": "今晚 22:00-23:00 系统例行维护，请合理安排申报时间。",
    "headless": "1",
}


def _ensure_settings():
    conn = get_conn()
    for k, v in DEFAULT_SETTINGS.items():
        if get_setting(conn, k) is None:
            set_setting(conn, k, v)
    conn.close()


# ---------------- 页面 ----------------
@app.route("/")
def page_tasks():
    q = request.args.get("status", "")
    conn = get_conn()
    sql = ("SELECT t.*, p.stage, p.item_index, p.sim_ref, r.result AS recon "
           "FROM tasks t LEFT JOIN task_progress p ON p.task_id=t.id "
           "LEFT JOIN reconcile_reports r ON r.task_id=t.id")
    args = []
    if q:
        sql += " WHERE t.status=?"
        args.append(q)
    sql += " ORDER BY t.id"
    rows = conn.execute(sql, args).fetchall()
    stats = {r["status"]: r["c"] for r in
             conn.execute("SELECT status, COUNT(*) c FROM tasks GROUP BY status")}
    conn.close()
    return render_template("tasks.html", rows=rows, q=q, stats=stats)


@app.route("/preview/<int:task_id>")
def page_preview(task_id):
    conn = get_conn()
    t = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    d = conn.execute("SELECT * FROM declarations WHERE task_id=?", (task_id,)).fetchone()
    shots = conn.execute(
        "SELECT * FROM screenshots WHERE task_id=? ORDER BY id", (task_id,)).fetchall()
    rep = conn.execute(
        "SELECT * FROM reconcile_reports WHERE task_id=?", (task_id,)).fetchone()
    prog = conn.execute(
        "SELECT * FROM task_progress WHERE task_id=?", (task_id,)).fetchone()
    conn.close()
    decl = {"header": loads(d["header_json"], {}), "items": loads(d["items_json"], [])} if d else None
    diffs = loads(rep["diffs_json"], []) if rep else []
    return render_template("preview.html", t=t, decl=decl, shots=shots,
                           diffs=diffs, prog=prog)


@app.route("/monitor")
def page_monitor():
    conn = get_conn()
    workers = conn.execute("SELECT * FROM workers ORDER BY id").fetchall()
    running = conn.execute(
        "SELECT t.id,t.ref_no,t.assigned_worker,p.stage,p.item_index FROM tasks t "
        "JOIN task_progress p ON p.task_id=t.id WHERE t.status='running'").fetchall()
    logs = conn.execute(
        "SELECT * FROM audit_log ORDER BY id DESC LIMIT 60").fetchall()
    conn.close()
    # 进程字典没记录时（worker 由演示编排脚本启动），用心跳新鲜度兜底判断存活
    live = {}
    for w in workers:
        if w["id"] in WORKER_PROCS:
            live[w["id"]] = WORKER_PROCS[w["id"]].poll() is None
        elif w["status"] in ("running", "idle") and w["heartbeat"]:
            age = (datetime.now() - datetime.fromisoformat(w["heartbeat"])).total_seconds()
            live[w["id"]] = age < 60
        else:
            live[w["id"]] = False
    return render_template("monitor.html", workers=workers, running=running,
                           logs=logs, live=live)


@app.route("/exceptions")
def page_exceptions():
    conn = get_conn()
    rows = conn.execute(
        "SELECT e.*, t.ref_no FROM exceptions e JOIN tasks t ON t.id=e.task_id "
        "ORDER BY e.status ASC, e.id DESC").fetchall()
    conn.close()
    return render_template("exceptions.html", rows=rows)


@app.route("/settings", methods=["GET", "POST"])
def page_settings():
    _ensure_settings()
    conn = get_conn()
    if request.method == "POST":
        for k in ("session_timeout_sec", "maintenance_announcement", "maintenance_hard",
                  "maintenance_title", "maintenance_body", "headless"):
            if k in request.form:
                set_setting(conn, k, request.form.get(k) or ("1" if k.endswith(("announcement", "hard", "headless")) else ""))
        audit(conn, "operator", "SETTINGS_UPDATE", request.path)
        conn.close()
        return redirect(url_for("page_settings"))
    accounts = conn.execute("SELECT * FROM accounts ORDER BY id").fetchall()
    s = {k: get_setting(conn, k, DEFAULT_SETTINGS.get(k, "")) for k in DEFAULT_SETTINGS}
    conn.close()
    return render_template("settings.html", s=s, accounts=accounts)


# ---------------- 操作 API ----------------
@app.route("/shot/<int:shot_id>")
def shot_file(shot_id):
    conn = get_conn()
    row = conn.execute("SELECT path FROM screenshots WHERE id=?", (shot_id,)).fetchone()
    conn.close()
    if not row or not Path(row["path"]).exists():
        return "not found", 404
    return send_file(row["path"], mimetype="image/png")


@app.route("/api/worker/start", methods=["POST"])
def api_worker_start():
    account = (request.form.get("account") or "").strip()
    conn = get_conn()
    row = conn.execute("SELECT * FROM accounts WHERE username=? AND enabled=1",
                       (account,)).fetchone()
    headless = get_setting(conn, "headless", "1") == "1"
    conn.close()
    if not row:
        return jsonify({"ok": False, "error": "账号不存在或停用"}), 400
    wid = f"worker-{account}"
    proc = WORKER_PROCS.get(wid)
    if proc and proc.poll() is None:
        return jsonify({"ok": False, "error": "该机器人已在运行"})
    logf = open(LOGS / f"{wid}.log", "a", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "run_worker.py"),
                 "--worker-id", wid, "--account", account,
                 "--password", row["password"],
                 *(["--headless"] if headless else ["--no-headless"])],
        cwd=str(ROOT), stdout=logf, stderr=subprocess.STDOUT,
    )
    WORKER_PROCS[wid] = proc
    audit(get_conn(), "operator", "WORKER_START", f"{wid} pid={proc.pid}")
    return jsonify({"ok": True, "pid": proc.pid})


@app.route("/api/worker/stop", methods=["POST"])
def api_worker_stop():
    wid = request.form.get("worker_id")
    proc = WORKER_PROCS.get(wid)
    if proc and proc.poll() is None:
        proc.terminate()
        audit(get_conn(), "operator", "WORKER_STOP", wid)
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "进程不在运行"})


@app.route("/api/task/requeue", methods=["POST"])
def api_requeue():
    tid = request.form.get("task_id", type=int)
    conn = get_conn()
    conn.execute(
        "UPDATE tasks SET status='queued', assigned_worker=NULL, lock_token=NULL, "
        "locked_at=NULL, updated_at=? WHERE id=? AND status IN ('failed','mismatch','running')",
        (now_iso(), tid))
    conn.execute(
        "UPDATE exceptions SET status='resolved', resolution='人工重新入队',resolved_at=? "
        "WHERE task_id=? AND status='open'", (now_iso(), tid))
    audit(conn, "operator", "TASK_REQUEUE", str(tid))
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/task/edit", methods=["POST"])
def api_edit():
    """人工改单：保存修改后的统一结构，重新校验，通过则重新入队。"""
    tid = request.form.get("task_id", type=int)
    decl = json.loads(request.form.get("decl_json"))
    from common.validation import validate
    errs = validate(decl)
    conn = get_conn()
    if errs:
        conn.execute("UPDATE tasks SET validation_errors=?, updated_at=? WHERE id=?",
                     (dumps(errs), now_iso(), tid))
        conn.close()
        return jsonify({"ok": False, "errors": errs})
    conn.execute(
        "UPDATE declarations SET header_json=?, items_json=?, updated_at=? WHERE task_id=?",
        (dumps(decl["header"]), dumps(decl["items"]), now_iso(), tid))
    conn.execute(
        "UPDATE tasks SET status='queued', validation_errors='[]', assigned_worker=NULL, "
        "lock_token=NULL, updated_at=? WHERE id=?", (now_iso(), tid))
    conn.execute(
        "UPDATE exceptions SET status='resolved', resolution='人工改单后重新校验通过',"
        "resolved_at=? WHERE task_id=? AND status='open'", (now_iso(), tid))
    audit(conn, "operator", "TASK_EDIT_REQUEUE", str(tid))
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/task/reissue", methods=["POST"])
def api_reissue():
    """采用 reissue/ 下客户补正件重新解析。"""
    tid = request.form.get("task_id", type=int)
    conn = get_conn()
    t = conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
    conn.close()
    ref = t["ref_no"]
    xlsx = REISSUE / f"{ref}_reissue.xlsx"
    pdf = REISSUE / f"{ref}_reissue.pdf"
    if not xlsx.exists() or not pdf.exists():
        return jsonify({"ok": False, "error": f"补正件不存在：{xlsx.name}"}), 404
    res = reissue_task(tid, str(xlsx), str(pdf))
    return jsonify({"ok": res["valid"], "errors": res["errors"]})


@app.route("/api/fault/inject", methods=["POST"])
def api_fault():
    """故障注入：会话过期 / 软公告 / 硬维护，用于演练机器人自恢复。"""
    kind = request.form.get("kind")
    conn = get_conn()
    if kind == "kill_sessions":
        conn.execute("DELETE FROM sim_sessions")
        msg = "全部网站会话已强制过期"
    elif kind == "announce":
        set_setting(conn, "maintenance_announcement", "1")
        set_setting(conn, "maintenance_hard", "0")
        msg = "已弹出维护公告（软提示，不阻断）"
    elif kind == "hard_maintenance":
        set_setting(conn, "maintenance_announcement", "1")
        set_setting(conn, "maintenance_hard", "1")
        msg = "已进入硬维护（503，机器人将等待恢复）"
    elif kind == "clear_maintenance":
        set_setting(conn, "maintenance_announcement", "0")
        set_setting(conn, "maintenance_hard", "0")
        msg = "维护结束，网站恢复"
    else:
        conn.close()
        return jsonify({"ok": False, "error": "unknown fault"}), 400
    audit(conn, "operator", "FAULT_INJECT", msg)
    conn.close()
    return jsonify({"ok": True, "message": msg})


if __name__ == "__main__":
    _ensure_settings()
    app.run(host="127.0.0.1", port=ADMIN_PORT, debug=False, use_reloader=False)
