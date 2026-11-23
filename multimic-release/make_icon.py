"""Draws the MultiMic icon (two studio microphones) and writes multimic.ico + multimic.png."""
from PIL import Image, ImageDraw, ImageFilter

S = 1024                       # draw big, then shrink for smooth edges


def gradient(size, top, bottom):
    img = Image.new("RGB", (size, size))
    px = img.load()
    for y in range(size):
        t = y / (size - 1)
        c = tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3))
        for x in range(size):
            px[x, y] = c
    return img


def mic(d, cx, top):
    """One studio condenser mic on a stand, centred on cx."""
    w, h = 200, 390
    x0, x1, y0, y1 = cx - w // 2, cx + w // 2, top, top + h
    # shock-mount cradle (amber U) behind the capsule
    d.arc([cx - 150, y0 + 110, cx + 150, y1 + 190], 0, 180, fill=(245, 166, 35), width=30)
    d.rounded_rectangle([cx - 168, y0 + 250, cx - 138, y0 + 300], 12, fill=(245, 166, 35))
    d.rounded_rectangle([cx + 138, y0 + 250, cx + 168, y0 + 300], 12, fill=(245, 166, 35))
    # capsule
    d.rounded_rectangle([x0, y0, x1, y1], w // 2, fill=(226, 233, 242))
    for k in range(7):                                  # grille lines
        y = y0 + 70 + k * 42
        d.line([x0 + 28, y, x1 - 28, y], fill=(150, 168, 190), width=10)
    d.rounded_rectangle([x0 + 26, y0 + 30, x0 + 52, y1 - 70], 12, fill=(250, 252, 255))  # highlight
    # stem and base
    d.rounded_rectangle([cx - 20, y1 + 150, cx + 20, y1 + 290], 8, fill=(226, 233, 242))
    d.rounded_rectangle([cx - 105, y1 + 270, cx + 105, y1 + 320], 22, fill=(226, 233, 242))


def draw():
    bg = gradient(S, (38, 78, 128), (11, 24, 42)).convert("RGBA")
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle([24, 24, S - 24, S - 24], 210, fill=255)
    out = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    out.paste(bg, (0, 0), mask)
    layer = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    mic(d, 316, 130)
    mic(d, 708, 130)
    shadow = layer.filter(ImageFilter.GaussianBlur(14))
    shadow = Image.eval(shadow, lambda v: v // 3) if False else shadow
    out.alpha_composite(Image.new("RGBA", (S, S), (0, 0, 0, 0)))
    out.alpha_composite(layer)
    return out


if __name__ == "__main__":
    big = draw()
    big.resize((512, 512), Image.LANCZOS).save("multimic.png")
    sizes = [16, 24, 32, 48, 64, 128, 256]
    big.save("multimic.ico", sizes=[(s, s) for s in sizes])
    for s in (256, 64, 32, 16):
        big.resize((s, s), Image.LANCZOS).save(f"/tmp/icon_{s}.png")
    print("wrote multimic.ico, multimic.png")
