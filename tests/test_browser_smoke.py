"""浏览器冒烟测试：真实 Playwright 登录仿真网站（OCR 过验证码）、打开录入页、检查公告。"""
from common.db import get_conn, set_setting
from rpa.browser import SimBrowser


def main():
    conn = get_conn()
    acc = conn.execute("SELECT * FROM accounts WHERE username='rpa01'").fetchone()
    conn.close()
    b = SimBrowser("smoke", acc["username"], acc["password"], headless=True)
    b.start()
    try:
        ok = b.login(task_id=0)
        assert ok, "登录失败（验证码 OCR 或账号问题）"
        print("login ok")
        # 打开空白录入页（表体分页）
        ok = b.open_draft(0, None, 0)
        assert ok, "打开录入页失败"
        assert "报关单录入" in b.page.content()
        shot_path = rpa_shot(b.page)
        print("form page ok, screenshot:", shot_path)
        # 软公告
        conn = get_conn()
        set_setting(conn, "maintenance_announcement", "1")
        conn.close()
        b.page.goto("http://127.0.0.1:5050/", wait_until="domcontentloaded")
        b._dismiss_announcement()
        print("announcement handled")
        conn = get_conn()
        set_setting(conn, "maintenance_announcement", "0")
        conn.close()
    finally:
        b.close()
    print("BROWSER SMOKE OK")


def rpa_shot(page):
    from rpa.screenshots import shot
    return shot(page, 0, "smoke_form")


if __name__ == "__main__":
    main()
