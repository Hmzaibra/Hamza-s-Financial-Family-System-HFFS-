"""Draw the app icons from the same shape the topbar draws.

Run once; the PNGs are committed. Kept as a script rather than a note in a
README because the mark is geometry — a rounded square and four bars — and
geometry regenerates exactly, where a hand-edited PNG diverges from the topbar
the first time the brand colour moves.

    python make_icons.py

No font is loaded on purpose. A letter drawn with `ImageDraw.text` depends on
whatever font the machine happens to have, so the "E" here is four rectangles
with the proportions of the CSS mark. It also means this runs on the Pi.

Two shapes come out of it:

  * the plain icon, drawn edge to edge with its own rounded corners, which is
    what a browser tab and the older parts of Android want
  * the maskable one, drawn small inside a full-bleed square, because Android
    crops a maskable icon to whatever silhouette the launcher is using and
    anything within about 10% of the edge is not guaranteed to survive

iOS takes the 180px file and rounds the corners itself, so that one is opaque
and square — a transparent apple-touch-icon comes out black.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent / "static" / "img"

PRIMARY = (31, 111, 99, 255)      # --primary #1F6F63
INK = (245, 248, 246, 255)        # the mark's letter, --accent-ink at rest

# Supersampled and shrunk, which is how you get a smooth rounded corner and a
# clean bar edge out of Pillow without antialiasing controls.
SCALE = 4


def mark(size: int, shape: str = "rounded", letter: float = 0.42) -> Image.Image:
    """The tile with the E in it, at `size` pixels.

    `shape` is what the background does: "rounded" draws its own corners,
    "square" fills the canvas (iOS rounds it itself), "bleed" fills the canvas
    for a maskable icon, where a launcher crops to its own silhouette and only
    the middle 80% is guaranteed to survive.

    `letter` is the E's width as a fraction of the canvas. The maskable one is
    drawn larger, not smaller: the crop eats the edges, so a letter sized for
    the plain tile ends up a stamp in the middle of a field of green.
    """
    big = size * SCALE
    canvas = Image.new("RGBA", (big, big),
                       (0, 0, 0, 0) if shape == "rounded" else PRIMARY)
    draw = ImageDraw.Draw(canvas)

    if shape == "rounded":
        draw.rounded_rectangle((0, 0, big - 1, big - 1),
                               radius=round(big * 0.22), fill=PRIMARY)

    # The E: a spine and three arms, the middle one shorter, in the proportions
    # of the topbar mark rather than invented ones, so the two read as one
    # thing. Centred by measurement, not by a padding constant that stops being
    # centred the moment `letter` moves.
    width = big * letter
    height = width * 1.2
    thick = width * 0.225
    short_arm = width * 0.75
    left = (big - width) / 2
    top = (big - height) / 2

    draw.rectangle((left, top, left + thick, top + height), fill=INK)
    for y, length in ((top, width),
                      (top + (height - thick) / 2, short_arm),
                      (top + height - thick, width)):
        draw.rectangle((left, y, left + length, y + thick), fill=INK)

    return canvas.resize((size, size), Image.LANCZOS)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)

    mark(192).save(OUT / "icon-192.png")
    mark(512).save(OUT / "icon-512.png")
    mark(512, shape="bleed", letter=0.44).save(OUT / "icon-maskable-512.png")

    # iOS composites onto black rather than white, so this one carries no alpha
    # at all — a transparent apple-touch-icon is a black square on the home
    # screen, which is the sort of thing nobody notices until it is installed.
    mark(180, shape="square").convert("RGB").save(OUT / "apple-touch-icon.png")

    for path in sorted(OUT.glob("*.png")):
        print(f"  {path.name}  {path.stat().st_size:,} bytes")


if __name__ == "__main__":
    main()
