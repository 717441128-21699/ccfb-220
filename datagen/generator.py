"""生成 100 单测试单据：每单一份客户 Excel + 一份发票 PDF。
其中 80 单干净、20 单注入脏数据；脏单同时在 reissue/ 生成客户补正件与修复清单。
固定随机种子保证可复现。
"""
import json
import random
from datetime import date, timedelta

from openpyxl import Workbook
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfgen import canvas as pdfcanvas

from common.codes import (CURRENCY, HS, PACK, PORT_ALIAS, TRANS, TRADE_MODE,
                          UNIT, code_name)
from common.db import get_conn, init_db, set_setting
from common.paths import INBOX, REISSUE, ROOT

random.seed(42)
pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))

COMPANIES = [
    ("上海锦程电子进出口有限公司", "91310000MA1FL8XQ2T"),
    ("宁波海曙恒泰贸易有限公司", "91330200MA282K7P9A"),
    ("深圳前海博朗科技有限公司", "91440300MA5F3G6H1L"),
    ("苏州工业园区汇川实业有限公司", "91320594MA1X9R4B7C"),
    ("广州白云宏运供应链有限公司", "91440101MA9T5W2D8E"),
]
CONSIGNORS = [
    ("PACIFIC GLOBAL TRADING LTD", "US", "USLAX"),
    ("TOKYO SANGYO CO.,LTD.", "JP", "JPTYO"),
    ("HANSE IMPORT GMBH", "DE", "DEHAM"),
    ("SEOUL MOBILE INC.", "KR", "KRINC"),
]
PORTS_CN = ["CNSHA", "CNNGB", "CNSZX"]
# 境外港口（与 CONSIGNORS 国别对应）
FOREIGN_PORTS = {"US": "USLAX", "JP": "JPTYO", "DE": "DEHAM", "KR": "KRINC"}
DEFECT_TYPES = [
    "missing_field",          # 必填字段缺失
    "bad_credit_code",        # 信用代码格式错误
    "bad_date",               # 申报日期非法/未来日期
    "bad_currency",           # 币制不在代码表
    "bad_unit",               # 计量单位与 HS 法定单位不一致
    "price_mismatch",         # 总价 != 数量×单价
    "header_amount_mismatch", # 总金额汇总联动错误
    "qty_mismatch",           # 总数量汇总联动错误
    "port_country_mismatch",  # 出口港国别与发货人国别不一致
    "pdf_excel_conflict",     # PDF 与 Excel 金额不一致
    "bad_number",             # 数值列出现文本
    "unknown_hs",             # HS 编码不存在
]
SPEC_WORDS = ["工业级", "标准型", "增强版", "定制款", "通用", "耐高温", "低功耗", "防水型"]
GOODS_NAME = {
    "8471300000": "便携式笔记本电脑", "8471414000": "微型计算机处理部件",
    "8517130000": "智能手机", "8528720000": "液晶彩色电视机",
    "8414599099": "工业离心风机", "9405420000": "LED 照明灯具",
    "6203429099": "棉制男式长裤", "3926909090": "塑料零配件",
    "7326909000": "钢制支架", "8544422000": "带接头连接电缆",
}


def _gen_clean(seq: int) -> dict:
    consignee, credit = random.choice(COMPANIES)
    consignor, country, fport = random.choice(CONSIGNORS)
    cur, _ = random.choice(CURRENCY)
    n_items = random.randint(6, 13)  # 配合每页 5 行，必然触发表体分页
    items, line_no = [], 1
    total_qty, total_goods = 0.0, 0.0
    for _ in range(n_items):
        hs, hs_name, legal_unit = random.choice(HS)
        qty = round(random.uniform(5, 400), 2)
        price = round(random.uniform(2, 900), 2)
        tp = round(qty * price, 2)
        unit = legal_unit
        legal_qty = qty  # 本演练中法定数量=成交数量
        items.append({
            "line_no": str(line_no),
            "hs_code": hs,
            "goods_name": GOODS_NAME[hs],
            "spec": f"{random.choice(SPEC_WORDS)}/{random.choice(['A','B','C'])}型",
            "qty": str(qty), "unit": code_name("unit", unit),
            "legal_qty": str(legal_qty), "currency": cur,
            "unit_price": str(price), "total_price": str(tp),
            "origin_country": country, "dest_country": "CN",
        })
        total_qty += qty
        total_goods += tp
        line_no += 1
    freight = round(total_goods * random.uniform(0.01, 0.03), 2)
    insurance = round(total_goods * random.uniform(0.001, 0.005), 2)
    d = date.today() - timedelta(days=random.randint(0, 20))
    header = {
        "ref_no": f"REF2026{seq:04d}",
        "trade_mode": "一般贸易",
        "levy_type": "一般征税",
        "contract_no": f"CT{random.randint(100000, 999999)}",
        "consignee_name": consignee,
        "consignee_credit_code": credit,
        "consignor_name": consignor,
        "consignor_country": country,
        "import_port": random.choice(PORTS_CN),       # 进口港：中国
        "export_port": fport,                          # 出口港：发货人所在国
        "currency": cur,
        "total_qty": str(round(total_qty, 2)),
        "total_amount": str(round(total_goods + freight + insurance, 2)),
        "freight": str(freight),
        "insurance": str(insurance),
        "pack_type": random.choice([p[1] for p in PACK]),
        "trans_mode": random.choice([t[1] for t in TRANS]),
        "decl_date": d.isoformat(),
    }
    return {"header": header, "items": items}


