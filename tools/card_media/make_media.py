"""Crop the captured frames and build the docs images: python3 make_media.py frames ../../docs/images (needs Pillow)."""
import sys
from pathlib import Path
from PIL import Image, ImageChops
src, out = Path(sys.argv[1]), Path(sys.argv[2])
BG = {"light": (250, 250, 250), "dark": (17, 17, 17)}

def cropped(paths, theme, margin):
    ims = []
    for p in paths:
        im = Image.open(p).convert("RGBA")
        ims.append(Image.alpha_composite(Image.new("RGBA", im.size, BG[theme] + (255,)), im).convert("RGB"))
    box = None
    for im in ims:  # union of content boxes, so every frame gets the same crop
        b = ImageChops.difference(im, Image.new("RGB", im.size, BG[theme])).getbbox()
        box = b if box is None else (min(box[0], b[0]), min(box[1], b[1]), max(box[2], b[2]), max(box[3], b[3]))
    res = []
    for im in ims:
        c = Image.new("RGB", (box[2] - box[0] + 2 * margin, box[3] - box[1] + 2 * margin), BG[theme])
        c.paste(im.crop(box), (margin, margin))
        res.append(c)
    return res

def gif(theme, k, name):
    frames = cropped(sorted(src.glob(f"anim_{theme}_{k}_*.png")), theme, 18)
    w, h = frames[0].size
    sample = Image.new("RGB", (w, h * 4))
    for i, f in enumerate(frames[:: len(frames) // 4][:4]):
        sample.paste(f, (0, h * i))
    pal = sample.quantize(colors=128, method=Image.Quantize.MEDIANCUT)
    q = [f.quantize(palette=pal, dither=Image.Dither.NONE) for f in frames]
    q[0].save(out / name, save_all=True, append_images=q[1:], duration=50, loop=0, optimize=True)
    print(name, f"{w}x{h}", (out / name).stat().st_size // 1024, "KB")

for k in ("heat", "cool", "furnace"):
    gif("light", k, f"system-card-{k}.gif")
gif("dark", "heat", "system-card-heat-dark.gif")

for theme in ("light", "dark"):
    sfx = "" if theme == "light" else "-dark"
    ims = cropped([src / f"still_{theme}_{k}.png" for k in ("heat", "cool", "furnace", "idle")], theme, 24)
    for k, im in zip(("heat", "cool", "furnace", "idle"), ims):
        im.save(out / f"system-card-{k}{sfx}.png", optimize=True)
    w, h = ims[0].size
    g = Image.new("RGB", (w * 2, h * 2), BG[theme])
    for n, im in enumerate(ims):
        g.paste(im, ((n % 2) * w, (n // 2) * h))
    g.save(out / f"system-card{sfx}.png", optimize=True)
    print(f"system-card{sfx}.png", g.size, (out / f"system-card{sfx}.png").stat().st_size // 1024, "KB")


def grid_gif(theme, name, width=1200):
    """All four states in one looping GIF, laid out like the system-card.png still (idle is static)."""
    states = ("heat", "cool", "furnace", "idle")
    paths = {k: sorted(src.glob(f"anim_{theme}_{k}_*.png")) for k in states}
    every = cropped([p for k in states for p in paths[k]], theme, 18)  # one crop box: cells line up
    seqs, i = {}, 0
    for k in states:
        seqs[k] = every[i:i + len(paths[k])]
        i += len(paths[k])
    n = max(len(s) for s in seqs.values())
    w, h = every[0].size
    frames = []
    for f in range(n):
        g = Image.new("RGB", (w * 2, h * 2), BG[theme])
        for j, k in enumerate(states):
            g.paste(seqs[k][f % len(seqs[k])], ((j % 2) * w, (j // 2) * h))
        frames.append(g.resize((width, round(width * g.height / g.width)), Image.Resampling.LANCZOS))
    fw, fh = frames[0].size
    sample = Image.new("RGB", (fw, fh * 4))
    for j, f in enumerate(frames[:: n // 4][:4]):
        sample.paste(f, (0, fh * j))
    pal = sample.quantize(colors=192, method=Image.Quantize.MEDIANCUT)
    q = [f.quantize(palette=pal, dither=Image.Dither.NONE) for f in frames]
    q[0].save(out / name, save_all=True, append_images=q[1:], duration=50, loop=0, optimize=True)
    print(name, f"{fw}x{fh}", (out / name).stat().st_size // 1024, "KB")


grid_gif("light", "system-card-all.gif")
grid_gif("dark", "system-card-all-dark.gif")
