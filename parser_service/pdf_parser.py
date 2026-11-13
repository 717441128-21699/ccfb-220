"""发票 PDF 解析 -> 发票字段与明细金额，用于与 Excel 交叉核对。"""
import re

import pdfplumber

# 行首：序号 + 10位HS编码；行尾：单价 + 金额；中间为品名/规格/数量/单位
LINE_RE = re.compile(
    r"^(\d{1,2})\s+(\d{10})\s+(.+?)\s+"        # line_no, hs, 品名(贪婪到最后一个空格段前)
    r"([\d.,]+)\s+(\S+)\s+([\d.,]+)\s+([\d.,]+)\s*$"  # qty, unit, price, amount
)


def parse_pdf(path: str) -> dict:
    text, tables = "", []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            text += (page.extract_text() or "") + "\n"
            for t in page.extract_tables() or []:
                tables.append(t)

    def find(prefix):
        for line in text.splitlines():
            if prefix in line:
                return line.split(":", 1)[1].strip()
        return ""

    meta = {
        "invoice_no": find("Invoice No").split()[0] if find("Invoice No") else "",
        "contract_no": find("Contract No"),
        "currency": find("Currency").split()[0] if find("Currency") else "",
    }
    # 明细：优先按表格列位置解析（reportlab 生成的列固定），
    # 兜底再用"第2列为10位HS编码"的规则按文本解析
    lines = []
    for t in tables:
        for row in t:
            cells = [(c or "").replace("\n", " ").strip() for c in row]
            if len(cells) >= 7 and cells[1].isdigit() and len(cells[1]) == 10:
                lines.append({
                    "line_no": cells[0], "hs_code": cells[1],
                    "goods_name": cells[2], "qty": cells[3],
                    "unit": cells[4], "unit_price": cells[5],
                    "total_price": cells[6].replace(",", ""),
                })
    if not lines:
        for line in text.splitlines():
            m = LINE_RE.match(line.strip())
            if m:
                lines.append({
                    "line_no": m.group(1), "hs_code": m.group(2),
                    "goods_name": m.group(3).strip(),
                    "qty": m.group(4).replace(",", ""), "unit": m.group(5),
                    "unit_price": m.group(6).replace(",", ""),
                    "total_price": m.group(7).replace(",", ""),
                })
    return {"meta": meta, "lines": lines, "raw_text": text[:2000]}
