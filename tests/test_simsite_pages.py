"""仿真网站页面渲染检查：登录后访问工作台/列表/详情/录入页。"""
from common.db import get_conn
from rpa.browser import SimBrowser

conn = get_conn()
acc = conn.execute("SELECT * FROM accounts WHERE username='rpa01'").fetchone()
sim_ref = conn.execute("SELECT sim_ref FROM sim_declarations LIMIT 1").fetchone()["sim_ref"]
conn.close()

b = SimBrowser("t-pages", acc["username"], acc["password"], headless=True)
b.start()
try:
    assert b.login(0)
    for path, marker in [
        ("/", "工作台"),
        ("/declaration/list", "已申报数据"),
        (f"/declaration/detail/{sim_ref}", "申报详情"),
        ("/declaration/new", "报关单录入"),
    ]:
        b.page.goto("http://127.0.0.1:5050" + path, wait_until="domcontentloaded")
        assert marker in b.page.content(), f"{path} 缺少 {marker}"
        print(path, "OK")
    print("SIMSITE PAGES OK")
finally:
    b.close()
