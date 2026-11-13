"""本地仿真「单一窗口」申报网站。
仅监听 127.0.0.1，纯本地模拟，不连接任何真实系统。
特性：登录 + 图形验证码、服务端会话超时、表体分页录入、
字段联动服务端校验、维护公告弹窗、提交幂等、申报数据回查。
"""
import json
import secrets
import sys
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from flask import (Flask, flash, jsonify, redirect, render_template,
                   request, session, url_for)

from common.captcha import issue, verify
from common.db import audit, get_conn, get_setting, now_iso
from common.validation import validate

PAGE_SIZE = 5

app = Flask(__name__, template_folder="templates")
app.secret_key = "sim-single-window-local-only"


# ---------------- 会话管理 ----------------
def _timeout_sec():
    return int(get_setting(get_conn(), "session_timeout_sec", 180) or 180)


def _create_session(account):
    sid = secrets.token_hex(16)
    conn = get_conn()
    conn.execute(
        "INSERT INTO sim_sessions(sid,account,created_at,last_seen) VALUES(?,?,?,?)",
        (sid, account, now_iso(), now_iso()),
    )
    audit(conn, account, "SIM_LOGIN", "仿真网站登录")
    conn.close()
    session["sid"] = sid
    session["account"] = account


def _destroy_session():
    sid = session.pop("sid", None)
    if sid:
        conn = get_conn()
        conn.execute("DELETE FROM sim_sessions WHERE sid=?", (sid,))
        conn.execute("DELETE FROM sim_captcha WHERE sid=?", (sid,))
        conn.close()
    session.pop("account", None)


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        sid = session.get("sid")
        if not sid:
            if request.path.startswith("/api/"):
                return jsonify({"ok": False, "error": "unauthorized"}), 401
            return redirect(url_for("login", reason="timeout"))
        conn = get_conn()
        row = conn.execute("SELECT last_seen FROM sim_sessions WHERE sid=?", (sid,)).fetchone()
        if not row:
            conn.close()
            _destroy_session()
            return redirect(url_for("login", reason="timeout"))
        last = datetime.fromisoformat(row["last_seen"])
        if datetime.now() - last > timedelta(seconds=_timeout_sec()):
            conn.execute("DELETE FROM sim_sessions WHERE sid=?", (sid,))
            conn.close()
            _destroy_session()
            if request.path.startswith("/api/"):
                return jsonify({"ok": False, "error": "session_expired"}), 401
            return redirect(url_for("login", reason="timeout"))
        conn.execute("UPDATE sim_sessions SET last_seen=? WHERE sid=?", (now_iso(), sid))
        conn.close()
        return fn(*args, **kwargs)
    return wrapper


@app.before_request
def _maintenance_guard():
    # 硬维护：除静态资源/登录/公告查询外一律 503
    if request.path.startswith("/static") or request.path in ("/login", "/captcha.png",
                                                              "/health", "/api/announcement"):
        return None
    conn = get_conn()
    hard = get_setting(conn, "maintenance_hard", "0")
    conn.close()
    if hard == "1" and not request.path.startswith("/static"):
        if request.path.startswith("/api/"):
            return jsonify({"ok": False, "error": "maintenance"}), 503
        return render_template("maintenance.html"), 503


@app.after_request
def _no_cache(resp):
    resp.headers["Cache-Control"] = "no-store"
    return resp


# ---------------- 认证 ----------------
@app.route("/health")
def health():
    return jsonify({"ok": True, "service": "sim-single-window", "ts": now_iso()})


@app.route("/captcha.png")
def captcha_png():
    sid = session.get("sid") or secrets.token_hex(16)
    if "sid" not in session:
        session["sid"] = sid  # 预登录临时 sid，登录成功后复用
    from flask import Response
    return Response(issue(sid), mimetype="image/png")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return render_template("login.html", reason=request.args.get("reason"))
    username = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""
    cap = request.form.get("captcha") or ""
    sid = session.get("sid", "")
    if not verify(sid, cap):
        flash("图形验证码错误或已过期，请重新输入", "error")
        return render_template("login.html"), 400
    conn = get_conn()
    row = conn.execute("SELECT * FROM accounts WHERE username=? AND enabled=1", (username,)).fetchone()
    conn.close()
    if not row or row["password"] != password:
        flash("用户名或密码错误", "error")
        return render_template("login.html"), 401
    _create_session(username)
    return redirect(url_for("home"))


@app.route("/logout")
def logout():
    _destroy_session()
    return redirect(url_for("login"))


