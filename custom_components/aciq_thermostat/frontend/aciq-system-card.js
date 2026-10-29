/*
 * ACiQ System Card: animated schematic of a dual-fuel, ducted air-to-air heat
 * pump (outdoor unit -> refrigerant lines -> coil on gas furnace -> ducts ->
 * house), driven by the ha-aciq-thermostat entities. Served and loaded by the
 * integration itself: no Lovelace resource needed.
 *
 *   type: custom:aciq-system-card
 *   device_id: 0123abcd...      # optional: the aciq_thermostat device (auto when there is one)
 *   prefix: family_room         # optional: entity-id prefix instead of the device lookup
 *   title: HVAC system          # optional
 *   entities:                   # optional overrides (any key below; null hides one)
 *     power: sensor.heat_pump_power
 *     filter: sensor.furnace_filter_life_used
 *
 * Entity lookup, per key: entities override > device_id > prefix > the only
 * aciq_thermostat device > prefix "family_room".
 */
const VERSION = "1.2.0";
const DOMAIN = "aciq_thermostat";

// key -> [entity domain, translation_key in the integration, entity-id suffix for the prefix lookup]
const KEYS = {
  climate: ["climate", "thermostat", ""],
  room: ["sensor", "temperature", "_temperature"],
  humidity: ["sensor", "humidity", "_humidity"],
  setpoint: ["sensor", "setpoint", "_setpoint"],
  mode: ["sensor", "mode", "_mode"],
  outdoor: ["sensor", "outdoor_temperature", "_outdoor_temperature"],
  outdoor_coil: ["sensor", "outdoor_coil_temperature", "_outdoor_coil_temperature"],
  indoor_coil: ["sensor", "indoor_coil_temperature", "_indoor_coil_temperature"],
  return_air: ["sensor", "indoor_unit_t1", "_indoor_unit_t1_temperature"],
  hz: ["sensor", "compressor_frequency", "_compressor_frequency"],
  blower_stage: ["sensor", "blower_stage", "_blower_stage"],
  compressor: ["binary_sensor", "compressor", "_compressor"],
  furnace: ["binary_sensor", "furnace", "_furnace"],
  blower: ["binary_sensor", "blower", "_blower"],
  fault: ["binary_sensor", "fault", "_fault"],
  fault_codes: ["sensor", "fault_codes", "_fault_codes"],
  power: null,          // optional, not from the integration (e.g. an energy monitor)
  filter: null,         // optional % used sensor
};

const HZ_MAX = 90;

// aciq_thermostat device ids, in registry order (from the frontend's entity registry)
function aciqDevices(hass) {
  const ids = [];
  for (const e of Object.values(hass?.entities || {})) {
    if (e.platform === DOMAIN && e.device_id && !ids.includes(e.device_id)) ids.push(e.device_id);
  }
  return ids;
}

// key -> entity id for the given config
function resolveEntities(config, hass) {
  const reg = hass?.entities || {};
  let device = config.device_id;
  let prefix = config.prefix;
  if (!device && !prefix) {
    const devices = aciqDevices(hass);
    if (devices.length === 1) device = devices[0];
    else prefix = "family_room";
  }
  const byTk = {};
  if (device) {
    for (const e of Object.values(reg)) {
      if (e.platform === DOMAIN && e.device_id === device && e.translation_key) {
        byTk[`${e.entity_id.split(".")[0]}:${e.translation_key}`] = e.entity_id;
      }
    }
  }
  const out = {};
  for (const [k, spec] of Object.entries(KEYS)) {
    const o = config.entities?.[k];
    if (o !== undefined) out[k] = o || null;
    else if (!spec) out[k] = null;
    else if (device) out[k] = byTk[`${spec[0]}:${spec[1]}`] || null;
    else out[k] = `${spec[0]}.${prefix}${spec[2]}`;
  }
  return out;
}

class AciqSystemCard extends HTMLElement {
  static getConfigElement() { return document.createElement("aciq-system-card-editor"); }

  static getStubConfig(hass) {
    const device = aciqDevices(hass)[0];
    return device ? { device_id: device, title: "HVAC system" } : { title: "HVAC system" };
  }

  setConfig(config) {
    this._config = config;
    this._e = {};
    this._reg = undefined;
    if (!this.shadowRoot) this.attachShadow({ mode: "open" });
    this._sig = null;
    if (this._hass) this.hass = this._hass;
  }

  set hass(hass) {
    this._hass = hass;
    if (hass.entities !== this._reg) {     // the registry object is replaced when it changes
      this._reg = hass.entities;
      this._e = resolveEntities(this._config, hass);
    }
    const sig = Object.values(this._e)
      .map((id) => (id && hass.states[id] ? id + hass.states[id].state + JSON.stringify(hass.states[id].attributes.hvac_action || "") : "-"))
      .join("|");
    if (sig === this._sig) return;
    this._sig = sig;
    this._render();
  }

