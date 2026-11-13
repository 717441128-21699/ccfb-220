"""机器人进程入口（管理后台用 subprocess 调起，也可手动运行）。"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from rpa.worker import run_worker


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--worker-id", required=True)
    ap.add_argument("--account", required=True)
    ap.add_argument("--password", required=True)
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--no-headless", dest="headless", action="store_false")
    ap.add_argument("--idle-exit", type=int, default=120)
    ap.set_defaults(headless=True)
    args = ap.parse_args()
    run_worker(args.worker_id, args.account, args.password,
               headless=args.headless, idle_exit=args.idle_exit)


if __name__ == "__main__":
    main()