# ---------------- 脏数据注入 ----------------
def _inject(defect: str, decl: dict, seq: int):
    h, items = decl["header"], decl["items"]
    it = items[0]
    if defect == "missing_field":
        del h["contract_no"]
    elif defect == "bad_credit_code":
        h["consignee_credit_code"] = "91310000MA1FL8XQ2"  # 17 位
    elif defect == "bad_date":
        h["decl_date"] = (date.today() + timedelta(days=7)).isoformat()
    elif defect == "bad_currency":
        h["currency"] = "XYZ"
        it["currency"] = "XYZ"
    elif defect == "bad_unit":
        from common.codes import hs_legal_unit
        legal = hs_legal_unit(it["hs_code"])
        others = [u for u in UNIT if u[0] != legal]
        it["unit"] = random.choice(others)[1]
    elif defect == "price_mismatch":
        it["total_price"] = str(round(float(it["total_price"]) + 50, 2))
    elif defect == "header_amount_mismatch":
        h["total_amount"] = str(round(float(h["total_amount"]) + 999, 2))
    elif defect == "qty_mismatch":
        h["total_qty"] = str(round(float(h["total_qty"]) + 10, 2))
    elif defect == "port_country_mismatch":
        cc = h["consignor_country"]
        wrong = [p for p in PORTS_CN]  # 出口港改成中国港且发货人非中国 -> 国别不一致
        h["export_port"] = random.choice(wrong)
        h["consignor_country"] = {"US": "JP", "JP": "DE", "DE": "KR", "KR": "US"}[cc]
    elif defect == "pdf_excel_conflict":
        it["total_price"] = str(round(float(it["total_price"]) + 120, 2))
    elif defect == "bad_number":
        it["qty"] = "约壹佰件"
    elif defect == "unknown_hs":
        it["hs_code"] = "9999999999"
    return decl


# ---------------- Excel ----------------
HEADER_ORDER = ["ref_no", "trade_mode", "levy_type", "contract_no", "consignee_name",
                "consignee_credit_code", "consignor_name", "consignor_country",
                "import_port", "export_port", "currency", "total_qty", "total_amount",
                "freight", "insurance", "pack_type", "trans_mode", "decl_date"]
ITEM_ORDER = ["line_no", "hs_code", "goods_name", "spec", "qty", "unit",
              "legal_qty", "currency", "unit_price", "total_price",
              "origin_country", "dest_country"]


def write_excel(decl: dict, path):
    wb = Workbook()
    ws = wb.active
    ws.title = "报关委托"
    ws.append(["报关委托书（客户填写）"])
    ws.append(HEADER_ORDER)
    ws.append([decl["header"].get(k, "") for k in HEADER_ORDER])
    ws.append([])
    ws.append(["表体商品明细"])
    ws.append(ITEM_ORDER)
    for it in decl["items"]:
        ws.append([it.get(k, "") for k in ITEM_ORDER])
    for col in ws.columns:
        w = max((len(str(c.value)) for c in col if c.value is not None), default=8)
        ws.column_dimensions[col[0].column_letter].width = min(max(w + 2, 10), 26)
    wb.save(path)


