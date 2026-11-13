"""一键端到端演示编排。
流程：初始化 -> 起仿真网站/管理后台 -> 生成100单(含20脏单) -> 解析入库(脏单拦截)
     -> 3账号并发录入(中途注入会话过期+硬维护) -> 客户补正 -> 脏单重跑
     -> 回查核对 -> 断言 100 单全部完成且差异为 0。
"""
import json
import subprocess
import sys
import time
from pathlib import Path

import requests

from admin.app import DEFAULT_SETTINGS
from bootstrap import bootstrap
from common.db import get_conn, get_setting, set_setting
from common.paths import (DB_PATH, INBOX, LOGS, REISSUE, REPORTS, ROOT,
                          SIM_BASE, SIM_PORT, ADMIN_PORT)
from datagen.generator import generate
from parser_service.ingest import ingest_file, reissue_task

PY = sys.executable
ACCOUNTS = [("rpa01", "Rpa@2026a"), ("rpa02", "Rpa@2026b"), ("rpa03", "Rpa@2026c")]


def log(msg):
    print(f"[demo {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def wait_http(url, timeout=30):
    ddl = time.time() + timeout
    while time.time() < ddl:
        try:
            if requests.get(url, timeout=3).status_code in (200, 401, 404, 302):
                return True
        except requests.RequestException:
            time.sleep(0.5)
    raise RuntimeError(f"service not ready: {url}")


def start_server(script, name):
    logf = open(LOGS / f"{name}.log", "a", encoding="utf-8")
    p = subprocess.Popen([PY, str(ROOT / script)], cwd=str(ROOT),
                         stdout=logf, stderr=subprocess.STDOUT)
    return p


def count_status():
    conn = get_conn()
    rows = {r["status"]: r["c"] for r in
            conn.execute("SELECT status, COUNT(*) c FROM tasks GROUP BY status")}
    conn.close()
    return rows


def wait_while(cond_fn, timeout, tick=2):
    ddl = time.time() + timeout
    while time.time() < ddl:
        if not cond_fn():
            return True
        time.sleep(tick)
    return not cond_fn()


def main():
    t0 = time.time()
    log("1/8 初始化数据库（reset）")
    bootstrap(reset=True)

    log("2/8 启动仿真申报网站与管理后台")
    sim = start_server("simsite/app.py", "simsite")
    admin = start_server("admin/app.py", "admin")
    wait_http(f"{SIM_BASE}/health")
    wait_http(f"http://127.0.0.1:{ADMIN_PORT}/")
    log(f"   仿真网站 {SIM_BASE} ｜ 管理后台 http://127.0.0.1:{ADMIN_PORT}")

    try:
        log("3/8 生成 100 单测试单据（80 干净 + 20 脏数据）")
        manifest = generate(100, 20)

        log("4/8 解析 Excel+PDF 并做双源交叉核对/联动校验")
        n_invalid = 0
        for rec in manifest:
            r = ingest_file(rec["excel"], rec["pdf"], defect_type=rec.get("defect_type", ""))
            if not r["valid"]:
                n_invalid += 1
        st = count_status()
        log(f"   解析完成：拦截脏单 {n_invalid}（invalid={st.get('invalid',0)}，queued={st.get('queued',0)}）")
        assert n_invalid == 20, "脏单拦截数量应为 20"

        log("5/8 启动 3 个机器人账号并发录入（同一单 SQLite 抢锁）")
        workers = []
        for i, (acc, pw) in enumerate(ACCOUNTS):
            wid = f"worker-{acc}"
            logf = open(LOGS / f"{wid}.log", "a", encoding="utf-8")
            p = subprocess.Popen(
                [PY, str(ROOT / "run_worker.py"), "--worker-id", wid,
                 "--account", acc, "--password", pw, "--headless", "--idle-exit", "600"],
                cwd=str(ROOT), stdout=logf, stderr=subprocess.STDOUT)
            workers.append((wid, p))
            time.sleep(1.5)  # 错开启动，便于观察抢锁

        # 故障注入窗口 1：跑起来约 12 秒后强制全部会话过期
        time.sleep(12)
        log("   [故障注入] 强制全部网站会话过期 -> 机器人应重登并断点续录")
        requests.post(f"http://127.0.0.1:{ADMIN_PORT}/api/fault/inject",
                      data={"kind": "kill_sessions"}, timeout=10)

        # 故障注入窗口 2：再过 15 秒开 20 秒硬维护
        time.sleep(15)
        log("   [故障注入] 开启硬维护(503) 20 秒 -> 机器人应等待恢复且不丢单")
        requests.post(f"http://127.0.0.1:{ADMIN_PORT}/api/fault/inject",
                      data={"kind": "hard_maintenance"}, timeout=10)
        time.sleep(20)
        requests.post(f"http://127.0.0.1:{ADMIN_PORT}/api/fault/inject",
                      data={"kind": "clear_maintenance"}, timeout=10)
        log("   [故障注入] 维护结束，网站恢复")

        log("6/8 等待 80 张干净单录入+回查完成")
        ok = wait_while(lambda: count_status().get("verified", 0) < 80, timeout=720, tick=3)
        st = count_status()
        log(f"   阶段状态：{st}")
        assert ok, f"干净单未在时限内完成：{st}"
        assert st.get("running", 0) == 0 and st.get("queued", 0) == 0

        log("7/8 对 20 张脏单采用客户补正件重新解析并入队")
        for rec in manifest:
            if not rec["dirty"]:
                continue
            conn = get_conn()
            row = conn.execute("SELECT id FROM tasks WHERE ref_no=?", (rec["ref_no"],)).fetchone()
            conn.close()
            r = reissue_task(row["id"], rec["reissue_excel"], rec["reissue_pdf"])
            if not r["valid"]:
                raise RuntimeError(f"补正件仍校验失败 {rec['ref_no']}: {r['errors']}")
        log("   补正单全部重新入队，等待机器人续录（worker 空闲等待中会自动抢新任务）")
        ok = wait_while(lambda: count_status().get("verified", 0) < 100, timeout=600, tick=3)
        st = count_status()
        log(f"   最终任务状态：{st}")
        assert ok, f"补正单未在时限内完成：{st}"

        log("8/8 汇总核对结果并生成报告")
        report = build_report(manifest)
        out = REPORTS / "final_report.json"
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report["summary"], ensure_ascii=False, indent=2))

        # 硬断言：100 单跑完，核对差异为 0
        assert report["summary"]["total"] == 100
        assert report["summary"]["verified"] == 100
        assert report["summary"]["total_diffs"] == 0
        assert report["summary"]["declared_rows"] == 100  # 网站端恰好 100 条，无重复申报
        log(f"✅ 全部断言通过：100/100 完成，差异 0，无重复申报，用时 {time.time()-t0:.0f}s")
        log(f"报告：{out}")
        if "--exit-when-done" in sys.argv:
            return report
        log("服务保持运行中，可打开管理后台查看；按 Ctrl+C 结束。")
        # 保持服务运行，供人工查看后台
        while True:
            time.sleep(60)
    except Exception:
        log("演示失败，保留服务与日志以便排查")
        raise
    finally:
        if "--exit-when-done" in sys.argv:
            for p in [w[1] for w in workers] + [admin, sim]:
                try:
                    p.terminate()
                except Exception:
                    pass
            log("已清理全部子进程")


