"""Playwright 浏览器控制：登录、图形验证码 OCR、会话掉线检测、
维护公告确认、硬维护窗口等待、页面导航。
"""
import time

from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

from common.captcha import solve
from common.db import get_conn, get_setting
from common.paths import SIM_BASE
from rpa.screenshots import shot


class SimBrowser:
    def __init__(self, worker_id: str, account: str, password: str, headless: bool = True):
        self.worker_id = worker_id
        self.account = account
        self.password = password
        self.headless = headless
        self._pw = None
        self.browser = None
        self.page = None

    def start(self):
        self._pw = sync_playwright().start()
        self.browser = self._pw.chromium.launch(headless=self.headless)
        ctx = self.browser.new_context(viewport={"width": 1366, "height": 900},
                                       ignore_https_errors=True)
        self.page = ctx.new_page()
        self.page.set_default_timeout(15000)

    def close(self):
        try:
            if self.browser:
                self.browser.close()
        finally:
            if self._pw:
                self._pw.stop()

    # ---------- 登录 ----------
    def login(self, task_id: int = 0, max_retry: int = 6) -> bool:
        page = self.page
        for attempt in range(1, max_retry + 1):
            page.goto(f"{SIM_BASE}/login", wait_until="domcontentloaded")
            self._wait_maintenance(task_id)
            page.fill("input[name=username]", self.account)
            page.fill("input[name=password]", self.password)
            # 下载验证码图片字节 -> 本地 OCR（不读取网站答案）
            cap_el = page.locator("#capImg")
            cap_bytes = cap_el.screenshot()
            answer = solve(cap_bytes)
            page.fill("input[name=captcha]", answer)
            shot(page, task_id, f"login_attempt{attempt}")
            page.click("button[type=submit]")
            page.wait_for_load_state("domcontentloaded")
            url = page.url
            if "/login" not in url:
                shot(page, task_id, "login_ok")
                self._dismiss_announcement()
                return True
            # 验证码错误/账号错误：刷新再来
            time.sleep(0.4)
        return False

    # ---------- 会话状态 ----------
    def is_logged_in(self) -> bool:
        try:
            self.page.goto(f"{SIM_BASE}/", wait_until="domcontentloaded", timeout=10000)
        except PWTimeout:
            return False
        if "/login" in self.page.url:
            return False
        return "/login" not in self.page.url

    def ensure_session(self, task_id: int) -> bool:
        """任何操作前调用：掉线/会话过期则重登（断点在服务端草稿里，不丢）。"""
        if self.is_logged_in():
            self._dismiss_announcement()
            return True
        return self.login(task_id)

    def open_draft(self, task_id: int, draft_id, page_no: int = 0):
        url = f"{SIM_BASE}/declaration/new?page={page_no}"
        if draft_id:
            url += f"&draft_id={draft_id}"
        self.page.goto(url, wait_until="domcontentloaded")
        if "/login" in self.page.url:
            return False
        self._dismiss_announcement()
        return True

    # ---------- 公告 / 维护 ----------
    def _dismiss_announcement(self):
        try:
            mask = self.page.locator("#annMask")
            if mask.is_visible(timeout=1200):
                mask.locator("button").click(timeout=2000)
        except Exception:
            pass

    def _wait_maintenance(self, task_id: int):
        """硬维护期间页面为 503，轮询等待恢复。"""
        for _ in range(60):
            conn = get_conn()
            hard = get_setting(conn, "maintenance_hard", "0")
            conn.close()
            if hard != "1":
                return
            time.sleep(2)
        raise RuntimeError("维护窗口等待超时")

    def handle_post_action(self, task_id: int, step: str) -> str:
        """动作后统一判断页面状态。返回 ok / relogin / maintenance / error。"""
        page = self.page
        try:
            page.wait_for_load_state("domcontentloaded", timeout=10000)
        except PWTimeout:
            pass
        shot(page, task_id, step)
        if "/login" in page.url:
            return "relogin"
        if "系统维护中" in page.content():
            return "maintenance"
        return "ok"
