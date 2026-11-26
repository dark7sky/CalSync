# -*- coding: utf-8 -*-
"""CalSync 브랜드 아이콘 생성 (Pillow).

캘린더(body + 헤더 밴드 + 링 탭) + 동기화(cyan 원형 화살표) 모티프.
1024 초고해상도에서 그린 뒤 LANCZOS로 256에 축소해 안티앨리어싱하고,
16px 트레이에서 읽히도록 단순·고대비 형태를 유지한다.
배경은 투명(RGBA). 딥 인디고~시안 브랜드 톤 + 흰 글리프.
"""
import math
import os

from PIL import Image, ImageDraw

SS = 1024  # supersample 크기
HERE = os.path.dirname(os.path.abspath(__file__))
OUT_PNG = os.path.join(HERE, "CalSync.png")
OUT_ICO = os.path.join(HERE, "CalSync.ico")

BODY_TOP = (63, 111, 234)   # #3f6fea
BODY_BOT = (30, 64, 175)    # #1e40af
HEADER = (37, 74, 190)      # #254abe
SYNC_A = (27, 208, 220)     # #1bd0dc
SYNC_B = (6, 140, 158)      # #068c9e
WHITE = (255, 255, 255)

# 캘린더 본체 박스 (8% 정도 안전 여백, 좌우·하단) ; 위는 링 탭이 살짝 돌출
BOX = (92, 130, 932, 898)          # (x0,y0,x1,y1)
R = 104                            # 모서리 반지름
DIVIDER_Y = 276                    # 헤더 밴드 하단 = 구분선 y


def rounded_polygon(box, radii, step=8):
    """화면 좌표(y 아래+) 시계 방향 폴리곤. radii=(tl,tr,br,bl), 0=직각."""
    x0, y0, x1, y1 = box
    r_tl, r_tr, r_br, r_bl = radii
    pts = []

    def arc(cx, cy, rr, a0, a1):
        for a in range(a0, a1 + 1, step):
            ar = math.radians(a)
            pts.append((cx + rr * math.cos(ar), cy + rr * math.sin(ar)))

    pts.append((x0 + r_tl, y0))
    pts.append((x1 - r_tr, y0))
    if r_tr:
        arc(x1 - r_tr, y0 + r_tr, r_tr, 270, 360)
    pts.append((x1, y0 + r_tr) if r_tr else (x1, y0))
    if r_br:
        arc(x1 - r_br, y1 - r_br, r_br, 0, 90)
    pts.append((x1 - r_br, y1) if r_br else (x1, y1))
    pts.append((x0 + r_bl, y1))
    if r_bl:
        arc(x0 + r_bl, y1 - r_bl, r_bl, 90, 180)
    pts.append((x0, y1 - r_bl) if r_bl else (x0, y1))
    if r_tl:
        arc(x0 + r_tl, y0 + r_tl, r_tl, 180, 270)
    return pts


def v_gradient_image(size, c0, c1):
    w, h = size
    img = Image.new("RGB", (w, h))
    px = img.load()
    for y in range(h):
        t = y / max(1, h - 1)
        r = int(c0[0] + (c1[0] - c0[0]) * t)
        g = int(c0[1] + (c1[1] - c0[1]) * t)
        b = int(c0[2] + (c1[2] - c0[2]) * t)
        for x in range(w):
            px[x, y] = (r, g, b)
    return img


def donut_sector(cx, cy, ro, ri, a0, sweep, fill, n=88):
    sweep %= 360
    pts = []
    for i in range(n + 1):
        a = math.radians(a0 + sweep * i / n)
        pts.append((cx + ro * math.cos(a), cy + ro * math.sin(a)))
    for i in range(n, -1, -1):
        a = math.radians(a0 + sweep * i / n)
        pts.append((cx + ri * math.cos(a), cy + ri * math.sin(a)))
    return pts


def arrowhead(cx, cy, ro, ri, a0, sweep, fill):
    end = (a0 + sweep) % 360
    er = math.radians(end)
    bx0 = (cx + ro * math.cos(er), cy + ro * math.sin(er))
    bx1 = (cx + ri * math.cos(er), cy + ri * math.sin(er))
    mx = (bx0[0] + bx1[0]) / 2
    my = (bx0[1] + bx1[1]) / 2
    dr = math.radians(end + 90)          # 이동 방향(탄젠트)
    length = (ro - ri) * 0.95
    tipx = mx + length * math.cos(dr)
    tipy = my + length * math.sin(dr)
    half = (ro - ri) * 0.30
    pr = er
    p1 = (tipx + half * math.cos(pr), tipy + half * math.sin(pr))
    p2 = (tipx - half * math.cos(pr), tipy - half * math.sin(pr))
    return [bx0, bx1, p2, p1]


def main():
    d = ImageDraw.Draw
    canvas = Image.new("RGBA", (SS, SS), (0, 0, 0, 0))

    # 1) 본체: 세로 라이트 그라데이션(딥 인디고)을 둥근 사각형으로 클리핑
    grad = v_gradient_image((SS, SS), BODY_TOP, BODY_BOT)
    mask = Image.new("L", (SS, SS), 0)
    d(mask).polygon(rounded_polygon(BOX, (R, R, R, R)), fill=255)
    canvas.paste(grad, (0, 0), mask)

    # 2) 헤더 밴드 (상단 둥글, 하단 직각) - 조금 더 짙은 인디고
    d(canvas).polygon(
        rounded_polygon((BOX[0], BOX[1], BOX[2], DIVIDER_Y), (R, R, 0, 0)),
        fill=HEADER)

    # 3) 헤더 밴드 하단 흰 구분선
    d(canvas).line(
        [(BOX[0] + 26, DIVIDER_Y), (BOX[2] - 26, DIVIDER_Y)],
        fill=WHITE, width=16)

    # 4) 링 탭 2개 (흰색, 캘린더 상단 경계에 걸침)
    for l, r in ((266, 326), (698, 758)):
        d(canvas).rounded_rectangle([l, 102, r, 178], radius=18, fill=WHITE)

    # 5) 동기화 원형 화살표 (cyan)
    cx = cy = SS // 2
    ro, ri = 168, 118
    for a0, sweep in ((340, 250), (160, 250)):
        d(canvas).polygon(donut_sector(cx, cy, ro, ri, a0, sweep, None), fill=SYNC_A)
        d(canvas).polygon(arrowhead(cx, cy, ro, ri, a0, sweep, None), fill=SYNC_B)

    # 256 축소 저장 (안티앨리어싱)
    img256 = canvas.resize((256, 256), Image.LANCZOS)
    img256.save(OUT_PNG)

    # 멀티사이즈 ICO
    img256.save(OUT_ICO, format="ICO", sizes=[
        (16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])

    # 디버그: 16px 축소본을 크게 확대해 확인
    small = canvas.resize((16, 16), Image.LANCZOS)
    small.resize((256, 256), Image.NEAREST).save(os.path.join(HERE, "_debug16.png"))

    print("PNG:", OUT_PNG)
    print("ICO:", OUT_ICO)
    with Image.open(OUT_ICO) as im:
        from PIL import IcoImagePlugin
        print("ICO embedded sizes:", sorted(im.info.get("sizes", [])) or "n/a")
    with Image.open(OUT_PNG) as im:
        print("PNG size/mode:", im.size, im.mode)


if __name__ == "__main__":
    main()
