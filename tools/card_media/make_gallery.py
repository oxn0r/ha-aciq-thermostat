"""One looping GIF per system type, every state it can show in a 2-column grid:
python3 make_gallery.py frames ../../docs/images/systems (needs Pillow). Frames come from gallery.html:

    let i = 0;
    for (const [sys, k, c] of cards) await captureAll(`g_${theme}__${String(i++).padStart(2, "0")}_${sys}__${k}`, c, 10, 3, 1);
"""
import re
import sys
from collections import defaultdict
from pathlib import Path

from PIL import Image, ImageChops

BG = {"light": (250, 250, 250), "dark": (17, 17, 17)}


def cropped(paths, theme, margin):
    """Frames on the page background, all cut to the union of their content boxes."""
    ims = []
    for p in paths:
        im = Image.open(p).convert("RGBA")
        ims.append(Image.alpha_composite(Image.new("RGBA", im.size, BG[theme] + (255,)), im).convert("RGB"))
    box = None
    for im in ims:
        b = ImageChops.difference(im, Image.new("RGB", im.size, BG[theme])).getbbox()
        box = b if box is None else (min(box[0], b[0]), min(box[1], b[1]), max(box[2], b[2]), max(box[3], b[3]))
    res = []
    for im in ims:
        c = Image.new("RGB", (box[2] - box[0] + 2 * margin, box[3] - box[1] + 2 * margin), BG[theme])
        c.paste(im.crop(box), (margin, margin))
        res.append(c)
    return res

src, out = Path(sys.argv[1]), Path(sys.argv[2])
out.mkdir(parents=True, exist_ok=True)
WIDTH = 900

groups: dict[tuple[str, str], dict[str, list[Path]]] = defaultdict(lambda: defaultdict(list))
for p in sorted(src.glob("g_*.png")):      # sorted: the card index keeps the gallery's order
    theme, system, state = re.match(r"g_(light|dark)__\d{2}_(\w+)__(\w+)_\d{3}\.png", p.name).groups()
    groups[(theme, system)][state].append(p)

for (theme, system), states in groups.items():
    names = list(states)                      # capture order = the gallery's order
    every = cropped([p for k in names for p in states[k]], theme, 14)   # one crop box: cells line up
    seqs, i = [], 0
    for k in names:
        seqs.append(every[i:i + len(states[k])])
        i += len(states[k])
    n = max(len(s) for s in seqs)
    w, h = every[0].size
    cols = 2 if len(names) > 1 else 1
    rows = -(-len(names) // cols)
    frames = []
    for f in range(n):
        g = Image.new("RGB", (w * cols, h * rows), BG[theme])
        for j, s in enumerate(seqs):
            g.paste(s[f % len(s)], ((j % cols) * w, (j // cols) * h))
        width = WIDTH if cols == 2 else WIDTH // 2
        frames.append(g.resize((width, round(width * g.height / g.width)), Image.Resampling.LANCZOS))
    fw, fh = frames[0].size
    sample = Image.new("RGB", (fw, fh * 4))
    for j, f in enumerate(frames[:: max(1, n // 4)][:4]):
        sample.paste(f, (0, fh * j))
    pal = sample.quantize(colors=128, method=Image.Quantize.MEDIANCUT)
    q = [f.quantize(palette=pal, dither=Image.Dither.NONE) for f in frames]
    name = f"{system.replace('_', '-')}{'-dark' if theme == 'dark' else ''}.gif"
    q[0].save(out / name, save_all=True, append_images=q[1:], duration=100, loop=0, optimize=True)
    print(name, f"{fw}x{fh}", len(names), "states", (out / name).stat().st_size // 1024, "KB")