# ---------------- 业务页面 ----------------
@app.route("/")
@login_required
def home():
    return render_template("home.html", account=session.get("account"))


def _load_draft(draft_id):
    conn = get_conn()
    row = conn.execute("SELECT * FROM sim_drafts WHERE id=? AND account=?",
                       (draft_id, session["account"])).fetchone()
    conn.close()
    if not row:
        return None
    return {
        "id": row["id"], "ref_no": row["ref_no"],
        "header": json.loads(row["header_json"]),
        "items": json.loads(row["items_json"]),
    }


def _upsert_draft(draft_id, ref_no, header, items):
    conn = get_conn()
    if draft_id:
        row = conn.execute("SELECT id FROM sim_drafts WHERE id=? AND account=?",
                           (draft_id, session["account"])).fetchone()
    if draft_id and row:
        conn.execute(
            "UPDATE sim_drafts SET ref_no=?, header_json=?, items_json=?, saved_at=? WHERE id=?",
            (ref_no, json.dumps(header, ensure_ascii=False),
             json.dumps(items, ensure_ascii=False), now_iso(), draft_id),
        )
    else:
        cur = conn.execute(
            "INSERT INTO sim_drafts(account,ref_no,header_json,items_json,saved_at) VALUES(?,?,?,?,?)",
            (session["account"], ref_no, json.dumps(header, ensure_ascii=False),
             json.dumps(items, ensure_ascii=False), now_iso()),
        )
        draft_id = cur.lastrowid
    conn.close()
    return draft_id


