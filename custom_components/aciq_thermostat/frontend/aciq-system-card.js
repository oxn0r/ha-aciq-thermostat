/*
 * ACiQ System Card: animated schematic of the HVAC equipment the thermostat runs, drawn from what the
 * thermostat reports (the System type sensor): a heat pump or air conditioner outdoors, and indoors a
 * furnace, or an air handler with or without electric heat strips or a hot-water coil from a boiler, or
 * a boiler heating radiators. Driven by the ha-aciq-thermostat entities. Served and loaded by the
 * integration itself: no Lovelace resource needed.
 *
 *   type: custom:aciq-system-card
 *   device_id: 0123abcd...      # optional: the aciq_thermostat device (auto when there is one)
 *   prefix: family_room         # optional: entity-id prefix instead of the device lookup
 *   title: HVAC system          # optional
 *   outdoor_unit: heat_pump     # optional: heat_pump | air_conditioner | none (default: from the thermostat)
 *   heat_source: electric       # optional: furnace | electric | boiler | none (default: from the thermostat)
 *   entities:                   # optional overrides (any key below; null hides one)
 *     power: sensor.heat_pump_power
 *     filter: sensor.furnace_filter_life_used
 *
 * Entity lookup, per key: entities override > device_id > prefix > the only
 * aciq_thermostat device > prefix "family_room".
 */
const VERSION = "1.5.0";
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
  dry_left: ["sensor", "dry_time_left", "_dry_time_left"],
  system_type: ["sensor", "system_type", "_system_type"],
  // conventional 24 V: the outputs the thermostat drives
  compressor_call: ["binary_sensor", "compressor_call", "_compressor_call"],
  compressor_stage_2: ["binary_sensor", "compressor_stage_2", "_compressor_stage_2_call"],
  aux_heat_call: ["binary_sensor", "aux_heat_call", "_aux_heat_call"],
  fan_call: ["binary_sensor", "fan_call", "_fan_call"],
  power: null,          // optional, not from the integration (e.g. an energy monitor)
  filter: null,         // optional % used sensor
};

const HZ_MAX = 90;
const HOT = "#ff5a36";
const HEAT_NAMES = { furnace: "furnace", electric: "electric heat", boiler: "boiler" };
const OUTDOOR_LABELS = { heat_pump: "Heat pump", air_conditioner: "Air conditioner" };
const HEAT_LABELS = { furnace: "Furnace", electric: "Electric heat", boiler: "Boiler" };

