# System card media

Regenerates the README / release images of the ACiQ System Card (`docs/images/`) from the card's real code.

1. `python3 server.py` and open <http://localhost:8765/index.html> in a Chromium-based browser. It shows the
   card in heat, cool, gas furnace and idle, in a light and a dark theme, from made-up states.
2. In the browser console:
   ```js
   for (const [th, k, c] of cards) await captureFrame(c, `still_${th}_${k}`, 0.15, 2);
   for (const [th, k, c] of cards) await captureAll(`anim_${th}_${k}`, c, 20, k === "idle" ? 0.05 : 3, 1.5);
   ```
   `captureFrame` pauses the CSS animations at a given time, draws the card through an SVG `<foreignObject>`
   and posts the PNG to the server (saved in `frames/`).
3. `python3 make_media.py frames ../../docs/images` (Pillow) crops the frames and writes the GIFs
   and stills, including `system-card-all.gif` / `system-card.png` with all four states (README and release notes).

The demo states use 54 Hz and blower stage 4 so each 3 s GIF loops cleanly (fan, refrigerant and blower all
complete whole cycles).

## Every system type (README gallery)

`gallery.html` shows every system type the card draws, in each state it can show (`?theme=dark`, `?only=<key>`,
`?fixed` for the capture layout). Open `gallery.html?fixed` (then `?fixed&theme=dark`) and run:
```js
let i = 0;
for (const [sys, k, c] of cards) await captureAll(`g_${"light"}__${String(i++).padStart(2, "0")}_${sys}__${k}`, c, 10, 3, 1);
```
(with `"dark"` on the dark page), then `python3 make_gallery.py frames ../../docs/images/systems`: one looping GIF per
system with all its states, light and dark.
