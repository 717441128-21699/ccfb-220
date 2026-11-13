"""客户 Excel 解析 -> 原始报关单结构（未归一化）。"""
from openpyxl import load_workbook

from datagen.generator import HEADER_ORDER, ITEM_ORDER


def parse_excel(path: str) -> dict:
    wb = load_workbook(path, data_only=True)
    ws = wb.active
    rows = [[("" if c is None else str(c).strip()) for c in r]
            for r in ws.iter_rows(values_only=True)]
    # 定位表头行与表体行（标签驱动，避免依赖固定行号）
    hdr_idx = next(i for i, r in enumerate(rows) if r and r[0] == HEADER_ORDER[0])
    hdr_keys = rows[hdr_idx]
    hdr_vals = rows[hdr_idx + 1]
    header = {k: v for k, v in zip(hdr_keys, hdr_vals) if k}

    item_hdr_idx = next(i for i, r in enumerate(rows) if r and r[0] == ITEM_ORDER[0])
    item_keys = rows[item_hdr_idx]
    items = []
    for r in rows[item_hdr_idx + 1:]:
        if not any(r):
            continue
        it = {k: v for k, v in zip(item_keys, r) if k}
        if it.get("hs_code"):
            items.append(it)
    return {"header": header, "items": items}
