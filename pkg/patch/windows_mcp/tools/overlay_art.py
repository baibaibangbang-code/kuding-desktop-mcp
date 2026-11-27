# -*- coding: utf-8 -*-
"""提示层的画面：用 PIL 画，不用 GDI。

为什么换掉 GDI：GDI 没有抗锯齿，画出来的是**直角硬边**——
9-16 维护者拍了 Codex 的提示层来对比，差距一眼可见：
它是圆角胶囊 + 柔和光晕，我们是直角矩形 + 实心硬圆。
同一天抓我们自己的横幅还发现一个**真 bug**：横幅宽度写死 460px，
而文案是变长的，于是两头各被切掉一截（「uia-desktop…」的 ui 和「按 Esc 停下」的下）。
一个把话说到一半的提示，比不提示更糟。

PIL 我们本来就依赖，画完用 UpdateLayeredWindow 贴上去，
能拿到**逐像素 alpha**：圆角、阴影、渐变光晕全都平滑。
而且这个模块同时给预览脚本用，所以**看到的就是将来贴上去的那些像素**，
不是效果图——设计稿到实现之间那一道翻译，正是失真的地方。
"""
import os

from PIL import Image, ImageDraw, ImageFilter, ImageFont

# 中文界面字体。msyh.ttc 是微软雅黑，Win 上一定有；找不到就退到 PIL 自带的。
_FONTS = (
    r"C:\Windows\Fonts\msyhbd.ttc",      # 雅黑粗体：小字号下更清楚
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\segoeuib.ttf",
)

# 三套配色。COLORREF 那套 BGR 的坑在这里不存在——PIL 一律 RGB。
STYLES = {
    # A：实心胶囊。和 Codex 同一路数，最直白，远处也看得见。
    "solid": {
        "bg": (26, 132, 255, 255),
        "fg": (255, 255, 255, 255),
        "sub": (255, 255, 255, 178),
        "dot": None,
        "edge": (255, 255, 255, 46),
    },
    # B：深色玻璃。不抢画面，长时间挂着不烦人；左边一颗呼吸点表示"活着"。
    "glass": {
        "bg": (22, 24, 28, 232),
        "fg": (255, 255, 255, 240),
        "sub": (255, 255, 255, 150),
        "dot": (26, 132, 255, 255),
        "edge": (255, 255, 255, 38),
    },
    # C：深底 + 左侧强调色端帽。把"谁在动"和"在做什么"分开，信息层次最清楚。
    "capsule": {
        "bg": (22, 24, 28, 236),
        "fg": (255, 255, 255, 240),
        "sub": (255, 255, 255, 150),
        "dot": None,
        "cap": (26, 132, 255, 255),
        "edge": (255, 255, 255, 38),
    },
}


def _font(px):
    for p in _FONTS:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, px)
            except Exception:
                continue
    return ImageFont.load_default()


def _shadow(size, box, radius, blur=10, alpha=90, dy=3):
    """在 size 大小的画布上，为 box 这个圆角矩形画一层投影。"""
    sh = Image.new("RGBA", size, (0, 0, 0, 0))
    d = ImageDraw.Draw(sh)
    x0, y0, x1, y1 = box
    d.rounded_rectangle((x0, y0 + dy, x1, y1 + dy), radius=radius,
                        fill=(0, 0, 0, alpha))
    return sh.filter(ImageFilter.GaussianBlur(blur))


def banner_image(main, tail="", style="glass", h=46, scale=1.0):
    """把一行提示画成圆角胶囊。**宽度按文字算**，不写死——这是之前被截断的原因。

    main 是主文案，tail 是次要的那半（比如「按 Esc 停下」），用更淡的颜色，
    让人一眼分出"在干什么"和"你能做什么"。
    """
    st = STYLES.get(style) or STYLES["glass"]
    h = int(h * scale)
    f_main = _font(int(h * 0.40))
    f_tail = _font(int(h * 0.36))

    pad_x = int(h * 0.62)
    gap = int(h * 0.42)
    dot_r = int(h * 0.11)
    cap_w = int(h * 0.30) if st.get("cap") else 0

    probe = ImageDraw.Draw(Image.new("RGBA", (8, 8)))
    w_main = probe.textlength(main, font=f_main)
    w_tail = probe.textlength(tail, font=f_tail) if tail else 0
    lead = (dot_r * 2 + gap) if st.get("dot") else 0
    inner = lead + w_main + ((gap + w_tail) if tail else 0)
    w = int(cap_w + pad_x * 2 + inner)

    # 画布留出投影的余量
    m = int(h * 0.5)
    im = Image.new("RGBA", (w + m * 2, h + m * 2), (0, 0, 0, 0))
    box = (m, m, m + w, m + h)
    radius = h // 2

    im.alpha_composite(_shadow(im.size, box, radius, blur=int(h * 0.26),
                               alpha=96, dy=int(h * 0.09)))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle(box, radius=radius, fill=st["bg"])
    if st.get("edge"):
        # 一圈极淡的内描边：贴在深色桌面上时不会糊成一块
        d.rounded_rectangle(box, radius=radius, outline=st["edge"], width=1)
    if cap_w:
        cap = Image.new("RGBA", im.size, (0, 0, 0, 0))
        cd = ImageDraw.Draw(cap)
        cd.rounded_rectangle(box, radius=radius, fill=st["cap"])
        mask = Image.new("L", im.size, 0)
        ImageDraw.Draw(mask).rectangle((m, m, m + cap_w, m + h), fill=255)
        im.paste(cap, (0, 0), mask)

    x = m + cap_w + pad_x
    cy = m + h / 2.0
    if st.get("dot"):
        d.ellipse((x, cy - dot_r, x + dot_r * 2, cy + dot_r), fill=st["dot"])
        x += dot_r * 2 + gap
    d.text((x, cy), main, font=f_main, fill=st["fg"], anchor="lm")
    if tail:
        x += w_main + gap
        d.text((x, cy), tail, font=f_tail, fill=st["sub"], anchor="lm")
    return im