  getCardSize() { return 5; }
  getGridOptions() { return { columns: 12, min_columns: 6, rows: 5 }; }

  _st(k) { const id = this._e[k]; return id ? this._hass.states[id] : undefined; }
  _num(k) { const s = this._st(k); const n = s ? parseFloat(s.state) : NaN; return Number.isFinite(n) ? n : null; }
  _on(k) { return this._st(k)?.state === "on"; }
  _t(k, d = 0) { const n = this._num(k); return n === null ? "–" : `${n.toFixed(d)}°`; }

  _moreInfo(k) {
    const entityId = this._e[k];
    if (!entityId) return;
    this.dispatchEvent(new CustomEvent("hass-more-info", { detail: { entityId }, bubbles: true, composed: true }));
  }

  // Map an air/coil temperature to a blue -> neutral -> red colour.
  _tempColor(t) {
    if (t === null) return "var(--disabled-text-color, #888)";
    if (t <= 55) return "#2b8cff";
    if (t >= 95) return "#ff5a36";
    if (t < 70) return "#6aa9ff";
    if (t > 80) return "#ff8a50";
    return "#9aa4b1";
  }

  _render() {
    const climate = this._st("climate");
    const mode = this._st("mode")?.state || climate?.state || "unknown";
    const action = climate?.attributes?.hvac_action || "idle";
    const comp = this._on("compressor");
    const furn = this._on("furnace");
    const blow = this._on("blower");
    const fault = this._on("fault");
    const hz = this._num("hz") || 0;
    const stage = this._num("blower_stage");
    const coil = this._num("indoor_coil");
    const power = this._num("power");
    const filt = this._num("filter");
    const unavailable = !climate || climate.state === "unavailable";

    // Refrigerant direction: in heat the hot gas goes indoors, in cool it comes back.
    const heatingHP = comp && action !== "cooling";
    const refColor = !comp ? "var(--divider-color, #555)" : heatingHP ? "#ff6b3d" : "#2b8cff";
    const fanDur = comp ? Math.max(0.35, 2.4 - (hz / HZ_MAX) * 2) : 0;
    const blowDur = blow ? Math.max(0.3, 1.4 - ((stage || 1) - 1) * 0.3) : 0;
    const supplyColor = blow ? (furn ? "#ff5a36" : this._tempColor(coil)) : "var(--divider-color, #555)";

    let headline;
    if (unavailable) headline = "Thermostat unavailable";
    else if (furn) headline = "Heating · gas furnace";
    else if (comp) headline = `${action === "cooling" ? "Cooling" : "Heating"} · heat pump ${hz.toFixed(0)} Hz`;
    else if (blow) headline = "Fan only";
    else headline = mode === "off" ? "System off" : "Idle";
    const headColor = furn ? "#ff5a36" : comp ? refColor : "var(--secondary-text-color)";

    const title = this._config.title ?? "HVAC system";
    const cap = (s) => (s ? s.charAt(0).toUpperCase() + s.slice(1).replace("_", " ") : "");
    const faultTxt = fault ? `<span class="chip bad">Fault ${this._st("fault_codes")?.state || ""}</span>` : "";

    this.shadowRoot.innerHTML = `
<style>
  :host { display: block; }
  ha-card { padding: 12px 12px 8px; overflow: hidden; }
  .top { display: flex; align-items: baseline; justify-content: space-between; gap: 8px; flex-wrap: wrap; }
  .title { font-size: 16px; font-weight: 500; color: var(--primary-text-color); }
  .head { font-size: 14px; font-weight: 500; color: ${headColor}; }
  .chips { display: flex; gap: 6px; margin-top: 4px; flex-wrap: wrap; }
  .chip { font-size: 12px; padding: 2px 8px; border-radius: 10px; background: var(--secondary-background-color); color: var(--secondary-text-color); }
  .chip.warn { background: #f9a825; color: #000; }
  .chip.bad { background: #c62828; color: #fff; }
  svg { width: 100%; height: auto; display: block; margin-top: 4px; }
  text { fill: var(--primary-text-color); font-family: inherit; font-size: 12px; }
  .sub { fill: var(--secondary-text-color); font-size: 10px; }
  .big { font-size: 20px; font-weight: 500; }
  .box { fill: var(--secondary-background-color, #2a2a2a); stroke: var(--divider-color, #555); stroke-width: 1.5; }
  .hit { cursor: pointer; }
  .hit:hover .box { stroke: var(--primary-color); }
  .pipe { fill: none; stroke-width: 4; stroke-linecap: round; }
  .flow { fill: none; stroke-width: 4; stroke-linecap: round; stroke-dasharray: 6 10; stroke: rgba(255,255,255,.75); }
  .duct { fill: none; stroke-width: 10; stroke-linecap: round; stroke-linejoin: round; opacity: .55; }
  .air { fill: none; stroke-width: 3; stroke-linecap: round; stroke-dasharray: 3 12; stroke: rgba(255,255,255,.8); }
  .spin { transform-box: view-box; animation: spin linear infinite; }
  .run { animation: dash 1.2s linear infinite; }
  .runrev { animation: dash 1.2s linear infinite reverse; }
  .flame { transform-box: fill-box; transform-origin: bottom center; animation: flick .5s ease-in-out infinite alternate; }
  @keyframes spin { to { transform: rotate(360deg); } }
  @keyframes dash { to { stroke-dashoffset: -32; } }
  @keyframes flick { from { transform: scaleY(.85); } to { transform: scaleY(1.1); } }
  @media (prefers-reduced-motion: reduce) { .spin, .run, .runrev, .flame { animation: none; } }
</style>
<ha-card>
  <div class="top">
    <span class="title">${title}</span>
    <span class="head">${headline}</span>
  </div>
  <div class="chips">
    <span class="chip">Mode ${cap(mode)}</span>
    ${stage ? `<span class="chip">Blower stage ${stage}</span>` : ""}
    ${power !== null ? `<span class="chip">${power >= 1000 ? (power / 1000).toFixed(2) + " kW" : power.toFixed(0) + " W"}</span>` : ""}
    ${filt !== null ? `<span class="chip hit${filt >= 100 ? " bad" : filt >= 90 ? " warn" : ""}" data-k="filter">Filter ${filt.toFixed(0)}%</span>` : ""}
    ${faultTxt}
  </div>
  <svg viewBox="0 0 400 220" role="img" aria-label="${headline}">
    <!-- Outdoor unit -->
    <g class="hit" data-k="hz">
      <rect class="box" x="8" y="96" width="96" height="84" rx="6"/>
      <circle cx="56" cy="134" r="27" fill="none" stroke="var(--divider-color,#555)" stroke-width="1.5"/>
      <g class="${comp ? "spin" : ""}" style="transform-origin:56px 134px;animation-duration:${fanDur}s">
        ${[0, 90, 180, 270].map((a) => `<path d="M56 134 C49 128 49 114 56 110 C63 114 63 128 56 134Z" fill="${comp ? refColor : "var(--secondary-text-color)"}" transform="rotate(${a + 45} 56 134)"/>`).join("")}
      </g>
      <circle cx="56" cy="134" r="3.5" fill="var(--primary-text-color)"/>
      <text x="56" y="175" text-anchor="middle" class="sub">${comp ? `${hz.toFixed(0)} Hz` : "Compressor off"}</text>
    </g>
    <g class="hit" data-k="outdoor">
      <text x="56" y="70" text-anchor="middle" class="big">${this._t("outdoor", 0)}</text>
      <text x="56" y="86" text-anchor="middle" class="sub">Outdoor · coil ${this._t("outdoor_coil", 0)}</text>
    </g>

    <!-- Refrigerant lines -->
    <path class="pipe" stroke="${refColor}" d="M104 150 H170"/>
    <path class="pipe" stroke="${refColor}" d="M104 164 H170" opacity=".6"/>
    ${comp ? `<path class="flow ${heatingHP ? "run" : "runrev"}" d="M104 150 H170"/><path class="flow ${heatingHP ? "runrev" : "run"}" d="M104 164 H170" opacity=".6"/>` : ""}
    <text x="137" y="182" text-anchor="middle" class="sub">Refrigerant</text>

    <!-- Indoor: coil on top of furnace + blower -->
    <g class="hit" data-k="indoor_coil">
      <rect class="box" x="170" y="96" width="70" height="40" rx="4"/>
      <path d="M180 132 L205 102 L230 132" fill="none" stroke="${this._tempColor(coil)}" stroke-width="3"/>
      <text x="205" y="128" text-anchor="middle" class="sub">${this._t("indoor_coil", 0)}</text>
    </g>
    <g class="hit" data-k="furnace">
      <rect class="box" x="170" y="136" width="70" height="40" rx="4"/>
      ${furn
        ? `<g class="flame"><path d="M196 170 q-8 -12 3 -24 q0 9 6 11 q2 -8 -1 -13 q14 10 5 26z" fill="#ff7a29"/></g>`
        : `<path d="M190 168 H220" stroke="var(--secondary-text-color)" stroke-width="2" stroke-dasharray="3 3"/>`}
      <text x="205" y="148" text-anchor="middle" class="sub">${furn ? "Gas on" : "Furnace"}</text>
    </g>
    <g class="hit" data-k="blower">
      <rect class="box" x="170" y="176" width="70" height="34" rx="4"/>
      <g class="${blow ? "spin" : ""}" style="transform-origin:205px 193px;animation-duration:${blowDur}s">
        <circle cx="205" cy="193" r="11" fill="none" stroke="${blow ? "var(--primary-color)" : "var(--secondary-text-color)"}" stroke-width="2" stroke-dasharray="4 3"/>
      </g>
    </g>

    <!-- Return air (house -> bottom of furnace) -->
    <path class="duct" stroke="var(--divider-color,#555)" d="M240 200 H288"/>
    ${blow ? `<path class="air run" d="M288 200 H240"/>` : ""}
    <g class="hit" data-k="return_air"><text x="270" y="216" text-anchor="middle" class="sub">Return ${this._t("return_air", 0)}</text></g>

    <!-- Supply air (top of coil -> house) -->
    <path class="duct" stroke="${supplyColor}" d="M205 96 V40 H288"/>
    ${blow ? `<path class="air run" d="M205 96 V40 H288"/>` : ""}
    <text x="250" y="32" text-anchor="middle" class="sub">Supply${blow && coil !== null && !furn ? ` ~${coil.toFixed(0)}°` : ""}</text>

    <!-- House -->
    <g class="hit" data-k="climate">
      <path class="box" d="M288 30 L342 6 L396 30 V208 H288 Z"/>
      <text x="342" y="112" text-anchor="middle" class="big">${this._t("room", 1)}</text>
      <text x="342" y="134" text-anchor="middle" class="sub">Set ${this._t("setpoint", 0)}</text>
      <text x="342" y="152" text-anchor="middle" class="sub">${this._num("humidity") === null ? "" : `${this._num("humidity").toFixed(0)}% RH`}</text>
    </g>
  </svg>
</ha-card>`;
    this.shadowRoot.querySelectorAll(".hit").forEach((el) =>
      el.addEventListener("click", () => this._moreInfo(el.dataset.k)));
  }
}

