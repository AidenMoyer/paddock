"""Render the Paddock logo (same geometry as server/paddock/web/static/icon.svg) into the desktop app icons.

    python design/gen_icons.py

Outputs android/desktop/icons/paddock.png (512 px) and paddock.ico (16-256 px).
"""

from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "android" / "desktop" / "icons"
TOP, BOTTOM = (0xFF, 0x7A, 0x2F), (0xE5, 0x24, 0x3B)


def logo(size: int) -> Image.Image:
    ss = 4                                  # draw large, then downsample for smooth edges
    n = size * ss
    u = n / 64                              # one SVG unit
    grad = Image.new("RGB", (n, n))
    px = grad.load()
    for y in range(n):
        for x in range(n):
            t = (x + y) / (2 * (n - 1))     # the SVG's diagonal gradient
            px[x, y] = tuple(round(a + (b - a) * t) for a, b in zip(TOP, BOTTOM))
    mask = Image.new("L", (n, n), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, n - 1, n - 1), radius=16 * u, fill=255)
    img = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    img.paste(grad, (0, 0), mask)

    p = Image.new("L", (n, n), 0)
    d = ImageDraw.Draw(p)
    d.rounded_rectangle((18 * u, 14 * u, 29 * u, 51 * u), radius=3 * u, fill=255)        # stem
    d.rectangle((21 * u, 14 * u, 38 * u, 40 * u), fill=255)                               # bowl body
    d.pieslice((25 * u, 14 * u, 51 * u, 40 * u), 270, 90, fill=255)                       # round of the bowl
    d.rectangle((29 * u, 21 * u, 35 * u, 27 * u), fill=0)                                 # checkered window
    d.rectangle((35 * u, 27 * u, 41 * u, 33 * u), fill=0)
    white = Image.new("RGBA", (n, n), (255, 255, 255, 255))
    img.paste(white, (0, 0), p)
    return img.resize((size, size), Image.LANCZOS)


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    big = logo(512)
    big.save(OUT / "paddock.png")
    big.save(OUT / "paddock.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print(f"wrote {OUT.relative_to(ROOT)}")
