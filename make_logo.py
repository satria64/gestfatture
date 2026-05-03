"""Genera un logo PNG 512x512 per GestFatture."""
from PIL import Image, ImageDraw, ImageFont

W = H = 512
NAVY  = (30, 58, 95)        # #1e3a5f
BLUE  = (37, 99, 235)       # #2563eb
WHITE = (255, 255, 255)
GRAY  = (203, 213, 225)

img  = Image.new("RGB", (W, H), NAVY)
draw = ImageDraw.Draw(img)

# Bordo arrotondato simulato con angoli
img2 = Image.new("RGBA", (W, H), (0, 0, 0, 0))
d2   = ImageDraw.Draw(img2)
d2.rounded_rectangle((0, 0, W, H), radius=88, fill=NAVY)

# Accento blu in alto a destra
d2.rounded_rectangle((W - 180, 0, W, 180), radius=88, fill=BLUE)
d2.rectangle((W - 180, 88, W - 88, 180), fill=BLUE)
d2.rectangle((W - 180, 0, W - 88, 88), fill=BLUE)

# Font (prova Arial Bold, fallback al default)
def load_font(size):
    for name in ("arialbd.ttf", "arial.ttf", "Helvetica-Bold.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()

font_big   = load_font(290)
font_small = load_font(48)

# "GF" al centro
text = "GF"
bbox = d2.textbbox((0, 0), text, font=font_big)
tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
d2.text(((W - tw) / 2 - bbox[0], (H - th) / 2 - bbox[1] - 30),
        text, fill=WHITE, font=font_big)

# "GestFatture" sotto
subtitle = "GestFatture"
bbox = d2.textbbox((0, 0), subtitle, font=font_small)
tw = bbox[2] - bbox[0]
d2.text(((W - tw) / 2 - bbox[0], H - 100),
        subtitle, fill=GRAY, font=font_small)

img2.save("logo.png", "PNG")
print("✅ Logo creato: logo.png (512x512)")