@app.route("/declaration/new", methods=["GET"])
@login_required
def decl_new():
    draft_id = request.args.get("draft_id", type=int)
    page = max(0, request.args.get("page", 0, type=int))
    draft = _load_draft(draft_id) if draft_id else None
    header = (draft or {}).get("header", {})
    items = (draft or {}).get("items", [])
    total_pages = max(1, (len(items) + PAGE_SIZE - 1) // PAGE_SIZE)
    return render_template("form.html", header=header, items=items, draft_id=draft_id,
                           page=page, total_pages=total_pages, page_size=PAGE_SIZE,
                           account=session["account"])


@app.route("/declaration/save-page", methods=["POST"])
@login_required
def save_page():
    f = request.form
    draft_id = f.get("draft_id", type=int)
    page = f.get("page", 0, type=int)
    total_pages = f.get("total_pages", 1, type=int)
    action = f.get("action", "next")

    header = {k: f.get(k, "") for k in (
        "ref_no", "trade_mode", "levy_type", "contract_no", "consignee_name",
        "consignee_credit_code", "consignor_name", "consignor_country",
        "import_port", "export_port", "currency", "total_qty", "total_amount",
        "freight", "insurance", "pack_type", "trans_mode", "decl_date")}
    old = _load_draft(draft_id) if draft_id else None
    items = (old or {}).get("items", [])

    rows = int(f.get("rows_on_page", 0))
    for i in range(rows):
        idx = page * PAGE_SIZE + i
        it = {k: f.get(f"item_{k}_{i}", "") for k in (
            "line_no", "hs_code", "goods_name", "spec", "qty", "unit",
            "legal_qty", "currency", "unit_price", "total_price",
            "origin_country", "dest_country")}
        while len(items) <= idx:
            items.append({})
        items[idx] = it
    # 删掉尾部空行
    while items and not any(str(v).strip() for v in items[-1].values()):
        items.pop()

    draft_id = _upsert_draft(draft_id, header.get("ref_no", ""), header, items)
    next_page = page + 1 if action == "next" else max(0, page - 1)
    if action == "addpage":
        next_page = page + 1
        total_pages = max(total_pages, next_page + 1)
    next_page = max(0, min(next_page, total_pages - 1))  # 末页点"下一页"钳制在末页
    return redirect(url_for("decl_new", draft_id=draft_id, page=next_page))


@app.route("/declaration/submit", methods=["POST"])
@login_required
def decl_submit():
    draft_id = request.form.get("draft_id", type=int)
    draft = _load_draft(draft_id) if draft_id else None
    if not draft:
        flash("草稿不存在或已过期", "error")
        return redirect(url_for("decl_new"))
    header, items = draft["header"], draft["items"]

    # 服务端联动校验（与机器人侧同一套规则）
    typed = _coerce(header, items)
    errs = validate(typed)
    if errs:
        return render_template("submit_error.html", errs=errs, draft_id=draft_id,
                               account=session["account"]), 422

    conn = get_conn()
    # 幂等：同一 ref_no 重复提交直接返回原回执，杜绝重复申报
    exist = conn.execute("SELECT sim_ref FROM sim_declarations WHERE ref_no=?",
                         (header.get("ref_no"),)).fetchone()
    if exist:
        audit(conn, session["account"], "SIM_SUBMIT_DUP",
              f"{header.get('ref_no')} -> {exist['sim_ref']}")
        conn.close()
        return render_template("receipt.html", sim_ref=exist["sim_ref"],
                               duplicate=True, ref_no=header.get("ref_no"),
                               account=session["account"])
    seq = conn.execute("SELECT COUNT(*) c FROM sim_declarations").fetchone()["c"] + 1
    sim_ref = f"SIM{datetime.now():%Y%m%d%H%M%S}{seq:04d}"
    conn.execute(
        "INSERT INTO sim_declarations(sim_ref,ref_no,account,header_json,items_json,submitted_at)"
        " VALUES(?,?,?,?,?,?)",
        (sim_ref, header.get("ref_no"), session["account"],
         json.dumps(typed["header"], ensure_ascii=False),
         json.dumps(typed["items"], ensure_ascii=False), now_iso()),
    )
    conn.execute("DELETE FROM sim_drafts WHERE id=?", (draft_id,))
    audit(conn, session["account"], "SIM_SUBMIT_OK",
          f"{header.get('ref_no')} -> {sim_ref}")
    conn.close()
    return render_template("receipt.html", sim_ref=sim_ref, duplicate=False,
                           ref_no=header.get("ref_no"), account=session["account"])


def _coerce(header, items):
    h = dict(header)
    for f in ("total_qty", "total_amount", "freight", "insurance"):
        try:
            h[f] = float(h[f]) if str(h.get(f, "")).strip() else None
        except (TypeError, ValueError):
            pass
    its = []
    for it in items:
        x = dict(it)
        for f in ("qty", "legal_qty", "unit_price", "total_price"):
            try:
                x[f] = float(x[f]) if str(x.get(f, "")).strip() else None
            except (TypeError, ValueError):
                pass
        try:
            x["line_no"] = int(x["line_no"])
        except (TypeError, ValueError):
            pass
        its.append(x)
    return {"header": h, "items": its}


@app.route("/declaration/list")
@login_required
def decl_list():
    conn = get_conn()
    rows = conn.execute(
        "SELECT sim_ref,ref_no,account,submitted_at FROM sim_declarations "
        "ORDER BY rowid DESC LIMIT 200"
    ).fetchall()
    conn.close()
    return render_template("list.html", rows=rows, account=session["account"])


@app.route("/declaration/detail/<sim_ref>")
@login_required
def decl_detail(sim_ref):
    conn = get_conn()
    row = conn.execute("SELECT * FROM sim_declarations WHERE sim_ref=?", (sim_ref,)).fetchone()
    conn.close()
    if not row:
        return "not found", 404
    data = {"header": json.loads(row["header_json"]), "items": json.loads(row["items_json"])}
    return render_template("detail.html", d=data, sim_ref=sim_ref,
                           submitted_at=row["submitted_at"], account=session["account"])


@app.route("/api/declaration/<sim_ref>")
@login_required
def api_decl(sim_ref):
    """回查接口：返回网站端已申报数据（机器人核对用）。"""
    conn = get_conn()
    row = conn.execute("SELECT * FROM sim_declarations WHERE sim_ref=?", (sim_ref,)).fetchone()
    conn.close()
    if not row:
        return jsonify({"ok": False, "error": "not_found"}), 404
    return jsonify({
        "ok": True, "sim_ref": sim_ref, "ref_no": row["ref_no"],
        "header": json.loads(row["header_json"]),
        "items": json.loads(row["items_json"]),
    })


@app.route("/api/announcement")
@login_required
def api_announcement():
    conn = get_conn()
    active = get_setting(conn, "maintenance_announcement", "0") == "1"
    hard = get_setting(conn, "maintenance_hard", "0") == "1"
    title = get_setting(conn, "maintenance_title", "系统维护公告")
    body = get_setting(conn, "maintenance_body", "今晚 22:00-23:00 系统例行维护，请合理安排申报时间。")
    conn.close()
    return jsonify({"active": active, "hard": hard, "title": title, "body": body})


if __name__ == "__main__":
    from common.paths import SIM_HOST, SIM_PORT
    app.run(host=SIM_HOST, port=SIM_PORT, debug=False, use_reloader=False)