# ---------------- 发票 PDF ----------------
def write_pdf(decl: dict, path, conflict_defect=False):
    c = pdfcanvas.Canvas(str(path), pagesize=A4)
    w, h = A4
    y = h - 22 * mm
    c.setFont("STSong-Light", 16)
    c.drawCentredString(w / 2, y, "COMMERCIAL INVOICE 商业发票")
    y -= 10 * mm
    c.setFont("STSong-Light", 9)
    hdr = decl["header"]
    lines = [
        f"Invoice No: INV-{hdr['ref_no']}    Contract No: {hdr.get('contract_no', '')}",
        f"Consignee: {hdr['consignee_name']}  ({hdr['consignee_credit_code']})",
        f"Consignor: {hdr['consignor_name']}   Country: {hdr['consignor_country']}",
        f"Port of loading: {hdr['export_port']}   Port of discharge: {hdr['import_port']}",
        f"Currency: {hdr['currency']}   Freight: {hdr['freight']}   Insurance: {hdr['insurance']}",
        f"Packing: {hdr['pack_type']}   Transport: {hdr['trans_mode']}   Date: {hdr['decl_date']}",
    ]
    for ln in lines:
        c.drawString(18 * mm, y, ln)
        y -= 6 * mm
    y -= 4 * mm
    # 列 x 坐标（mm）：A4 宽 210mm，左右各留 12mm，列间留足空隙防 CID 字体撞列
    cols = [12, 22, 48, 108, 128, 142, 164]
    heads = ["Line", "HS Code", "Goods", "Qty", "Unit", "Price", "Amount"]
    c.setFillColor(colors.HexColor("#1f5fa8"))
    c.rect(11 * mm, y - 1 * mm, 186 * mm, 6 * mm, fill=1, stroke=0)
    c.setFillColor(colors.white)
    for x, t in zip(cols, heads):
        c.drawString(x * mm, y + 0.6 * mm, t)
    c.setFillColor(colors.black)
    y -= 8 * mm
    total = 0.0
    for it in decl["items"]:
        # pdf_excel_conflict：PDF 保持正确金额，Excel 被注入错误 -> 解析交叉核对拦截
        tp = float(it["total_price"])
        if conflict_defect:
            tp = round(tp - 120, 2)
        total += tp
        vals = [it["line_no"], it["hs_code"], f"G-{it['goods_name'][:16]}", it["qty"],
                it["unit"], it["unit_price"], f"{tp:.2f}"]
        for x, v in zip(cols, vals):
            c.drawString(x * mm, y, str(v))
        y -= 6 * mm
        if y < 28 * mm:
            c.showPage()
            y = h - 22 * mm
    y -= 4 * mm
    c.setFont("STSong-Light", 11)
    c.drawString(120 * mm, y, f"TOTAL ({hdr['currency']}): {total:.2f}")
    y -= 9 * mm
    c.setFont("STSong-Light", 9)
    c.drawString(18 * mm, y, "This invoice is locally generated test data for RPA simulation only.")
    c.save()


def generate(n: int = 100, dirty_n: int = 20):
    init_db()
    manifest = []
    dirty_seqs = set(random.sample(range(1, n + 1), dirty_n))
    for seq in range(1, n + 1):
        decl = _gen_clean(seq)
        ref = decl["header"]["ref_no"]
        is_dirty = seq in dirty_seqs
        defect = random.choice(DEFECT_TYPES) if is_dirty else ""
        if is_dirty:
            _inject(defect, decl, seq)
        xlsx = INBOX / f"{ref}.xlsx"
        pdf = INBOX / f"{ref}.pdf"
        write_excel(decl, xlsx)
        write_pdf(decl, pdf, conflict_defect=(defect == "pdf_excel_conflict"))

        rec = {"ref_no": ref, "excel": str(xlsx), "pdf": str(pdf),
               "dirty": is_dirty, "defect_type": defect}
        if is_dirty:
            # 客户补正件：重新生成一份正确单据（覆盖同一 ref 的干净版本）
            clean = _gen_clean(seq)
            clean["header"]["ref_no"] = ref
            rx = REISSUE / f"{ref}_reissue.xlsx"
            rp = REISSUE / f"{ref}_reissue.pdf"
            write_excel(clean, rx)
            write_pdf(clean, rp)
            rec["reissue_excel"] = str(rx)
            rec["reissue_pdf"] = str(rp)
        manifest.append(rec)

    out = ROOT / "data" / "manifest.json"
    out.write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    conn = get_conn()
    set_setting(conn, "demo_total", n)
    set_setting(conn, "demo_dirty", dirty_n)
    conn.close()
    print(f"generated {n} declarations ({n - dirty_n} clean / {dirty_n} dirty)")
    print(f"manifest -> {out}")
    return manifest


if __name__ == "__main__":
    generate()