def build_report(manifest):
    conn = get_conn()
    tasks = conn.execute("SELECT * FROM tasks ORDER BY id").fetchall()
    reps = conn.execute("SELECT * FROM reconcile_reports").fetchall()
    declared = conn.execute("SELECT COUNT(*) c, COUNT(DISTINCT ref_no) d FROM sim_declarations").fetchone()
    shots = conn.execute("SELECT COUNT(*) c FROM screenshots").fetchone()["c"]
    excs = conn.execute(
        "SELECT kind, COUNT(*) c FROM exceptions GROUP BY kind").fetchall()
    conn.close()
    items, total_diffs = [], 0
    rep_map = {r["task_id"]: r for r in reps}
    for t in tasks:
        r = rep_map.get(t["id"])
        diffs = json.loads(r["diffs_json"]) if r else []
        total_diffs += len(diffs)
        items.append({
            "task_id": t["id"], "ref_no": t["ref_no"], "status": t["status"],
            "defect_type": t["defect_type"] or "",
            "reconcile": r["result"] if r else None,
            "diffs": diffs, "run_count": t["run_count"],
        })
    return {
        "summary": {
            "total": len(tasks),
            "verified": sum(1 for t in tasks if t["status"] == "verified"),
            "invalid_initial": sum(1 for t in tasks if t["defect_type"]),
            "total_diffs": total_diffs,
            "declared_rows": declared["c"],
            "distinct_refs": declared["d"],
            "screenshots": shots,
            "exceptions_by_kind": {r["kind"]: r["c"] for r in excs},
        },
        "tasks": items,
    }


if __name__ == "__main__":
    main()
