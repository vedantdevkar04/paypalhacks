"""Draws the demo shop's product photos: flat illustrations we made ourselves, so we own them.
Run once after changing the catalog (needs Pillow, which the app itself doesn't):

    pip install pillow
    python fixtures/make_product_photos.py

Writes JPEGs to hiclose/api/static/products/, which the app serves at /products/<file>.
WhatsApp only accepts JPEG or PNG, so these are not SVG."""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

OUT = Path(__file__).resolve().parents[1] / "hiclose" / "api" / "static" / "products"
S = 1600  # drawn at 2x, then downscaled for smooth edges
FONT = "C:/Windows/Fonts/georgia.ttf"


def font(size):
    try:
        return ImageFont.truetype(FONT, size)
    except OSError:
        return ImageFont.load_default(size)


def canvas(bg):
    image = Image.new("RGB", (S, S), bg)
    return image, ImageDraw.Draw(image)


def label(draw, name, ink):
    draw.text((S // 2, S - 120), name, fill=ink, font=font(64), anchor="mm")
    draw.text((S // 2, S - 50), "INDIGO LANE", fill=ink, font=font(34), anchor="mm")


def jacket():
    image, d = canvas((222, 230, 240))
    denim, dark, seam, brass = (74, 104, 150), (52, 76, 114), (150, 176, 210), (196, 156, 72)
    d.ellipse((360, 1250, 1240, 1330), fill=(196, 206, 220))
    # sleeves
    d.polygon([(520, 380), (330, 470), (230, 1180), (370, 1200), (470, 700)], fill=dark)
    d.polygon([(1080, 380), (1270, 470), (1370, 1180), (1230, 1200), (1130, 700)], fill=dark)
    d.rectangle((228, 1130, 372, 1210), fill=denim)
    d.rectangle((1228, 1130, 1372, 1210), fill=denim)
    # body
    d.polygon([(520, 370), (1080, 370), (1150, 520), (1150, 1240), (450, 1240), (450, 520)], fill=denim)
    d.rectangle((450, 1150, 1150, 1240), fill=dark)
    # collar
    d.polygon([(600, 340), (800, 470), (1000, 340), (1070, 380), (820, 640), (800, 600), (780, 640), (530, 380)], fill=dark)
    d.line([(800, 600), (800, 1240)], fill=seam, width=8)
    # chest pockets
    for x in (560, 880):
        d.rectangle((x, 640, x + 160, 800), outline=seam, width=8)
        d.polygon([(x, 640), (x + 160, 640), (x + 160, 690), (x + 80, 720), (x, 690)], fill=dark)
        d.ellipse((x + 66, 676, x + 94, 704), fill=brass)
    # buttons
    for y in (560, 760, 960, 1180):
        d.ellipse((828, y - 18, 864, y + 18), fill=brass)
    label(d, "Washed Denim Jacket", (40, 56, 86))
    return image


def tote():
    image, d = canvas((240, 228, 210))
    canvas_c, ink, strap = (232, 214, 184), (47, 58, 143), (196, 170, 130)
    d.ellipse((380, 1260, 1220, 1340), fill=(216, 202, 180))
    d.arc((560, 220, 1040, 760), 180, 360, fill=strap, width=56)
    d.polygon([(420, 520), (1180, 520), (1230, 1290), (370, 1290)], fill=canvas_c)
    d.line([(420, 520), (1180, 520)], fill=strap, width=20)
    # block print: little indigo flowers in a grid
    for row in range(5):
        for col in range(5):
            cx = 520 + col * 140 + (70 if row % 2 else 0)
            cy = 640 + row * 140
            if cx > 1130:
                continue
            for dx, dy in ((0, -26), (26, 0), (0, 26), (-26, 0)):
                d.ellipse((cx + dx - 20, cy + dy - 20, cx + dx + 20, cy + dy + 20), fill=ink)
            d.ellipse((cx - 12, cy - 12, cx + 12, cy + 12), fill=canvas_c)
    label(d, "Block-Print Canvas Tote", (70, 54, 36))
    return image


def scarf():
    image, d = canvas((222, 216, 238))
    indigo, light, edge = (40, 48, 120), (120, 128, 200), (230, 226, 245)
    d.ellipse((380, 1250, 1220, 1330), fill=(200, 194, 220))
    c, r = (800, 760), 470
    diamond = [(c[0], c[1] - r), (c[0] + r, c[1]), (c[0], c[1] + r), (c[0] - r, c[1])]
    d.polygon(diamond, fill=indigo)
    inner = [(c[0], c[1] - r + 70), (c[0] + r - 70, c[1]), (c[0], c[1] + r - 70), (c[0] - r + 70, c[1])]
    d.polygon(inner, outline=edge, width=10)
    # shibori-style rings
    for k, radius in enumerate((260, 180, 100)):
        d.ellipse((c[0] - radius, c[1] - radius, c[0] + radius, c[1] + radius), outline=light if k % 2 == 0 else edge, width=14)
    d.ellipse((c[0] - 30, c[1] - 30, c[0] + 30, c[1] + 30), fill=edge)
    # folded corner
    d.polygon([(c[0] + r, c[1]), (c[0] + r - 150, c[1] + 150), (c[0] + r - 150, c[1])], fill=(70, 80, 160))
    label(d, "Indigo Silk Scarf", (36, 34, 80))
    return image


def shirt():
    image, d = canvas((220, 232, 216))
    linen, shade, line_c, button = (244, 240, 228), (220, 214, 198), (200, 194, 178), (150, 116, 80)
    d.ellipse((360, 1250, 1240, 1330), fill=(196, 210, 192))
    d.polygon([(540, 380), (330, 520), (260, 820), (420, 860), (470, 640)], fill=shade)
    d.polygon([(1060, 380), (1270, 520), (1340, 820), (1180, 860), (1130, 640)], fill=shade)
    d.polygon([(540, 370), (1060, 370), (1150, 540), (1150, 1250), (450, 1250), (450, 540)], fill=linen)
    # camp collar: open, flat lapels
    d.polygon([(620, 350), (800, 560), (980, 350), (1060, 380), (860, 720), (800, 620), (740, 720), (540, 380)], fill=shade)
    d.polygon([(680, 350), (800, 500), (920, 350)], fill=(220, 232, 216))
    d.line([(800, 620), (800, 1250)], fill=line_c, width=8)
    for y in (720, 880, 1040, 1200):
        d.ellipse((812, y - 20, 852, y + 20), fill=button)
    d.rectangle((900, 760, 1040, 900), outline=line_c, width=8)
    label(d, "Relaxed Linen Shirt", (44, 70, 44))
    return image


PHOTOS = {"jacket-denim": jacket, "tote-canvas": tote, "scarf-silk": scarf, "shirt-linen": shirt}


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    for product_id, draw in PHOTOS.items():
        path = OUT / f"{product_id}.jpg"
        draw().resize((800, 800), Image.LANCZOS).save(path, quality=86, optimize=True)
        print("wrote", path.relative_to(OUT.parents[3]), path.stat().st_size // 1024, "KB")