// Visual editor: device, title and the two optional non-integration sensors.
const EDITOR_SCHEMA = [
  { name: "device_id", selector: { device: { integration: DOMAIN } } },
  { name: "title", selector: { text: {} } },
  { name: "power", selector: { entity: { domain: "sensor", device_class: "power" } } },
  { name: "filter", selector: { entity: { domain: "sensor" } } },
];
const EDITOR_LABELS = {
  device_id: "Thermostat (ACiQ device)",
  title: "Title",
  power: "Power sensor (optional)",
  filter: "Filter used % sensor (optional)",
};

class AciqSystemCardEditor extends HTMLElement {
  setConfig(config) { this._config = config; this._render(); }
  set hass(hass) { this._hass = hass; if (this._form) this._form.hass = hass; else this._render(); }

  _render() {
    if (!this._config || !this._hass) return;
    if (!this._form) {
      this._form = document.createElement("ha-form");
      this._form.computeLabel = (s) => EDITOR_LABELS[s.name] || s.name;
      this._form.computeHelper = (s) => (s.name === "device_id" && this._config.prefix
        ? `Uses entity prefix "${this._config.prefix}" until a device is picked` : undefined);
      this._form.addEventListener("value-changed", (ev) => this._changed(ev.detail.value));
      this.appendChild(this._form);
    }
    this._form.hass = this._hass;
    this._form.schema = EDITOR_SCHEMA;
    this._form.data = {
      device_id: this._config.device_id,
      title: this._config.title,
      power: this._config.entities?.power,
      filter: this._config.entities?.filter,
    };
  }

