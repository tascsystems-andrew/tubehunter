#!/usr/bin/env python3
"""Generate TubeHunter's app icon.

Draws a vacuum tube on a Big Sur-style rounded square: 1024x1024 canvas,
artwork on an 824x824 rounded rect (radius 186) centered with transparent
margin — the geometry macOS expects, so the Dock never shows square white
corners and the icon sits at the same visual size as its neighbours.

Renders at 2x and downsamples for antialiasing. Output:
    scripts/icon_1024.png       master
    TubeHunter.iconset/         all Dock/Finder sizes
Then:  iconutil -c icns TubeHunter.iconset -o TubeHunter.app/Contents/Resources/TubeHunter.icns
"""
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

HERE = Path(__file__).resolve().parent
S = 2048                 # supersampled canvas (final 1024)
M = 200                  # transparent margin (2x of 100)
R = 372                  # corner radius (2x of 186)


def radial(size, cx, cy, r, rgba_in, rgba_out):
    """Radial gradient tile as RGBA image."""
    y, x = np.ogrid[:size[1], :size[0]]
    d = np.sqrt((x - cx) ** 2 + (y - cy) ** 2) / r
    d = np.clip(d, 0, 1)
    out = np.zeros((size[1], size[0], 4), dtype=np.float32)
    for i in range(4):
        out[..., i] = rgba_in[i] + (rgba_out[i] - rgba_in[i]) * d
    return Image.fromarray(out.astype(np.uint8), "RGBA")


def vertical(size, rgba_top, rgba_bot):
    t = np.linspace(0, 1, size[1], dtype=np.float32)[:, None, None]
    top = np.array(rgba_top, dtype=np.float32)[None, None, :]
    bot = np.array(rgba_bot, dtype=np.float32)[None, None, :]
    grad = top + (bot - top) * t
    return Image.fromarray(np.broadcast_to(grad, (size[1], size[0], 4)).astype(np.uint8), "RGBA")


img = Image.new("RGBA", (S, S), (0, 0, 0, 0))

# ---- background: navy rounded square with a soft vignette --------------------
bg = vertical((S, S), (34, 47, 76, 255), (7, 11, 24, 255))
mask = Image.new("L", (S, S), 0)
ImageDraw.Draw(mask).rounded_rectangle([M, M, S - M, S - M], radius=R, fill=255)
img.paste(bg, (0, 0), mask)

# ambient warm pool behind the tube so the navy doesn't read flat
glow_bg = radial((S, S), S // 2, int(S * 0.56), int(S * 0.34),
                 (255, 170, 60, 70), (255, 170, 60, 0))
img.alpha_composite(glow_bg)
img = Image.composite(img, Image.new("RGBA", (S, S), (0, 0, 0, 0)), mask)

d = ImageDraw.Draw(img)
cx = S // 2

# ---- glass envelope ----------------------------------------------------------
glass_w = 620
gx0, gx1 = cx - glass_w // 2, cx + glass_w // 2
gy_top, gy_bot = 470, 1450
env = Image.new("RGBA", (S, S), (0, 0, 0, 0))
de = ImageDraw.Draw(env)
de.rounded_rectangle([gx0, gy_top - 310, gx1, gy_bot], radius=310,
                     fill=(150, 185, 225, 26))
# heater glow inside the glass
env.alpha_composite(radial((S, S), cx, 1060, 420, (255, 150, 40, 105), (255, 150, 40, 0)))
img.alpha_composite(env)

# ---- internals ---------------------------------------------------------------
# mica spacers — on their own layer so the alpha actually blends
mica = Image.new("RGBA", (S, S), (0, 0, 0, 0))
dm = ImageDraw.Draw(mica)
dm.ellipse([cx - 205, 596, cx + 205, 640], fill=(205, 210, 225, 70))
dm.ellipse([cx - 205, 1312, cx + 205, 1356], fill=(205, 210, 225, 55))
img.alpha_composite(mica)
# anode: two dark plates with the glowing gap between them
plate_h0, plate_h1 = 660, 1300
d.rounded_rectangle([cx - 150, plate_h0, cx - 34, plate_h1], radius=26, fill=(56, 62, 72, 235))
d.rounded_rectangle([cx + 34, plate_h0, cx + 150, plate_h1], radius=26, fill=(56, 62, 72, 235))
# heater filament in the gap
fil = radial((S, S), cx, (plate_h0 + plate_h1) // 2, 330, (255, 190, 90, 220), (255, 150, 40, 0))
fil_mask = Image.new("L", (S, S), 0)
ImageDraw.Draw(fil_mask).rounded_rectangle([cx - 26, plate_h0 + 15, cx + 26, plate_h1 - 15],
                                           radius=22, fill=255)
img.paste(fil, (0, 0), fil_mask)

# ---- getter flash at the dome — translucent silvery sheen ---------------------
getter = Image.new("RGBA", (S, S), (0, 0, 0, 0))
dg = ImageDraw.Draw(getter)
dg.ellipse([cx - 130, 352, cx + 130, 462], fill=(168, 178, 194, 88))
dg.ellipse([cx - 92, 372, cx + 92, 440], fill=(120, 130, 146, 96))
img.alpha_composite(getter.filter(ImageFilter.GaussianBlur(6)))

# ---- glass outline + specular ------------------------------------------------
outline = Image.new("RGBA", (S, S), (0, 0, 0, 0))
do = ImageDraw.Draw(outline)
do.rounded_rectangle([gx0, gy_top - 310, gx1, gy_bot], radius=310,
                     outline=(210, 228, 255, 90), width=7)
img.alpha_composite(outline)
spec = Image.new("RGBA", (S, S), (0, 0, 0, 0))
ImageDraw.Draw(spec).rounded_rectangle([gx0 + 55, gy_top - 240, gx0 + 130, gy_bot - 130],
                                       radius=38, fill=(255, 255, 255, 42))
img.alpha_composite(spec.filter(ImageFilter.GaussianBlur(16)))

# ---- bakelite base -----------------------------------------------------------
d.rounded_rectangle([cx - 360, 1450, cx + 360, 1620], radius=34, fill=(16, 16, 18, 255))
d.rounded_rectangle([cx - 360, 1450, cx + 360, 1520], radius=34, fill=(28, 28, 32, 255))
# gold pin stubs
for px in range(-3, 4):
    x = cx + px * 100
    d.rounded_rectangle([x - 14, 1620, x + 14, 1700], radius=12, fill=(201, 160, 74, 255))

# clip everything back inside the rounded square + finalize
img = Image.composite(img, Image.new("RGBA", (S, S), (0, 0, 0, 0)), mask)
master = img.resize((1024, 1024), Image.LANCZOS)
master.save(HERE / "icon_1024.png")

# ---- iconset -----------------------------------------------------------------
iconset = HERE.parent / "TubeHunter.iconset"
iconset.mkdir(exist_ok=True)
for size in (16, 32, 128, 256, 512):
    master.resize((size, size), Image.LANCZOS).save(iconset / f"icon_{size}x{size}.png")
    master.resize((size * 2, size * 2), Image.LANCZOS).save(iconset / f"icon_{size}x{size}@2x.png")
print(f"wrote {HERE / 'icon_1024.png'} and {iconset}/")