def screen_frame(w, h, thick=3, radius=0, color=(26, 132, 255), alpha=235):
    """全屏外框：整块屏幕镶一道边，会话期间全程挂着。

    9-16 维护者提的，理由很实在：**这一层是给人看的**——
    窗口外框回答"AI 在动哪个窗口"，全屏外框回答"这台电脑现在归不归我"。
    后者才是坐在电脑前的人第一眼要的答案，而且它必须全程在，
    否则人得盯着才能确认，那就等于没有。

    做得淡：全程挂着的东西不能抢画面。内侧再压一道更淡的，
    边缘就不会是一条生硬的线。
    """
    im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    # 最外面几圈**接近实色**，再往内才衰减。
    #
    # 第一版我把整条边都做成渐隐的淡蓝，量出来底边确实画在任务栏上了
    # （y=1594~1599 是蓝的），可**在浅色任务栏上几乎看不出来**——
    # 9-16 维护者说"全屏外框要包含底部快捷栏"，其实说的是这个：
    # 画到了但看不见，跟没画是一回事。
    # 全程挂着的东西要淡，但"淡"不能淡到在浅背景上消失。
    # **整条厚度都用实色**，再往里才渐隐。
    #
    # 上一版 thick=4 只让最外 2 像素是实色、其余渐隐，结果是：
    # 像素测得到（四边确实是蓝的），但人眼和照片里基本看不出来——
    # 9-16 维护者看到窗口外框却看不到全屏外框，就是这个原因。
    # "画到了" 和 "看得见" 是两件事，而这一层的全部意义就是给人看。
    solid = thick
    for i in range(thick + thick * 3):
        if i < solid:
            a = alpha
        else:
            t = (i - solid) / float(max(1, thick * 3))
            a = int(alpha * (1.0 - t) ** 1.6 * 0.55)
        if a <= 0:
            continue
        d.rounded_rectangle((i, i, w - 1 - i, h - 1 - i), radius=max(0, radius - i),
                            outline=color + (a,), width=1)
    return im


def glow_image(d=112, color=(40, 130, 235), core=245):
    """光标光晕：一团**柔和的实心光**，中心最亮、长尾散开。

    9-16 把 Codex 那张照片放大 4 倍逐像素看过之后重做的。看之前我猜错了两次：
    先做成"中心最亮的小硬团"，又改成"环形、中心留白"——**都不对**。
    它其实是一团实心高斯光晕：中心最亮、没有空心，只是散得很开，
    半径大约是光标高度的两倍半，边缘完全化掉、没有可见轮廓。

    它还**自己画了光标**（黑箭头 + 厚白描边），所以压在蓝光上依然清楚。
    我们不画：Codex 画自己的是因为它把系统光标藏了，
    我们没藏，再画一个就是两个箭头重影——那比糊一点更糟。

    参数是照着那张照片比出来的：sigma=0.38 的高斯 + (1-r)^0.9 收尾，
    peak 180 让它在深灰和浅灰底上都不刺眼。
    """
    import math
    im = Image.new("RGBA", (d, d), (0, 0, 0, 0))
    px = im.load()
    c = d / 2.0
    sigma = 0.38
    for y in range(d):
        for x in range(d):
            r = ((x - c + 0.5) ** 2 + (y - c + 0.5) ** 2) ** 0.5 / c
            if r >= 1.0:
                continue
            a = math.exp(-(r * r) / (2 * sigma * sigma)) * (1 - r) ** 0.9
            px[x, y] = (color[0], color[1], color[2], int(min(1.0, a) * core))
    return im.filter(ImageFilter.GaussianBlur(d * 0.06))


def frame_edges(w, h, thick=3, radius=14, color=(26, 132, 255), alpha=210,
                soft=True):
    """窗口外框：圆角 + 外侧柔光。返回整块 RGBA（调用方按需切边贴）。

    直角框在圆角窗口上尤其难看——Win11 的窗口本身就是圆角的，
    我们套一个直角框上去，四个角会各露出一块。
    """
    pad = int(thick * 3) if soft else 0
    im = Image.new("RGBA", (w + pad * 2, h + pad * 2), (0, 0, 0, 0))
    box = (pad, pad, pad + w - 1, pad + h - 1)
    if soft:
        g = Image.new("RGBA", im.size, (0, 0, 0, 0))
        ImageDraw.Draw(g).rounded_rectangle(box, radius=radius,
                                            outline=color + (120,),
                                            width=thick + pad)
        im.alpha_composite(g.filter(ImageFilter.GaussianBlur(pad * 0.8)))
    ImageDraw.Draw(im).rounded_rectangle(box, radius=radius,
                                         outline=color + (alpha,), width=thick)
    return im, pad
