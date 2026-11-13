"""提交后回查：拉取网站端申报数据，与原单逐字段核对，出差异报告。"""
from common.db import audit, dumps, get_conn, now_iso
from common.paths import SIM_BASE


def _norm_num(v):
    try:
        return round(float(v), 2)
    except (TypeError, ValueError):
        return v


def _norm_str(v):
    return "" if v is None else str(v).strip()


def compare(original: dict, remote: dict) -> list:
    """逐字段核对，返回差异列表（空列表 = 零差异）。"""
    diffs = []
    for k, ov in original["header"].items():
        rv = remote["header"].get(k)
        a, b = _norm_num(ov), _norm_num(rv)
        if isinstance(a, float) or isinstance(b, float):
            if a != b:
                diffs.append({"field": f"header.{k}", "original": ov, "remote": rv})
        elif _norm_str(ov) != _norm_str(rv):
            diffs.append({"field": f"header.{k}", "original": ov, "remote": rv})
    if len(original["items"]) != len(remote["items"]):
        diffs.append({"field": "items.length",
                      "original": len(original["items"]), "remote": len(remote["items"])})
    for i, (oi, ri) in enumerate(zip(original["items"], remote["items"])):
        for k, ov in oi.items():
            rv = ri.get(k)
            a, b = _norm_num(ov), _norm_num(rv)
            if isinstance(a, float) or isinstance(b, float):
                if a != b:
                    diffs.append({"field": f"items[{i + 1}].{k}", "original": ov, "remote": rv})
            elif _norm_str(ov) != _norm_str(rv):
                diffs.append({"field": f"items[{i + 1}].{k}", "original": ov, "remote": rv})
    return diffs


def reconcile(page, task_id: int, sim_ref: str, original: dict) -> dict:
    resp = page.request.get(f"{SIM_BASE}/api/declaration/{sim_ref}")
    if resp.status != 200:
        return {"result": "mismatch",
                "diffs": [{"field": "api", "original": None,
                           "remote": f"回查接口 HTTP {resp.status}"}],
                "remote": None}
    remote = resp.json()
    diffs = compare(original, remote)
    result = "match" if not diffs else "mismatch"
    conn = get_conn()
    conn.execute(
        "INSERT INTO reconcile_reports(task_id,result,diffs_json,sim_json,created_at) "
        "VALUES(?,?,?,?,?) ON CONFLICT(task_id) DO UPDATE SET result=excluded.result,"
        "diffs_json=excluded.diffs_json,sim_json=excluded.sim_json,created_at=excluded.created_at",
        (task_id, result, dumps(diffs), dumps(remote), now_iso()),
    )
    audit(conn, f"rpa-task{task_id}", "RECONCILE",
          f"{sim_ref} -> {result} ({len(diffs)} diffs)")
    conn.close()
    return {"result": result, "diffs": diffs, "remote": remote}