  _changed(v) {
    const config = { ...this._config };
    const entities = { ...(config.entities || {}) };
    for (const k of ["device_id", "title"]) {
      if (v[k]) config[k] = v[k];
      else delete config[k];
    }
    if (v.device_id) delete config.prefix;          // the device lookup replaces the prefix
    for (const k of ["power", "filter"]) {
      if (v[k]) entities[k] = v[k];
      else delete entities[k];
    }
    if (Object.keys(entities).length) config.entities = entities;
    else delete config.entities;
    this._config = config;
    this.dispatchEvent(new CustomEvent("config-changed", { detail: { config }, bubbles: true, composed: true }));
  }
}

if (!customElements.get("aciq-system-card")) {
  customElements.define("aciq-system-card", AciqSystemCard);
  if (!customElements.get("aciq-system-card-editor")) customElements.define("aciq-system-card-editor", AciqSystemCardEditor);
  window.customCards = window.customCards || [];
  window.customCards.push({
    type: "aciq-system-card", name: "ACiQ System Card", description: "Animated dual-fuel heat pump schematic",
    preview: true, documentationURL: "https://github.com/oxn0r/ha-aciq-thermostat#system-card",
  });
  console.info(`%c ACIQ-SYSTEM-CARD %c v${VERSION} `, "background:#ff6b3d;color:#fff", "");
}
