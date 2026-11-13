"""入库编排：Excel + PDF 双源解析、交叉核对、归一化校验、任务落库。
校验不过的单据直接拦下（status=invalid）并开异常工单，绝不进入录入队列。
"""
import json

from common.db import (audit, dumps, get_conn, loads, now_iso)
from common.validation import normalize_and_validate
from parser_service.excel_parser import parse_excel
from parser_service.pdf_parser import parse_pdf


def cross_check(raw_excel: dict, pdf: dict) -> list:
    """Excel 与发票 PDF 交叉核对，返回错误列表。"""
    errs = []
    h = raw_excel["header"]
    inv_no = pdf["meta"].get("invoice_no", "").replace("INV-", "")
    if inv_no and h.get("ref_no") and inv_no != h["ref_no"]:
        errs.append({"field": "pdf.invoice_no",
                     "message": f"PDF 发票号 {inv_no} 与 Excel 业务编号 {h['ref_no']} 不一致"})
    pdf_cur = pdf["meta"].get("currency", "")
    if pdf_cur and h.get("currency") and pdf_cur != h["currency"]:
        errs.append({"field": "pdf.currency",
                     "message": f"PDF 币制 {pdf_cur} 与 Excel 币制 {h['currency']} 不一致"})
    # HS 编码在同一单内可能重复：按 HS 建队列，逐条匹配并出队
    pdf_queues = {}
    for ln in pdf["lines"]:
        pdf_queues.setdefault(ln["hs_code"], []).append(ln)
    for idx, it in enumerate(raw_excel["items"], 1):
        hs = it.get("hs_code", "")
        q = pdf_queues.get(hs)
        if not q:
            errs.append({"field": f"pdf.items[{idx}]",
                         "message": f"HS 编码 {hs} 在发票 PDF 中找不到对应明细"})
            continue
        p_ln = q.pop(0)
        try:
            x_amt = float(str(it["total_price"]).replace(",", ""))
            p_amt = float(p_ln["total_price"])
            if abs(x_amt - p_amt) > 0.02:
                errs.append({"field": f"pdf.items[{idx}].total_price",
                             "message": f"HS {hs} 发票金额 {p_amt} 与 Excel 金额 {x_amt} 不一致"})
        except (KeyError, TypeError, ValueError):
            errs.append({"field": f"pdf.items[{idx}].total_price", "message": "金额无法解析"})
    if not pdf["lines"]:
        errs.append({"field": "pdf.items", "message": "发票 PDF 未解析出任何商品明细"})
    return errs


def ingest_file(excel_path: str, pdf_path: str, defect_type: str = "",
                task_id: int = None) -> dict:
    """解析一对来件并入库。task_id 非空表示补正重跑（复用原任务）。"""
    raw = parse_excel(excel_path)
    pdf = parse_pdf(pdf_path)
    cross_errs = cross_check(raw, pdf)
    decl, val_errs = normalize_and_validate(raw)
    all_errs = cross_errs + val_errs

    conn = get_conn()
    ref = decl["header"].get("ref_no") or raw["header"].get("ref_no")
    if task_id is None:
        cur = conn.execute(
            "INSERT INTO tasks(ref_no,inbox_excel,inbox_pdf,status,defect_type,"
            "validation_errors,run_count,created_at,updated_at) VALUES(?,?,?,?,?,?,0,?,?)",
            (ref, excel_path, pdf_path, "invalid" if all_errs else "queued",
             defect_type, dumps(all_errs), now_iso(), now_iso()),
        )
        task_id = cur.lastrowid
    else:
        conn.execute(
            "UPDATE tasks SET inbox_excel=?,inbox_pdf=?,status=?,validation_errors=?,"
            "updated_at=? WHERE id=?",
            (excel_path, pdf_path, "invalid" if all_errs else "queued",
             dumps(all_errs), now_iso(), task_id),
        )
        conn.execute("DELETE FROM exceptions WHERE task_id=? AND status='open'", (task_id,))

    conn.execute(
        "INSERT INTO declarations(task_id,ref_no,header_json,items_json,updated_at) "
        "VALUES(?,?,?,?,?) ON CONFLICT(task_id) DO UPDATE SET ref_no=excluded.ref_no,"
        "header_json=excluded.header_json,items_json=excluded.items_json,updated_at=excluded.updated_at",
        (task_id, ref, dumps(decl["header"]), dumps(decl["items"]), now_iso()),
    )
    if all_errs:
        conn.execute(
            "INSERT INTO exceptions(task_id,kind,message,status,created_at) VALUES(?,?,?,'open',?)",
            (task_id, "validation",
             f"{len(all_errs)} 项校验错误：" + "；".join(e["message"] for e in all_errs[:5]),
             now_iso()),
        )
        audit(conn, "parser", "INGEST_INVALID", f"task {task_id} ({ref}) 拦截 {len(all_errs)} 项错误")
    else:
        audit(conn, "parser", "INGEST_OK", f"task {task_id} ({ref}) 校验通过进入队列")
    conn.close()
    return {"task_id": task_id, "ref_no": ref, "errors": all_errs,
            "valid": not all_errs}


def reissue_task(task_id: int, new_excel: str, new_pdf: str) -> dict:
    """人工处理完脏数据（或收到客户补正件）后重跑解析。"""
    result = ingest_file(new_excel, new_pdf, task_id=task_id)
    if result["valid"]:
        conn = get_conn()
        conn.execute(
            "UPDATE exceptions SET status='resolved',resolution=?,resolved_at=? "
            "WHERE task_id=? AND status='open'",
            ("已采用补正件重新解析并通过校验", now_iso(), task_id),
        )
        audit(conn, "operator", "REISSUE_OK", f"task {task_id} 补正通过，重新入队")
        conn.close()
    return result


def load_declaration(task_id: int):
    conn = get_conn()
    row = conn.execute("SELECT * FROM declarations WHERE task_id=?", (task_id,)).fetchone()
    conn.close()
    if not row:
        return None
    return {"header": loads(row["header_json"], {}), "items": loads(row["items_json"], [])}
