"""图形验证码：本地 PIL 渲染（旋转/噪点/干扰线），本地 numpy 模板匹配 OCR。
仿真网站用 issue() 发码，机器人用 solve() 识别；OCR 不接触答案表。
"""
import io
import random
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from common.db import get_conn, now_iso

W, H = 140, 50
GLYPH_W, GLYPH_H = 22, 30
CHARS = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
FONT_CANDIDATES = [
    r"C:\Windows\Fonts\arialbd.ttf",
    r"C:\Windows\Fonts\arial.ttf",
    r"C:\Windows\Fonts\segoeui.ttf",
]
FONT_PATH = next((p for p in FONT_CANDIDATES if Path(p).exists()), None)
FONT = ImageFont.truetype(FONT_PATH, 28) if FONT_PATH else ImageFont.load_default()
ANGLES = (-12, -9, -6, -3, 0, 3, 6, 9, 12)
SHIFTS = [(dx, dy) for dx in (-1, 0, 1) for dy in (-1, 0, 1)]

_templates = None  # char -> np.ndarray, shape (n_angles, GLYPH_H, GLYPH_W)


# ---------------- 图像基础 ----------------
def _binarize(img: Image.Image) -> Image.Image:
    g = img.convert("L").filter(ImageFilter.MedianFilter(3))  # 去椒盐噪点
    return g.point(lambda x: 0 if x < 175 else 255)


def _to_mask(img: Image.Image) -> np.ndarray:
    return (np.asarray(img, dtype=np.uint8) < 128).astype(np.float32)


