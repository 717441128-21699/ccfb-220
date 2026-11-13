"""代码表：币制 / 计量单位 / 国别港口 / HS 商品编码 / 贸易方式 / 征免性质 / 包装种类。
仿真网站与机器人共用同一份代码表，保证映射结果与网站端一致。
"""
import json

from common.db import get_conn, init_db

# ---------- 种子数据 ----------
CURRENCY = [
    ("USD", "美元"), ("CNY", "人民币"), ("EUR", "欧元"),
    ("JPY", "日元"), ("GBP", "英镑"), ("HKD", "港币"),
    ("AUD", "澳元"), ("KRW", "韩元"),
]
UNIT = [
    ("001", "台"), ("002", "个"), ("003", "只"), ("004", "千克"),
    ("005", "套"), ("006", "米"), ("007", "平方米"), ("008", "批"),
    ("009", "件"), ("010", "箱"), ("011", "升"), ("012", "吨"),
]
COUNTRY_PORT = [
    # code, 名称, 所属国别
    ("CN", "中国", "CN"), ("US", "美国", "US"), ("JP", "日本", "JP"),
    ("DE", "德国", "DE"), ("KR", "韩国", "KR"), ("VN", "越南", "VN"),
    ("CNSHA", "上海港", "CN"), ("CNNGB", "宁波港", "CN"), ("CNSZX", "深圳港", "CN"),
    ("USLAX", "洛杉矶港", "US"), ("JPTYO", "东京港", "JP"),
    ("DEHAM", "汉堡港", "DE"), ("KRINC", "仁川港", "KR"),
]
# HS 编码 extra 记录法定计量单位 code
HS = [
    ("8471300000", "便携式自动数据处理设备", "001"),
    ("8471414000", "微型机处理部件", "001"),
    ("8517130000", "智能手机", "002"),
    ("8528720000", "彩色电视机", "001"),
    ("8414599099", "其他风机", "002"),
    ("9405420000", "LED 灯具", "003"),
    ("6203429099", "棉制男裤", "009"),
    ("3926909090", "塑料制品", "009"),
    ("7326909000", "钢铁制品", "004"),
    ("8544422000", "带接头电缆", "006"),
]
TRADE_MODE = [("0110", "一般贸易"), ("0214", "来料加工"), ("0615", "进料对口"), ("1210", "保税电商")]
LEVY = [("101", "一般征税"), ("502", "来料设备"), ("503", "进料加工"), ("799", "保税区")]
PACK = [("22", "木箱"), ("23", "纸箱"), ("34", "托盘"), ("39", "散装"), ("61", "裸装")]
TRANS = [("1", "海运"), ("2", "铁路运输"), ("3", "公路运输"), ("5", "航空运输")]

SEED = {
    "currency": [(c, n, None) for c, n in CURRENCY],
    "unit": [(c, n, None) for c, n in UNIT],
    "port": [(c, n, json.dumps({"country": x}, ensure_ascii=False)) for c, n, x in COUNTRY_PORT],
    "hs": [(c, n, json.dumps({"legal_unit": u}, ensure_ascii=False)) for c, n, u in HS],
    "trade_mode": [(c, n, None) for c, n in TRADE_MODE],
    "levy": [(c, n, None) for c, n in LEVY],
    "pack": [(c, n, None) for c, n in PACK],
    "transport": [(c, n, None) for c, n in TRANS],
}

# 名称 -> 代码的别名表（Excel/PDF 里常写中文名称或不规范写法）
CURRENCY_ALIAS = {
    "人民币": "CNY", "人民币元": "CNY", "元": "CNY", "RMB": "CNY",
    "美元": "USD", "美金": "USD", "美圆": "USD",
    "欧元": "EUR", "日元": "JPY", "日元（100）": "JPY",
    "英镑": "GBP", "港币": "HKD", "港元": "HKD",
    "澳元": "AUD", "澳币": "AUD", "韩元": "KRW",
}
UNIT_ALIAS = {n: c for c, n in UNIT} | {"公斤": "004", "千克（公斤）": "004", "台（套）": "001"}
PORT_ALIAS = {n: c for c, n, _ in COUNTRY_PORT} | {
    "上海": "CNSHA", "宁波": "CNNGB", "深圳": "CNSZX",
    "洛杉矶": "USLAX", "东京": "JPTYO", "汉堡": "DEHAM", "仁川": "KRINC",
}
TRADE_ALIAS = {n: c for c, n in TRADE_MODE} | {"一般贸易方式": "0110"}
LEVY_ALIAS = {n: c for c, n in LEVY}
PACK_ALIAS = {n: c for c, n in PACK}
TRANS_ALIAS = {n: c for c, n in TRANS} | {"水路运输": "1", "空运": "5"}


def seed_codes():
    init_db()
    conn = get_conn()
    for kind, rows in SEED.items():
        conn.executemany(
            "INSERT OR REPLACE INTO code_table(kind, code, name, extra) VALUES(?,?,?,?)",
            [(kind, c, n, e) for c, n, e in rows],
        )
    conn.close()


def name_to_code(kind: str, value: str):
    """代码原样返回（校验存在性）；名称/别名映射为代码。返回 (code, error)。"""
    if value is None:
        return None, "空值"
    v = str(value).strip()
    if not v:
        return None, "空值"
    conn = get_conn()
    row = conn.execute(
        "SELECT code FROM code_table WHERE kind=? AND code=?", (kind, v)
    ).fetchone()
    if row:
        conn.close()
        return v, None
    alias_maps = {
        "currency": CURRENCY_ALIAS, "unit": UNIT_ALIAS, "port": PORT_ALIAS,
        "trade_mode": TRADE_ALIAS, "levy": LEVY_ALIAS,
        "pack": PACK_ALIAS, "transport": TRANS_ALIAS,
    }
    m = alias_maps.get(kind, {})
    if v in m:
        conn.close()
        return m[v], None
    conn.close()
    return None, f"代码表[{kind}]中找不到 {v!r} 对应的代码"


def code_name(kind: str, code: str) -> str:
    conn = get_conn()
    row = conn.execute(
        "SELECT name FROM code_table WHERE kind=? AND code=?", (kind, code)
    ).fetchone()
    conn.close()
    return row["name"] if row else code


def hs_legal_unit(hs_code: str):
    conn = get_conn()
    row = conn.execute("SELECT extra FROM code_table WHERE kind='hs' AND code=?", (hs_code,)).fetchone()
    conn.close()
    if not row:
        return None
    return json.loads(row["extra"]).get("legal_unit")


def port_country(port_code: str):
    conn = get_conn()
    row = conn.execute("SELECT extra FROM code_table WHERE kind='port' AND code=?", (port_code,)).fetchone()
    conn.close()
    if not row:
        return None
    return json.loads(row["extra"]).get("country")
