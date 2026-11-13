"""统一报关单结构 + 归一化 + 联动校验。
解析层用它拦脏数据，仿真网站端用同一套规则做服务端校验，两端口径一致。
"""
import re
from datetime import date, datetime

from common.codes import hs_legal_unit, name_to_code, port_country

# 统一报关单结构字段定义
HEADER_FIELDS = [
    "ref_no", "trade_mode", "levy_type", "contract_no",
    "consignee_name", "consignee_credit_code", "consignor_name", "consignor_country",
    "import_port", "export_port", "currency", "total_qty", "total_amount",
    "freight", "insurance", "pack_type", "trans_mode", "decl_date",
]
ITEM_FIELDS = [
    "line_no", "hs_code", "goods_name", "spec", "qty", "unit",
    "legal_qty", "currency", "unit_price", "total_price",
    "origin_country", "dest_country",
]
REQUIRED_HEADER = [
    "ref_no", "trade_mode", "contract_no", "consignee_name",
    "consignee_credit_code", "consignor_name", "consignor_country",
    "import_port", "export_port", "currency", "pack_type", "trans_mode", "decl_date",
]
REQUIRED_ITEM = ["hs_code", "goods_name", "qty", "unit", "unit_price", "total_price",
                 "origin_country", "dest_country"]
CREDIT_RE = re.compile(r"^[0-9A-HJ-NPQRTUWXY]{18}$")
MONEY_TOL = 0.02


def _num(v):
    if v is None or str(v).strip() == "":
        return None, "空值"
    try:
        return float(str(v).replace(",", "").strip()), None
    except ValueError:
        return None, f"数值格式错误: {v!r}"


def normalize(raw: dict) -> dict:
    """名称/别名 -> 代码，数值转 float，字符串去空白。返回归一化副本（不抛异常）。"""
    h_in, h = raw.get("header", {}), {}
    for k, v in h_in.items():
        h[k] = v.strip() if isinstance(v, str) else v
    items = []
    for it_in in raw.get("items", []):
        it = {k: (v.strip() if isinstance(v, str) else v) for k, v in it_in.items()}
        items.append(it)

    # 代码映射
    for field, kind in (("trade_mode", "trade_mode"), ("levy_type", "levy"),
                        ("pack_type", "pack"), ("trans_mode", "transport")):
        if h.get(field) not in (None, ""):
            code, _ = name_to_code(kind, h[field])
            if code:
                h[field] = code
    for field in ("consignor_country", "import_port", "export_port"):
        if h.get(field) not in (None, ""):
            code, _ = name_to_code("port", h[field])
            if code:
                h[field] = code
    if h.get("currency") not in (None, ""):
        code, _ = name_to_code("currency", h["currency"])
        if code:
            h["currency"] = code
    for f in ("total_qty", "total_amount", "freight", "insurance"):
        n, _ = _num(h.get(f))
        if n is not None:
            h[f] = n
    try:
        h["decl_date"] = datetime.strptime(h["decl_date"], "%Y-%m-%d").date().isoformat()
    except Exception:
        pass  # 留给校验报错

    for it in items:
        if it.get("unit") not in (None, ""):
            code, _ = name_to_code("unit", it["unit"])
            if code:
                it["unit"] = code
        if it.get("currency") not in (None, ""):
            code, _ = name_to_code("currency", it["currency"])
            if code:
                it["currency"] = code
        for f in ("origin_country", "dest_country"):
            if it.get(f) not in (None, ""):
                code, _ = name_to_code("port", it[f])
                if code:
                    it[f] = code
        for f in ("qty", "legal_qty", "unit_price", "total_price"):
            n, _ = _num(it.get(f))
            if n is not None:
                it[f] = n
        try:
            it["line_no"] = int(it["line_no"])
        except Exception:
            pass
    return {"header": h, "items": items}


