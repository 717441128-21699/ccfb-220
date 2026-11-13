"""验证码 OCR 自测：渲染 N 个验证码并识别，统计准确率。"""
from common.captcha import render, solve


def main(n=100):
    import random
    random.seed(7)
    from common.captcha import CHARS
    ok = 0
    misses = []
    for i in range(n):
        text = "".join(random.choice(CHARS) for _ in range(4))
        guess = solve(render(text))
        if guess == text:
            ok += 1
        else:
            misses.append((text, guess))
    rate = ok / n
    print(f"captcha OCR: {ok}/{n} = {rate:.1%}")
    for t, g in misses[:15]:
        print(f"  truth={t} guess={g}")
    # 6 次重试下的整体通过率（每次独立渲染）
    import math
    print(f"login pass within 6 tries ~= {1 - (1 - rate) ** 6:.2%}")


if __name__ == "__main__":
    main()
