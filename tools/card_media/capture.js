// Freeze the card's CSS animations at time t, rasterize it via an SVG <foreignObject>, POST the PNG.
window.captureFrame = async (card, name, t, scale = 1, pad = 12) => {
  const theme = card.parentElement;
  const cs = getComputedStyle(theme);
  const vars = ["--primary-text-color", "--secondary-text-color", "--secondary-background-color", "--divider-color",
    "--primary-color", "--card-background-color", "--bg", "--disabled-text-color"]
    .map((v) => `${v}:${cs.getPropertyValue(v)}`).join(";");
  const r = card.shadowRoot.querySelector("ha-card").getBoundingClientRect();
  const w = Math.round(r.width) + 2 * pad, h = Math.round(r.height) + 2 * pad;
  const host = document.createElement("div");
  host.innerHTML = card.shadowRoot.innerHTML;
  const freeze = `*{animation-delay:-${t}s !important;animation-play-state:paused !important}
    ha-card{display:block;background:var(--card-background-color);border-radius:12px;box-shadow:0 1px 3px rgba(0,0,0,.2)}
    .wrap{${vars};background:var(--bg);padding:${pad}px;width:${w}px;height:${h}px;box-sizing:border-box;font-family:Roboto,system-ui,-apple-system,sans-serif}`;
  const xhtml = new XMLSerializer().serializeToString(host).replace(/^<div[^>]*>/, "").replace(/<\/div>$/, "");
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="${w * scale}" height="${h * scale}" viewBox="0 0 ${w} ${h}">
    <foreignObject width="${w}" height="${h}"><div xmlns="http://www.w3.org/1999/xhtml" class="wrap">
    <style>${freeze}</style>${xhtml}</div></foreignObject></svg>`;
  const img = new Image();
  img.src = "data:image/svg+xml;charset=utf-8," + encodeURIComponent(svg);
  await img.decode();
  const c = document.createElement("canvas");
  c.width = w * scale; c.height = h * scale;
  c.getContext("2d").drawImage(img, 0, 0);
  const res = await fetch(`/frame?name=${name}`, { method: "POST", body: c.toDataURL("image/png") });
  return res.status;
};
window.captureAll = async (prefix, card, fps, seconds, scale) => {
  const n = Math.round(fps * seconds);
  for (let i = 0; i < n; i++) await captureFrame(card, `${prefix}_${String(i).padStart(3, "0")}`, i / fps, scale);
  return n;
};
