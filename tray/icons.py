"""Tray icon rendering: a coloured tile with the battery percentage on it."""

import os

from PIL import Image, ImageDraw, ImageFont

GREEN = (46, 158, 91, 255)
AMBER = (217, 154, 30, 255)
RED = (214, 69, 65, 255)
GREY = (110, 110, 118, 255)
WHITE = (255, 255, 255, 255)
BOLT = (255, 214, 10, 255)
OUTLINE = (20, 20, 22, 255)

_FONT_CANDIDATES = [
    r"C:\Windows\Fonts\segoeuib.ttf",
    r"C:\Windows\Fonts\arialbd.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]
_font_cache = {}


def _font(size):
    if size in _font_cache:
        return _font_cache[size]
    f = None
    for path in _FONT_CANDIDATES:
        if os.path.exists(path):
            try:
                f = ImageFont.truetype(path, size)
                break
            except OSError:
                pass
    if f is None:
        f = ImageFont.load_default(size)
    _font_cache[size] = f
    return f


def level_colour(percent, stale=False):
    if percent is None or stale:
        return GREY
    if percent <= 15:
        return RED
    if percent <= 35:
        return AMBER
    return GREEN


def _fit_text(draw, text, box_w, box_h, start):
    size = start
    while size > 8:
        f = _font(size)
        l, t, r, b = draw.textbbox((0, 0), text, font=f)
        if r - l <= box_w and b - t <= box_h:
            return f, (l, t, r, b)
        size -= 2
    f = _font(size)
    return f, draw.textbbox((0, 0), text, font=f)


def _bolt(draw, x, y, s):
    # small lightning bolt, top-left at (x, y), size s
    pts = [(x + 0.55 * s, y), (x + 0.1 * s, y + 0.58 * s), (x + 0.45 * s, y + 0.58 * s),
           (x + 0.3 * s, y + s), (x + 0.9 * s, y + 0.38 * s), (x + 0.55 * s, y + 0.38 * s),
           (x + 0.75 * s, y)]
    draw.polygon(pts, fill=BOLT, outline=OUTLINE)


def tray_icon(percent, charging=False, stale=False, size=64):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    r = size // 6
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=r, fill=level_colour(percent, stale))
    text = "--" if percent is None else str(percent)
    pad = size * 0.06
    f, (l, t, rr, b) = _fit_text(d, text, size - 2 * pad, size * 0.78, int(size * 0.8))
    w, h = rr - l, b - t
    d.text(((size - w) / 2 - l, (size - h) / 2 - t), text, font=f, fill=WHITE)
    if charging:
        s = size * 0.42
        _bolt(d, size - s - 1, 1, s)
    return img


if __name__ == "__main__":
    # preview strip for checking by eye
    samples = [(100, False, False), (87, True, False), (42, False, False),
               (23, False, False), (9, False, False), (64, False, True), (None, False, False)]
    big = Image.new("RGBA", (len(samples) * 72, 160), (32, 32, 36, 255))
    for i, (pc, ch, st) in enumerate(samples):
        big.paste(tray_icon(pc, ch, st, 64), (i * 72 + 4, 4))
        small = tray_icon(pc, ch, st, 64).resize((24, 24), Image.LANCZOS)
        big.paste(small, (i * 72 + 24, 80), small)
        small16 = tray_icon(pc, ch, st, 64).resize((16, 16), Image.LANCZOS)
        big.paste(small16, (i * 72 + 28, 120), small16)
    big.save("icon_preview.png")
