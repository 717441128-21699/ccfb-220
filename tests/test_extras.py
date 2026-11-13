"""补充验收：会话超时、提交幂等（重复申报拦截）、管理后台页面与 API。"""
import json

from common.db import get_conn, set_setting
from common.paths import SIM_BASE
from parser_service.ingest import load_declaration
from rpa.browser import SimBrowser
from rpa.lock import claim_task


def test_session_timeout():
    conn = get_conn()
    acc = conn.execute("SELECT * FROM accounts WHERE username='rpa01'").fetchone()
    set_setting(conn, "session_timeout_sec", "2")
    conn.close()
    b = SimBrowser("t-timeout", acc["username"], acc["password"], headless=True)
    b.start()
    try:
        assert b.login(0), "登录失败"
        assert b.is_logged_in(), "登录后应在线"
        import time
        time.sleep(3)  # 超过 2 秒会话超时
        assert not b.is_logged_in(), "超时后应掉线"
        assert b.login(0), "重登应成功"
        print("session timeout + relogin OK")
    finally:
        b.close()
        conn = get_conn()
        set_setting(conn, "session_timeout_sec", "180")
        conn.close()


def test_submit_idempotent():
    """同一业务编号提交两次：网站端只允许存在一条申报。"""
    conn = get_conn()
    acc = conn.execute("SELECT * FROM accounts WHERE username='rpa02'").fetchone()
    # 取一张已申报单的统一结构作为重复提交素材
    row = conn.execute(
        "SELECT t.id FROM tasks t JOIN sim_declarations s ON s.ref_no=t.ref_no LIMIT 1").fetchone()
    ref = conn.execute("SELECT ref_no FROM tasks WHERE id=?", (row["id"],)).fetchone()["ref_no"]
    before = conn.execute("SELECT COUNT(*) c FROM sim_declarations WHERE ref_no=?", (ref,)).fetchone()["c"]
    conn.close()
    decl = load_declaration(row["id"])
    b = SimBrowser("t-idem", acc["username"], acc["password"], headless=True)
    b.start()
    try:
        assert b.login(0)
        # 手工走一遍：新建草稿 -> 填两页 -> 提交（该 ref 已存在，应幂等返回原回执）
        n_pages = max(1, (len(decl["items"]) + 4) // 5)
        draft_id = None
        for pno in range(n_pages):
            assert b.open_draft(0, draft_id, pno)
            page = b.page
            for k, v in decl["header"].items():
                sel = f"input[name={k}]"
                if page.locator(sel).count():
                    page.fill(sel, "" if v is None else str(v))
            start = pno * 5
            rows = min(5, len(decl["items"]) - start)
            for i in range(rows):
                for k, v in decl["items"][start + i].items():
                    sel = f"input[name=item_{k}_{i}]"
                    if page.locator(sel).count():
                        page.fill(sel, "" if v is None else str(v))
            page.click("button[name=action][value=next]")
            page.wait_for_load_state("domcontentloaded")
            if "draft_id=" in page.url:
                draft_id = int(page.url.split("draft_id=")[-1].split("&")[0])
        assert b.open_draft(0, draft_id, n_pages - 1)
        page = b.page
        page.on("dialog", lambda d: d.accept())
        page.click("form[action$='/declaration/submit'] button[type=submit]")
        page.wait_for_load_state("domcontentloaded")
        content = page.content()
        assert "该单已申报（幂等返回原回执）" in content, "重复提交应被幂等拦截"
        conn = get_conn()
        after = conn.execute("SELECT COUNT(*) c FROM sim_declarations WHERE ref_no=?", (ref,)).fetchone()["c"]
        dup_logs = conn.execute(
            "SELECT COUNT(*) c FROM audit_log WHERE action='SIM_SUBMIT_DUP'").fetchone()["c"]
        conn.close()
        assert before == after == 1, f"申报条数不应增加：{before} -> {after}"
        assert dup_logs >= 1
        print("idempotent submit OK (no duplicate declaration)")
    finally:
        b.close()


def test_admin_pages_and_apis():
    from admin.app import app
    client = app.test_client()
    for path in ("/", "/monitor", "/exceptions", "/settings"):
        r = client.get(path)
        assert r.status_code == 200, f"{path} -> {r.status_code}"
    # 预览页（取第一张任务）
    conn = get_conn()
    tid = conn.execute("SELECT id FROM tasks ORDER BY id LIMIT 1").fetchone()["id"]
    conn.close()
    assert client.get(f"/preview/{tid}").status_code == 200
    # 故障注入 API
    r = client.post("/api/fault/inject", data={"kind": "announce"})
    assert r.get_json()["ok"]
    r = client.post("/api/fault/inject", data={"kind": "clear_maintenance"})
    assert r.get_json()["ok"]
    # 非法账号启动机器人应被拒
    r = client.post("/api/worker/start", data={"account": "nobody"})
    assert r.status_code == 400
    # 人工改单：传入结构错误的单据应返回校验错误，不改状态
    bad = {"header": {}, "items": []}
    r = client.post("/api/task/edit", data={"task_id": tid, "decl_json": json.dumps(bad)})
    assert r.status_code == 200 and r.get_json()["ok"] is False and r.get_json()["errors"]
    print("admin pages + APIs OK")


def test_report_file():
    report = json.loads(open("data/reports/final_report.json", encoding="utf-8").read())
    s = report["summary"]
    assert s["total"] == 100 and s["verified"] == 100
    assert s["total_diffs"] == 0
    assert s["declared_rows"] == 100 and s["distinct_refs"] == 100
    assert len(report["tasks"]) == 100
    print("final report OK:", s)


if __name__ == "__main__":
    test_session_timeout()
    test_submit_idempotent()
    test_admin_pages_and_apis()
    test_report_file()
    print("ALL EXTRA CHECKS PASSED")
