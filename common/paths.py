"""全局路径与常量。"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DB_PATH = DATA / "rpa.db"
INBOX = DATA / "inbox"            # 客户来件（Excel + PDF）
REISSUE = DATA / "reissue"        # 客户补正资料
SCREENSHOTS = DATA / "screenshots"
REPORTS = DATA / "reports"
LOGS = DATA / "logs"

SIM_HOST = "127.0.0.1"
SIM_PORT = 5050
SIM_BASE = f"http://{SIM_HOST}:{SIM_PORT}"
ADMIN_PORT = 5051

for p in (DATA, INBOX, REISSUE, SCREENSHOTS, REPORTS, LOGS):
    p.mkdir(parents=True, exist_ok=True)