def validate(decl: dict) -> list:
    """对归一化后的单据做必填 + 代码表 + 联动校验，返回错误列表。"""
    h, items, errs = decl["header"], decl["items"], []

    def err(field, msg):
        errs.append({"field": field, "message": msg})

    # 1. 表头必填
    for f in REQUIRED_HEADER:
        if h.get(f) in (None, ""):
            err(f"header.{f}", "必填字段缺失")
    if not items:
        err("items", "表体为空")

    # 2. 代码表存在性
    for f, kind in (("trade_mode", "trade_mode"), ("levy_type", "levy"),
                    ("pack_type", "pack"), ("trans_mode", "transport"),
                    ("currency", "currency")):
        if h.get(f):
            code, e = name_to_code(kind, h[f])
            if e:
                err(f"header.{f}", e)
    for f in ("consignor_country", "import_port", "export_port"):
        if h.get(f):
            code, e = name_to_code("port", h[f])
            if e:
                err(f"header.{f}", e)

    # 3. 统一社会信用代码
    cc = h.get("consignee_credit_code")
    if cc and not CREDIT_RE.match(str(cc)):
        err("header.consignee_credit_code", f"统一社会信用代码格式错误(应为18位): {cc!r}")

    # 4. 申报日期
    dd = h.get("decl_date")
    if dd:
        try:
            d = datetime.strptime(dd, "%Y-%m-%d").date()
            if d > date.today():
                err("header.decl_date", f"申报日期不能晚于今天: {dd}")
        except ValueError:
            err("header.decl_date", f"日期格式错误(应为YYYY-MM-DD): {dd!r}")

    # 5. 表体逐行
    seen_lines = set()
    for idx, it in enumerate(items, 1):
        pfx = f"items[{idx}]"
        for f in REQUIRED_ITEM:
            if it.get(f) in (None, ""):
                err(f"{pfx}.{f}", "必填字段缺失")
        if it.get("line_no") in seen_lines:
            err(f"{pfx}.line_no", f"表体序号重复: {it.get('line_no')}")
        seen_lines.add(it.get("line_no"))

        hs = it.get("hs_code")
        if hs:
            row_code, e = name_to_code("hs", hs)
            if e:
                err(f"{pfx}.hs_code", e)
            else:
                legal = hs_legal_unit(row_code)
                if legal and it.get("unit") and it["unit"] != legal:
                    err(f"{pfx}.unit",
                        f"计量单位 {it['unit']} 与 HS 编码 {hs} 法定计量单位 {legal} 不一致")
        for f in ("origin_country", "dest_country"):
            if it.get(f):
                _, e = name_to_code("port", it[f])
                if e:
                    err(f"{pfx}.{f}", e)
        if it.get("currency"):
            _, e = name_to_code("currency", it["currency"])
            if e:
                err(f"{pfx}.currency", e)
        if it.get("unit"):
            _, e = name_to_code("unit", it["unit"])
            if e:
                err(f"{pfx}.unit", e)

        for f in ("qty", "legal_qty", "unit_price", "total_price"):
            if it.get(f) is not None and not isinstance(it[f], (int, float)):
                err(f"{pfx}.{f}", f"数值格式错误: {it[f]!r}")
        if isinstance(it.get("qty"), (int, float)) and it["qty"] <= 0:
            err(f"{pfx}.qty", "数量必须大于0")

        # 联动①：总价 = 数量 × 单价
        if all(isinstance(it.get(f), (int, float)) for f in ("qty", "unit_price", "total_price")):
            calc = round(it["qty"] * it["unit_price"], 2)
            if abs(calc - it["total_price"]) > MONEY_TOL:
                err(f"{pfx}.total_price",
                    f"总价联动错误: 数量{it['qty']}×单价{it['unit_price']}={calc}, 填报{it['total_price']}")
        # 联动②：表体币制必须与表头一致
        if h.get("currency") and it.get("currency") and it["currency"] != h["currency"]:
            err(f"{pfx}.currency", f"表体币制 {it['currency']} 与表头币制 {h['currency']} 不一致")

    # 6. 表头汇总联动
    nums_ok = all(isinstance(it.get("qty"), (int, float)) for it in items) and items
    if nums_ok:
        sum_qty = round(sum(it["qty"] for it in items), 4)
        if isinstance(h.get("total_qty"), (int, float)) and abs(h["total_qty"] - sum_qty) > MONEY_TOL:
            err("header.total_qty", f"总数量联动错误: 表体合计 {sum_qty}, 表头填报 {h['total_qty']}")
    prices_ok = all(isinstance(it.get("total_price"), (int, float)) for it in items) and items
    if prices_ok and isinstance(h.get("total_amount"), (int, float)):
        calc_amt = round(sum(it["total_price"] for it in items)
                         + (h.get("freight") or 0) + (h.get("insurance") or 0), 2)
        if abs(calc_amt - h["total_amount"]) > MONEY_TOL:
            err("header.total_amount",
                f"总金额联动错误: 表体总价合计+运费+保费={calc_amt}, 表头填报 {h['total_amount']}")

    # 7. 港口-国别联动：出口港国别须与发货人国别一致
    if h.get("export_port") and h.get("consignor_country"):
        pc = port_country(h["export_port"])
        if pc and pc != h["consignor_country"]:
            err("header.export_port",
                f"出口港国别 {pc} 与发货人国别 {h['consignor_country']} 不一致")
    return errs


def normalize_and_validate(raw: dict):
    decl = normalize(raw)
    return decl, validate(decl)
