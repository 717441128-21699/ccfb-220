"""调试：渲染一张验证码，保存原图/二值图/四个分割字形，并打印每位置候选。"""
import random
from pathlib import Path

from PIL import Image

from common.captcha import (CHARS, FONT, GLYPH_H, GLYPH_W, W, H, _binarize,
                            _build_templates, _score_char, _segment, render)

OUT = Path("data/debug")
OUT.mkdir(parents=True, exist_ok=True)
random.seed(11)
text = "".join(random.choice(CHARS) for _ in range(4))
png = render(text)
(OUT / "captcha.png").write_bytes(png)
bw = _binarize(Image.open(__import__("io").BytesIO(png)))
bw.save(OUT / "binary.png")

glyphs = _segment(bw, 4)
tpls = _build_templates()
strip = Image.new("L", (GLYPH_W * 4 + 30, GLYPH_H), 255)
for i, g in enumerate(glyphs):
    im = Image.fromarray((255 * (1 - g)).astype("uint8"))
    strip.paste(im, (i * (GLYPH_W + 10), 0))
strip.save(OUT / "segments.png")

tpls = _build_templates()
print("truth:", text)
for i, g in enumerate(glyphs):
    rows = sorted(((ch, round(_score_char(g, am), 3)) for ch, am in tpls.items()),
                  key=lambda x: -x[1])[:5]
    print(f"pos{i} truth={text[i]} -> {rows}")
print("saved to", OUT)