def _norm_glyph(bw: Image.Image) -> np.ndarray:
    """按墨迹包围盒裁紧，等比缩放后居中到统一画布，保持字形长宽比。"""
    bbox = bw.point(lambda x: 255 - x).getbbox()
    if bbox:
        bw = bw.crop(bbox)
    gw, gh = bw.size
    scale = min(GLYPH_W / gw, GLYPH_H / gh)
    nw, nh = max(1, round(gw * scale)), max(1, round(gh * scale))
    resized = bw.resize((nw, nh))
    canvas = Image.new("L", (GLYPH_W, GLYPH_H), 255)
    canvas.paste(resized, ((GLYPH_W - nw) // 2, (GLYPH_H - nh) // 2))
    return _to_mask(canvas)


def _shift(m: np.ndarray, dx: int, dy: int) -> np.ndarray:
    h, w = m.shape
    out = np.zeros_like(m)
    out[max(0, dy):h + min(0, dy), max(0, dx):w + min(0, dx)] = \
        m[max(0, -dy):h + min(0, -dy), max(0, -dx):w + min(0, -dx)]
    return out


def _edges(m: np.ndarray) -> np.ndarray:
    e = np.zeros_like(m)
    e[:-1, :] += np.abs(np.diff(m, axis=0))
    e[:, :-1] += np.abs(np.diff(m, axis=1))
    return (e > 0).astype(np.float32)


def _iou(a: np.ndarray, b: np.ndarray) -> float:
    union = float(np.count_nonzero(a + b))
    if union == 0:
        return 1.0
    return float((a * b).sum()) / union


def _dilate(m: np.ndarray) -> np.ndarray:
    """1px 四邻域膨胀。"""
    d = m.copy()
    d[1:, :] = np.maximum(d[1:, :], m[:-1, :])
    d[:-1, :] = np.maximum(d[:-1, :], m[1:, :])
    d[:, 1:] = np.maximum(d[:, 1:], m[:, :-1])
    d[:, :-1] = np.maximum(d[:, :-1], m[:, 1:])
    return d


def _soft_score(a: np.ndarray, b: np.ndarray) -> float:
    """带 1px 容差的相似度：双方墨迹落在对方膨胀区内的比例，抗抗锯齿/微旋转。"""
    if a.sum() == 0 or b.sum() == 0:
        return 0.0
    da, db = _dilate(a), _dilate(b)
    cover_a = float((a * db).sum()) / float(a.sum())
    cover_b = float((b * da).sum()) / float(b.sum())
    return (cover_a + cover_b) / 2


def _build_templates():
    t = {}
    for ch in "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        angles = []
        for angle in ANGLES:
            im = Image.new("L", (34, 40), 255)
            ImageDraw.Draw(im).text((17, 20), ch, font=FONT, fill=0, anchor="mm")
            if angle:
                im = im.rotate(angle, fillcolor=255)
            angles.append(_norm_glyph(_binarize(im)))
        t[ch] = np.stack(angles)
    return t


# ---------------- 连通域/投影分割 ----------------
def _components(bw: Image.Image):
    w, h = bw.size
    px = bw.load()
    seen = bytearray(w * h)
    comps = []
    for sy in range(h):
        for sx in range(w):
            idx = sy * w + sx
            if seen[idx] or px[sx, sy] != 0:
                continue
            stack = [(sx, sy)]
            seen[idx] = 1
            x0 = x1 = sx
            y0 = y1 = sy
            cnt = 0
            while stack:
                x, y = stack.pop()
                cnt += 1
                x0, x1 = min(x0, x), max(x1, x)
                y0, y1 = min(y0, y), max(y1, y)
                for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                    if 0 <= nx < w and 0 <= ny < h and not seen[ny * w + nx] and px[nx, ny] == 0:
                        seen[ny * w + nx] = 1
                        stack.append((nx, ny))
            comps.append((x0, y0, x1 + 1, y1 + 1, cnt))
    return sorted(comps, key=lambda c: c[0])


def _projection_segments(bw: Image.Image, n: int):
    w, h = bw.size
    px = bw.load()
    col_ink = [sum(1 for y in range(h) if px[x, y] == 0) for x in range(w)]
    gaps, i = [], 0
    while i < w:
        if col_ink[i] == 0:
            j = i
            while j < w and col_ink[j] == 0:
                j += 1
            gaps.append((i, j))
            i = j
        else:
            i += 1
    bounds, start = [], 0
    for g0, g1 in gaps:
        if g0 > start:
            bounds.append((start, g0))
        start = g1
    if start < w:
        bounds.append((start, w))
    bounds = [b for b in bounds if sum(col_ink[b[0]:b[1]]) > 10]
    if len(bounds) != n:
        return None
    return [_norm_glyph(bw.crop((x0, 2, x1, h - 2))) for x0, x1 in bounds]


def _segment(bw: Image.Image, n: int = 4):
    """三级策略：连通域恰好 n 个 -> 按固定格归并连通域 -> 投影/固定格兜底。"""
    comps = [c for c in _components(bw) if c[4] >= 18]
    if len(comps) == n:
        return [_norm_glyph(bw.crop((x0, y0, x1, y1))) for x0, y0, x1, y1, _ in comps]
    # 按固定格归并：每个连通域归到中心所在格，取该格所有墨迹的并集包围盒
    cell = W // n
    boxes = [None] * n
    for x0, y0, x1, y1, cnt in comps:
        idx = min(n - 1, max(0, (x0 + x1) // 2 // cell))
        if boxes[idx] is None:
            boxes[idx] = [x0, y0, x1, y1]
        else:
            b = boxes[idx]
            b[0], b[1] = min(b[0], x0), min(b[1], y0)
            b[2], b[3] = max(b[2], x1), max(b[3], y1)
    if all(b is not None for b in boxes):
        return [_norm_glyph(bw.crop((b[0], b[1], b[2], b[3]))) for b in boxes]
    proj = _projection_segments(bw, n)
    if proj is not None:
        return proj
    return [_norm_glyph(bw.crop((i * cell + 4, 4, (i + 1) * cell - 4, H - 6)))
            for i in range(n)]


# ---------------- 渲染/发码/校验 ----------------
def render(text: str) -> bytes:
    img = Image.new("RGB", (W, H), (245, 246, 247))
    draw = ImageDraw.Draw(img)
    cell = W // len(text)
    for i, ch in enumerate(text):
        layer = Image.new("RGBA", (34, 40), (245, 246, 247, 0))
        ImageDraw.Draw(layer).text(
            (17, 20), ch, font=FONT,
            fill=(random.randint(10, 90), random.randint(10, 90), random.randint(10, 90), 255),
            anchor="mm")
        layer = layer.rotate(random.uniform(-8, 8), resample=Image.BICUBIC,
                             fillcolor=(245, 246, 247, 0))
        img.paste(layer, (i * cell + (cell - 34) // 2, 5), layer)
    for _ in range(2):
        draw.line([(random.randint(0, W), random.randint(0, H)),
                   (random.randint(0, W), random.randint(0, H))],
                  fill=(185, 185, 185), width=1)
    px = img.load()
    for _ in range(120):
        x, y = random.randint(0, W - 1), random.randint(0, H - 1)
        px[x, y] = (random.randint(150, 210),) * 3
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def issue(sid: str) -> bytes:
    text = "".join(random.choice(CHARS) for _ in range(4))
    conn = get_conn()
    conn.execute(
        "INSERT INTO sim_captcha(sid,text,created_at) VALUES(?,?,?) "
        "ON CONFLICT(sid) DO UPDATE SET text=excluded.text, created_at=excluded.created_at",
        (sid, text, now_iso()),
    )
    conn.close()
    return render(text)


def verify(sid: str, answer: str, consume: bool = True) -> bool:
    conn = get_conn()
    row = conn.execute("SELECT text FROM sim_captcha WHERE sid=?", (sid,)).fetchone()
    ok = bool(row) and answer and row["text"].upper() == answer.strip().upper()
    if ok and consume:
        conn.execute("DELETE FROM sim_captcha WHERE sid=?", (sid,))
    conn.close()
    return ok


# ---------------- OCR ----------------
def _score_char(glyph: np.ndarray, tpl_angles: np.ndarray) -> float:
    g_edge = _edges(glyph)
    best = 0.0
    for tpl in tpl_angles:
        t_edge = _edges(tpl)
        for dx, dy in SHIFTS:
            solid = _soft_score(_shift(glyph, dx, dy), tpl)
            edge = _iou(_shift(g_edge, dx, dy), t_edge)
            best = max(best, 0.75 * solid + 0.25 * edge)
    return best


def solve(png_bytes: bytes) -> str:
    global _templates
    if _templates is None:
        _templates = _build_templates()
    bw = _binarize(Image.open(io.BytesIO(png_bytes)))
    out = []
    for glyph in _segment(bw, 4):
        best_ch, best_score = "", 0.0
        for ch, tpl_angles in _templates.items():
            s = _score_char(glyph, tpl_angles)
            if s > best_score:
                best_score, best_ch = s, ch
        out.append(best_ch)
    return "".join(out)