// "Heat pump + furnace", "Air conditioner", "Boiler", ...
function systemLabel(eq) {
  const out = OUTDOOR_LABELS[eq.outdoor], heat = HEAT_LABELS[eq.heat];
  return out && heat ? `${out} + ${heat.toLowerCase()}` : out || heat || "";
}

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

  // Equipment to draw: card config > the System type sensor > the original dual-fuel layout
  // (YAML without the sensor, e.g. custom entity overrides).
  _equipment() {
    const cfg = this._config;
    const st = this._st("system_type");
    const a = st ? st.attributes || {} : null;
    const known = (v) => v && v !== "unknown";
    const v24 = a?.wiring === "24v";
    let outdoor = cfg.outdoor_unit || (a ? a.outdoor_unit : "heat_pump");
    let heat = cfg.heat_source || (a ? a.heat_source : "furnace");
    if (!known(outdoor)) outdoor = v24 ? "none" : "heat_pump";       // every communicating unit seen has one
    if (!known(heat)) heat = this._on(v24 ? "aux_heat_call" : "furnace") ? "furnace" : "none";
    // ducted equipment always has a blower; only a boiler can run without one (radiators)
    const blower = a?.blower !== false || outdoor !== "none" || heat === "furnace" || heat === "electric";
    const indoor = heat === "furnace" ? "furnace" : blower ? "air_handler" : "none";
    return { outdoor, heat, indoor, blower, v24 };
  }

  _render() {
    const eq = this._equipment();
    const v24 = eq.v24;
    const climate = this._st("climate");
    const mode = this._st("mode")?.state || climate?.state || "unknown";
    const action = climate?.attributes?.hvac_action || "idle";
    const hasOut = eq.outdoor !== "none";
    const isHP = eq.outdoor === "heat_pump";
    const hasHeat = eq.heat !== "none";
    const comp = hasOut && this._on(v24 ? "compressor_call" : "compressor");
    const stage2 = v24 && comp && this._on("compressor_stage_2");
    const heatOn = hasHeat && this._on(v24 ? "aux_heat_call" : "furnace");
    const blow = v24 ? eq.blower && (this._on("fan_call") || comp || heatOn) : this._on("blower");
    const fault = this._on("fault");
    const hz = v24 ? 0 : this._num("hz") || 0;
    const stage = this._num("blower_stage");
    const coil = this._num("indoor_coil");
    const power = this._num("power");
    const filt = this._num("filter");
    const unavailable = !climate || climate.state === "unavailable";
    // Dry: the heat pump cools a cold coil with a slow blower to pull moisture out
    const dry = mode === "dry" || climate?.state === "dry";
    const dryLeft = this._num("dry_left");
    // Defrost: the heat pump runs reversed (indoor coil cold) and the outdoor fan stops
    const defrost = action === "defrosting";
    // Refrigerant direction: in heat the hot gas goes indoors, in cool (and Dry, and defrost) it comes back.
    const heatingHP = comp && isHP && action !== "cooling" && action !== "drying" && !dry && !defrost;
    const divider = "var(--divider-color, #555)";
    const refColor = !comp ? divider : heatingHP ? "#ff6b3d" : "#2b8cff";
    const step = (d) => Math.round(d * 20) / 20;   // 0.05 s steps: small Hz changes keep the same speed
    const outFan = comp && !defrost;
    const speed = v24 ? (stage2 ? 0.9 : 0.45) : hz / HZ_MAX;   // 24 V: 0.6 s / 1.5 s a turn
    const fanDur = outFan ? step(Math.max(0.35, 2.4 - speed * 2)) : 0;
    // stages 1-4 on ACiQ indoor units; some report a raw speed instead (e.g. 118), and 24 V has none: a medium spin
    const blowDur = blow ? step(stage > 4 || v24 ? 0.75 : Math.max(0.3, 1.4 - ((stage || 1) - 1) * 0.3)) : 0;
    const supplyColor = !blow ? divider : heatOn ? HOT : coil !== null ? this._tempColor(coil)
      : comp ? (heatingHP ? "#ff8a50" : "#6aa9ff") : "#9aa4b1";
    const heatName = HEAT_NAMES[eq.heat] || "backup heat";
    const compTxt = hz ? `${hz.toFixed(0)} Hz` : v24 && comp ? `stage ${stage2 ? 2 : 1}` : "";
    const compPart = `${isHP ? "heat pump" : "AC"}${compTxt ? ` ${compTxt}` : ""}`;

    let headline;
    if (unavailable) headline = "Thermostat unavailable";
    else if (defrost) headline = `Defrosting${comp ? ` · ${compPart}` : ""}${heatOn ? ` + ${heatName}` : ""}`;
    else if (dry) headline = `Drying${comp ? ` · ${compPart}` : ""}${dryLeft ? ` · ${dryLeft.toFixed(0)} min left` : ""}`;
    else if (comp && heatOn) headline = `Heating · ${compPart} + ${heatName}`;
    else if (heatOn) headline = `${mode === "emergency_heat" ? "Emergency heat" : "Heating"} · ${heatName}`;
    else if (comp) headline = `${heatingHP ? "Heating" : "Cooling"} · ${compPart}`;
    else if (blow) headline = "Fan only";
    else headline = mode === "off" ? "System off" : "Idle";
    const headColor = heatOn ? HOT : defrost || dry ? "#2b8cff" : comp ? refColor : "var(--secondary-text-color)";

    const title = this._config.title ?? "HVAC system";
    const cap = (s) => (s ? s.charAt(0).toUpperCase() + s.slice(1).replace("_", " ") : "");
    // Fault can be on from the indoor unit's fault state alone, with no code: then just "Fault"
    const codes = this._st("fault_codes")?.state;
    const codeTxt = codes && !["none", "unknown", "unavailable"].includes(codes) ? ` ${codes}` : "";
    const faultTxt = fault ? `<span class="chip bad">Fault${codeTxt}</span>` : "";
    const sysTxt = systemLabel(eq);
    const kHeat = v24 ? "aux_heat_call" : "furnace";
    const kBlow = v24 ? "fan_call" : "blower";
    const sec = "var(--secondary-text-color)";

    // ---- indoor stack (x 170-240, y 96-210), refrigerant line heights, boiler box ----
    let parts = [], lineY = null, boilerBox = null;
    const elec = eq.heat === "electric", boiler = eq.heat === "boiler";
    if (eq.indoor === "furnace") {
      if (hasOut) { parts = [["coil", 96, 136], ["furnace", 136, 176], ["blower", 176, 210]]; lineY = [108, 122]; }
      else parts = [["furnace", 96, 176], ["blower", 176, 210]];
    } else if (eq.indoor === "air_handler") {
      // upflow air handler: return air in at the bottom through the coil, blower above it, heat at the outlet
      if (hasOut && elec) { parts = [["strips", 96, 118], ["blower", 118, 150], ["coil", 150, 210]]; lineY = [160, 174]; }
      else if (hasOut && boiler) { parts = [["blower", 96, 140], ["coil", 140, 180], ["water", 180, 210]]; lineY = [150, 164]; boilerBox = [110, 176, 158, 214]; }
      else if (hasOut) { parts = [["blower", 96, 146], ["coil", 146, 210]]; lineY = [158, 172]; }
      else if (elec) parts = [["strips", 96, 150], ["blower", 150, 210]];
      else if (boiler) { parts = [["blower", 96, 150], ["water", 150, 210]]; boilerBox = [8, 140, 104, 204]; }
      else parts = [["blower", 96, 210]];
    } else {
      boilerBox = [170, 110, 240, 200];      // boiler heating radiators: no air handler, no ducts
    }
    const flame = (cx, bottom, k = 1) => `<g transform="translate(${cx} ${bottom}) scale(${k}) translate(-201 -170)"><g class="flame"><path d="M196 170 q-8 -12 3 -24 q0 9 6 11 q2 -8 -1 -13 q14 10 5 26z" fill="#ff7a29"/></g></g>`;
    const box = (y0, y1) => `<rect class="box" x="170" y="${y0}" width="70" height="${y1 - y0}" rx="4"/>`;
    const hasCoilT = !!this._st("indoor_coil");
    const draw = {
      coil: (y0, y1) => `<g class="hit" data-k="indoor_coil">${box(y0, y1)}
        <path d="M180 ${y1 - 4} L205 ${y0 + 6} L230 ${y1 - 4}" fill="none" stroke="${hasCoilT ? this._tempColor(coil) : comp ? refColor : sec}" stroke-width="3"/>
        ${hasCoilT ? `<text x="205" y="${y1 - 8}" text-anchor="middle" class="sub">${this._t("indoor_coil", 0)}</text>` : ""}
        ${dry ? [188, 222].map((x) => `<g class="drip"><path d="M${x} ${y0 + 16} q-3.5 5 0 7 q3.5 -2 0 -7z" fill="#6aa9ff"/></g>`).join("") : ""}</g>`,
      furnace: (y0, y1) => `<g class="hit" data-k="${kHeat}">${box(y0, y1)}
        ${heatOn ? flame(205, y1 - 6) : `<path d="M190 ${y1 - 8} H220" stroke="${sec}" stroke-width="2" stroke-dasharray="3 3"/>`}
        <text x="205" y="${y0 + 12}" text-anchor="middle" class="sub">${heatOn ? "Furnace on" : "Furnace"}</text></g>`,
      blower: (y0, y1) => {
        const cy = (y0 + y1) / 2, r = Math.min(11, (y1 - y0) / 2 - 5);
        return `<g class="hit" data-k="${kBlow}">${box(y0, y1)}
        <g class="${blow ? "spin" : ""}" style="transform-origin:205px ${cy}px;animation-duration:${blowDur}s">
          <circle cx="205" cy="${cy}" r="${r}" fill="none" stroke="${blow ? "var(--primary-color)" : sec}" stroke-width="2" stroke-dasharray="4 3"/>
        </g></g>`;
      },
      strips: (y0, y1) => {
        const label = y1 - y0 >= 40;
        const m = (y0 + y1) / 2 + (label ? 7 : 0), a = Math.min(8, (y1 - y0) / 2 - 4);
        const pts = Array.from({ length: 10 }, (_, i) => `${178 + i * 6},${i % 2 ? m - a : m + a}`).join(" ");
        return `<g class="hit" data-k="${kHeat}">${box(y0, y1)}
        ${heatOn ? `<polyline class="glow" points="${pts}" fill="none" stroke="${HOT}" stroke-width="7" stroke-linejoin="round" opacity=".35"/>` : ""}
        <polyline points="${pts}" fill="none" stroke="${heatOn ? "#ff7a29" : sec}" stroke-width="2.5" stroke-linejoin="round"/>
        ${label ? `<text x="205" y="${y0 + 12}" text-anchor="middle" class="sub">${heatOn ? "Strips on" : "Heat strips"}</text>` : ""}</g>`;
      },
      water: (y0, y1) => {
        const d = `M176 ${y0 + 7} H234 V${y0 + 13} H176 V${y0 + 19} H234 V${y1 - 5}`;
        return `<g class="hit" data-k="${kHeat}">${box(y0, y1)}
        <path class="pipe" stroke="${heatOn ? HOT : sec}" style="stroke-width:2.5" d="${d}"/>
        ${heatOn ? `<path class="flow run" style="stroke-width:2" d="${d}"/>` : ""}</g>`;
      },
    };
    const stack = parts.map(([p, y0, y1]) => draw[p](y0, y1)).join("");
    const top = parts.length ? parts[0][1] : null;
    const waterPart = parts.find(([p]) => p === "water");
    const water = heatOn ? HOT : divider;
    let boilerSvg = "";
    if (boilerBox) {
      const [x0, y0, x1, y1] = boilerBox, cx = (x0 + x1) / 2;
      const pipes = waterPart                       // hydro-air: hot water to the coil in the air handler
        ? [waterPart[1] + 8, waterPart[2] - 8].map((y) => `M${x1} ${y} H170`)
        : [176, 190].map((y) => `M240 ${y} H300`);  // radiators in the house
      boilerSvg = `${pipes.map((d, i) => `<path class="pipe" stroke="${water}" d="${d}"${i ? ' opacity=".6"' : ""}/>`).join("")}
        ${heatOn ? pipes.map((d, i) => `<path class="flow ${i ? "runrev" : "run"}" d="${d}"${i ? ' opacity=".6"' : ""}/>`).join("") : ""}
        <g class="hit" data-k="${kHeat}"><rect class="box" x="${x0}" y="${y0}" width="${x1 - x0}" height="${y1 - y0}" rx="4"/>
        ${heatOn ? flame(cx, y1 - 4, y1 - y0 < 50 ? 0.6 : 1) : `<path d="M${cx - 10} ${y1 - 7} H${cx + 10}" stroke="${sec}" stroke-width="2" stroke-dasharray="3 3"/>`}
        <text x="${cx}" y="${y0 + 12}" text-anchor="middle" class="sub">${heatOn && x1 - x0 > 50 ? "Boiler on" : "Boiler"}</text></g>`;
    }
    const radiator = eq.indoor === "none"
      ? `<g class="hit" data-k="${kHeat}"><rect class="box" x="300" y="170" width="84" height="26" rx="3"/>
        ${Array.from({ length: 9 }, (_, i) => `<path d="M${308 + i * 8.5} 173 V193" stroke="${heatOn ? HOT : sec}" stroke-width="3" stroke-linecap="round"${heatOn ? ' class="glow"' : ""}/>`).join("")}</g>`
      : "";

    const showOutT = !!this._st("outdoor");
    const outdoorSvg = hasOut ? `
    <g class="hit" data-k="${v24 ? "compressor_call" : "hz"}">
      <rect class="box" x="8" y="96" width="96" height="84" rx="6"/>
      <circle cx="56" cy="134" r="27" fill="none" stroke="${divider}" stroke-width="1.5"/>
      <g class="${outFan ? "spin" : ""}" style="transform-origin:56px 134px;animation-duration:${fanDur}s">
        ${[0, 90, 180, 270].map((a) => `<path d="M56 134 C49 128 49 114 56 110 C63 114 63 128 56 134Z" fill="${comp ? refColor : sec}" transform="rotate(${a + 45} 56 134)"/>`).join("")}
      </g>
      <circle cx="56" cy="134" r="3.5" fill="var(--primary-text-color)"/>
      <text x="56" y="175" text-anchor="middle" class="sub">${comp ? cap(compTxt) || "On" : "Off"}</text>
      <text x="56" y="194" text-anchor="middle" class="sub">${isHP ? "Heat pump" : "Air conditioner"}</text>
    </g>
    <path class="pipe" stroke="${refColor}" d="M104 ${lineY[0]} H170"/>
    <path class="pipe" stroke="${refColor}" d="M104 ${lineY[1]} H170" opacity=".6"/>
    ${comp ? `<path class="flow ${heatingHP ? "run" : "runrev"}" d="M104 ${lineY[0]} H170"/><path class="flow ${heatingHP ? "runrev" : "run"}" d="M104 ${lineY[1]} H170" opacity=".6"/>` : ""}
    <text x="137" y="${lineY[0] - 8}" text-anchor="middle" class="sub">Refrigerant</text>` : "";

    const ducts = top !== null ? `
    <path class="duct" stroke="${divider}" d="M240 200 H288"/>
    ${blow ? `<path class="air run" d="M288 200 H240"/>` : ""}
    ${this._st("return_air") ? `<g class="hit" data-k="return_air"><text x="270" y="216" text-anchor="middle" class="sub">Return ${this._t("return_air", 0)}</text></g>` : ""}
    <path class="duct" stroke="${supplyColor}" d="M205 ${top} V40 H288"/>
    ${blow ? `<path class="air run" d="M205 ${top} V40 H288"/>` : ""}
    <text x="250" y="32" text-anchor="middle" class="sub">Supply${blow && coil !== null && !heatOn ? ` ~${coil.toFixed(0)}°` : ""}</text>` : "";

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
  .chip.dry { background: #2b8cff; color: #fff; }
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
  .air.run { animation-name: airdash; }
  .spin { transform-box: view-box; animation: spin linear infinite; }
  .run { animation: dash 1.2s linear infinite; }
  .runrev { animation: dash 1.2s linear infinite reverse; }
  .flame { transform-box: fill-box; transform-origin: bottom center; animation: flick .5s ease-in-out infinite alternate; }
  .glow { animation: glow .75s ease-in-out infinite alternate; }
  @keyframes spin { to { transform: rotate(360deg); } }
  @keyframes dash { to { stroke-dashoffset: -32; } }      /* 2 x the .flow dash period (6+10) */
  @keyframes airdash { to { stroke-dashoffset: -30; } }   /* 2 x the .air dash period (3+12) */
  .drip { animation: drip 1.5s ease-in infinite; }
  @keyframes drip { 0% { transform: translateY(0); opacity: 0; } 15% { opacity: 1; } 80% { opacity: 1; } 100% { transform: translateY(12px); opacity: 0; } }
  @keyframes flick { from { transform: scaleY(.85); } to { transform: scaleY(1.1); } }
  @keyframes glow { from { opacity: .45; } to { opacity: 1; } }
  @media (prefers-reduced-motion: reduce) { .spin, .run, .runrev, .flame, .drip, .glow { animation: none; } }
</style>
<ha-card>
  <div class="top">
    <span class="title">${title}</span>
    <span class="head">${headline}</span>
  </div>
  <div class="chips">
    <span class="chip">Mode ${cap(mode)}</span>
    ${dry && dryLeft ? `<span class="chip dry hit" data-k="dry_left">Dry · ${dryLeft.toFixed(0)} min left</span>` : ""}
    ${stage ? `<span class="chip">${stage > 4 ? `Blower ${stage.toFixed(0)}` : `Blower stage ${stage}`}</span>` : ""}
    ${stage2 ? `<span class="chip">Stage 2</span>` : ""}
    ${power !== null ? `<span class="chip">${power >= 1000 ? (power / 1000).toFixed(2) + " kW" : power.toFixed(0) + " W"}</span>` : ""}
    ${filt !== null ? `<span class="chip hit${filt >= 100 ? " bad" : filt >= 90 ? " warn" : ""}" data-k="filter">Filter ${filt.toFixed(0)}%</span>` : ""}
    ${faultTxt}
    ${sysTxt ? `<span class="chip sys hit" data-k="system_type">${sysTxt}</span>` : ""}
  </div>
  <svg viewBox="0 0 400 220" role="img" aria-label="${sysTxt ? `${sysTxt}: ` : ""}${headline}">
    ${showOutT ? `<g class="hit" data-k="outdoor">
      <text x="56" y="70" text-anchor="middle" class="big">${this._t("outdoor", 0)}</text>
      <text x="56" y="86" text-anchor="middle" class="sub">Outdoor${this._st("outdoor_coil") ? ` · coil ${this._t("outdoor_coil", 0)}` : ""}</text>
    </g>` : ""}
    ${outdoorSvg}
    ${boilerSvg}
    ${stack}
    ${ducts}
    <!-- House -->
    <g class="hit" data-k="climate">
      <path class="box" d="M288 30 L342 6 L396 30 V208 H288 Z"/>
      <text x="342" y="112" text-anchor="middle" class="big">${this._t("room", 1)}</text>
      <text x="342" y="134" text-anchor="middle" class="sub">Set ${this._t("setpoint", 0)}</text>
      <text x="342" y="152" text-anchor="middle" class="sub">${this._num("humidity") === null ? "" : `${this._num("humidity").toFixed(0)}% RH`}</text>
    </g>
    ${radiator}
  </svg>
</ha-card>`;
    this.shadowRoot.querySelectorAll(".hit").forEach((el) =>
      el.addEventListener("click", () => this._moreInfo(el.dataset.k)));
    this._align();
  }

  // Each render replaces the SVG, which would restart every animation from its first frame
  // (a visible jump on every sensor update). Start them all at the page timeline's zero
  // instead, so a re-rendered card continues exactly where the old one was.
  _align() {
    for (const a of this.shadowRoot?.getAnimations?.() ?? [])
      if (a.playState === "running") a.startTime = 0;
  }

  connectedCallback() { this._align(); }   // the first render may happen before the card is on the page
}

// Visual editor: device, title and the two optional non-integration sensors.
const AUTO = "auto";
const pick = (opts) => ({ select: { mode: "dropdown", options: [{ value: AUTO, label: "Auto (from the thermostat)" }, ...opts] } });
const EDITOR_SCHEMA = [
  { name: "device_id", selector: { device: { integration: DOMAIN } } },
  { name: "title", selector: { text: {} } },
  { name: "outdoor_unit", selector: pick([{ value: "heat_pump", label: "Heat pump" },
    { value: "air_conditioner", label: "Air conditioner" }, { value: "none", label: "None" }]) },
  { name: "heat_source", selector: pick([{ value: "furnace", label: "Furnace" },
    { value: "electric", label: "Electric heat strips" }, { value: "boiler", label: "Boiler" }, { value: "none", label: "None" }]) },
  { name: "power", selector: { entity: { domain: "sensor", device_class: "power" } } },
  { name: "filter", selector: { entity: { domain: "sensor" } } },
];
const EDITOR_LABELS = {
  device_id: "Thermostat (ACiQ device)",
  title: "Title",
  outdoor_unit: "Outdoor unit",
  heat_source: "Heat source",
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
      outdoor_unit: this._config.outdoor_unit || AUTO,
      heat_source: this._config.heat_source || AUTO,
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
    for (const k of ["outdoor_unit", "heat_source"]) {
      if (v[k] && v[k] !== AUTO) config[k] = v[k];
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
    type: "aciq-system-card", name: "ACiQ System Card", description: "Animated schematic of your HVAC equipment",
    preview: true, documentationURL: "https://github.com/oxn0r/ha-aciq-thermostat#system-card",
  });
  console.info(`%c ACIQ-SYSTEM-CARD %c v${VERSION} `, "background:#ff6b3d;color:#fff", "");
}
