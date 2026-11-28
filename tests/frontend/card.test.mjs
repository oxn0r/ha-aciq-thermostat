// ACiQ System Card logic, without a browser: node --test tests/frontend/card.test.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

// Minimal DOM stand-ins: enough for the card's class definitions and string rendering.
const registry = new Map();
globalThis.window = globalThis;
globalThis.HTMLElement = class {
  constructor() { this.children = []; this.events = []; }
  attachShadow() { this.shadowRoot = { innerHTML: "", querySelectorAll: () => [] }; return this.shadowRoot; }
  appendChild(c) { this.children.push(c); }
  addEventListener() {}
  dispatchEvent(ev) { this.events.push(ev); }
};
globalThis.customElements = { get: (n) => registry.get(n), define: (n, c) => registry.set(n, c) };
globalThis.CustomEvent = class { constructor(type, init) { this.type = type; this.detail = init?.detail; } };
globalThis.document = { createElement: (n) => (registry.has(n) ? new (registry.get(n))() : { tag: n, addEventListener() {} }) };
console.info = () => {};

const src = new URL("../../custom_components/aciq_thermostat/frontend/aciq-system-card.js", import.meta.url);
await import(src);
const Card = customElements.get("aciq-system-card");
const manifest = JSON.parse(readFileSync(new URL("../../custom_components/aciq_thermostat/manifest.json", import.meta.url)));

// Registry + states for one device whose entity ids use `name` (not the translation key)
function system(name, device = "dev1", states = {}) {
  const tks = {
    [`climate.${name}`]: "thermostat", [`sensor.${name}_temperature`]: "temperature",
    [`sensor.${name}_humidity`]: "humidity", [`sensor.${name}_setpoint`]: "setpoint", [`sensor.${name}_mode`]: "mode",
    [`sensor.${name}_outdoor_temperature`]: "outdoor_temperature",
    [`sensor.${name}_outdoor_coil_temperature`]: "outdoor_coil_temperature",
    [`sensor.${name}_indoor_coil_temperature`]: "indoor_coil_temperature",
    [`sensor.${name}_indoor_unit_t1_temperature`]: "indoor_unit_t1",
    [`sensor.${name}_compressor_frequency`]: "compressor_frequency", [`sensor.${name}_blower_stage`]: "blower_stage",
    [`sensor.${name}_dry_time_left`]: "dry_time_left",
    [`binary_sensor.${name}_compressor`]: "compressor", [`binary_sensor.${name}_furnace`]: "furnace",
    [`binary_sensor.${name}_blower`]: "blower", [`binary_sensor.${name}_fault`]: "fault",
    [`sensor.${name}_fault_codes`]: "fault_codes",
  };
  const entities = {};
  for (const [id, tk] of Object.entries(tks)) {
    entities[id] = { entity_id: id, device_id: device, platform: "aciq_thermostat", translation_key: tk };
  }
  return { entities, ids: Object.keys(tks), states };
}

function hassFor(sys, st) {
  const n = (k) => sys.ids.find((id) => id.endsWith(k));
  const s = (state, attributes = {}) => ({ state: String(state), attributes });
  const states = {
    [sys.ids[0]]: s(st.mode, { hvac_action: st.action }),
    [n("_temperature")]: s(70.4), [n("_humidity")]: s(41), [n("_setpoint")]: s(70), [n("_mode")]: s(st.mode),
    [n("_outdoor_temperature")]: s(st.outdoor ?? 38), [n("_outdoor_coil_temperature")]: s(30),
    [n("_indoor_coil_temperature")]: s(st.coil ?? 72), [n("_indoor_unit_t1_temperature")]: s(69),
    [n("_compressor_frequency")]: s(st.hz ?? 0), [n("_blower_stage")]: s(st.stage ?? 0),
    [n("_compressor")]: s(st.comp ? "on" : "off"), [n("_furnace")]: s(st.furn ? "on" : "off"),
    [n("_blower")]: s(st.blow ? "on" : "off"), [n("_fault")]: s(st.fault ? "on" : "off"),
    [n("_fault_codes")]: s(st.codes ?? "none"),
    [n("_dry_time_left")]: s(st.left ?? 0),
    "sensor.furnace_filter_life_used": s(st.filter ?? 42),
  };
  return { entities: sys.entities, states };
}

function card(config, hass) {
  const c = new Card();
  c.setConfig(config);
  c.hass = hass;
  return c;
}

test("card version follows the integration version", () => {
  const v = readFileSync(src, "utf8").match(/const VERSION = "([^"]+)"/)[1];
  assert.equal(v, manifest.version);
});

test("existing YAML (prefix + filter override) resolves the same ids as v0.2.2", () => {
  const sys = system("family_room");
  const c = card({ prefix: "family_room", title: "HVAC system", entities: { filter: "sensor.furnace_filter_life_used" } },
    hassFor(sys, { mode: "heat" }));
  assert.equal(c._e.climate, "climate.family_room");
  assert.equal(c._e.return_air, "sensor.family_room_indoor_unit_t1_temperature");
  assert.equal(c._e.compressor, "binary_sensor.family_room_compressor");
  assert.equal(c._e.filter, "sensor.furnace_filter_life_used");
  assert.equal(c._e.power, null);
});

test("one ACiQ device: entities found by translation key, whatever the device is named", () => {
  const sys = system("upstairs_hallway");
  const c = card({}, hassFor(sys, { mode: "cool" }));
  assert.equal(c._e.climate, "climate.upstairs_hallway");
  assert.equal(c._e.hz, "sensor.upstairs_hallway_compressor_frequency");
  assert.equal(c._e.return_air, "sensor.upstairs_hallway_indoor_unit_t1_temperature");
  assert.equal(c._e.fault, "binary_sensor.upstairs_hallway_fault");
});

test("device_id picks one of several devices; renamed entity ids still resolve", () => {
  const a = system("first", "devA"), b = system("second", "devB");
  b.entities["sensor.renamed_by_user"] = { ...b.entities["sensor.second_mode"], entity_id: "sensor.renamed_by_user" };
  delete b.entities["sensor.second_mode"];
  const hass = { entities: { ...a.entities, ...b.entities }, states: {} };
  const c = card({ device_id: "devB" }, hass);
  assert.equal(c._e.climate, "climate.second");
  assert.equal(c._e.mode, "sensor.renamed_by_user");
  assert.equal(card({}, hass)._e.climate, "climate.family_room");   // ambiguous: falls back to the old default
});

test("stub config picks the first ACiQ device", () => {
  assert.deepEqual(Card.getStubConfig({ entities: system("x", "devX").entities }), { device_id: "devX", title: "HVAC system" });
  assert.deepEqual(Card.getStubConfig({ entities: {} }), { title: "HVAC system" });
});

test("re-renders only when a watched entity changes", () => {
  const sys = system("family_room");
  const hass = hassFor(sys, { mode: "heat" });
  const c = card({}, hass);
  let renders = 0;
  const orig = c._render.bind(c);
  c._render = () => { renders++; orig(); };
  c.hass = { ...hass, states: { ...hass.states, "sensor.unrelated": { state: "1", attributes: {} } } };
  assert.equal(renders, 0);
  c.hass = { ...hass, states: { ...hass.states, "sensor.family_room_temperature": { state: "71", attributes: {} } } };
  assert.equal(renders, 1);
});

const STATES = {
  heat: { mode: "heat", action: "heating", comp: true, blow: true, hz: 58, stage: 3, coil: 98 },
  cool: { mode: "cool", action: "cooling", comp: true, blow: true, hz: 47, stage: 2, coil: 46 },
  furnace: { mode: "heat", action: "heating", furn: true, blow: true, stage: 4, coil: 70 },
  idle: { mode: "heat", action: "idle" },
  dry: { mode: "dry", action: "drying", comp: true, blow: true, hz: 40, stage: 1, coil: 45, left: 23 },
  defrost: { mode: "heat", action: "defrosting", comp: true, blow: false, hz: 80, coil: 5 },
  defrost_pause: { mode: "heat", action: "defrosting", coil: 12 },
};

for (const [name, st] of Object.entries(STATES)) {
  test(`renders ${name}`, () => {
    const c = card({ entities: { filter: "sensor.furnace_filter_life_used" } }, hassFor(system("family_room"), st));
    const html = c.shadowRoot.innerHTML;
    const head = html.match(/<span class="head">([^<]*)<\/span>/)[1];
    assert.equal(head, { heat: "Heating · heat pump 58 Hz", cool: "Cooling · heat pump 47 Hz",
      furnace: "Heating · furnace", idle: "Idle", dry: "Drying · heat pump 40 Hz · 23 min left",
      defrost: "Defrosting · heat pump 80 Hz", defrost_pause: "Defrosting" }[name]);
    const defrost = name.startsWith("defrost");                  // outdoor fan stops in a defrost
    assert.equal(/class="spin" style="transform-origin:56px 134px/.test(html), !!st.comp && !defrost);
    assert.equal(/class="spin" style="transform-origin:205px 193px/.test(html), !!st.blow);    // blower
    assert.equal(html.includes('class="flame"'), !!st.furn);
    const cooling = name === "cool" || name === "dry" || defrost;  // Dry and defrost run it reversed
    if (st.comp) {
      assert.ok(html.includes(`class="flow ${cooling ? "runrev" : "run"}" d="M104 108 H170"`));      // into the coil on the furnace
      assert.ok(html.includes(`stroke="${cooling ? "#2b8cff" : "#ff6b3d"}" d="M104 108 H170"`));
    }
    assert.equal(html.includes('class="drip"'), name === "dry");
    assert.equal(html.includes("Dry · 23 min left"), name === "dry");
    assert.ok(html.includes("Filter 42%"));
  });
}

test("fault chip shows the code only when there is one", () => {
  const chip = (st) => card({}, hassFor(system("family_room"), { mode: "heat", action: "idle", ...st }))
    .shadowRoot.innerHTML.match(/<span class="chip bad">([^<]*)<\/span>/)?.[1];
  assert.equal(chip({}), undefined);
  assert.equal(chip({ fault: true }), "Fault");                       // fault state only
  assert.equal(chip({ fault: true, codes: "unavailable" }), "Fault");
  assert.equal(chip({ fault: true, codes: "12" }), "Fault 12");
  assert.equal(chip({ fault: false, codes: "12" }), undefined);
});

test("editor maps the form to the card config", () => {
  const ed = new (customElements.get("aciq-system-card-editor"))();
  ed.setConfig({ type: "custom:aciq-system-card", prefix: "family_room", entities: { filter: "sensor.f" } });
  ed.hass = { entities: {}, states: {} };
  assert.deepEqual(ed._form.data, { device_id: undefined, title: undefined, outdoor_unit: "auto", heat_source: "auto",
    power: undefined, filter: "sensor.f" });
  ed._changed({ device_id: "dev1", title: "HVAC", outdoor_unit: "auto", heat_source: "auto", power: "sensor.p", filter: "" });
  assert.deepEqual(ed.events.at(-1).detail.config,
    { type: "custom:aciq-system-card", device_id: "dev1", title: "HVAC", entities: { power: "sensor.p" } });
  ed._changed({ device_id: "dev1", outdoor_unit: "heat_pump", heat_source: "electric" });
  assert.deepEqual(ed.events.at(-1).detail.config,
    { type: "custom:aciq-system-card", device_id: "dev1", outdoor_unit: "heat_pump", heat_source: "electric" });
});

test("animations loop seamlessly and keep their phase across re-renders", () => {
  const src = readFileSync(new URL("../../custom_components/aciq_thermostat/frontend/aciq-system-card.js", import.meta.url), "utf8");
  // each dash animation moves a whole number of its pattern's periods
  const period = (cls) => src.match(new RegExp(`\\.${cls} \\{[^}]*stroke-dasharray: (\\d+) (\\d+)`)).slice(1).reduce((a, b) => +a + +b, 0);
  assert.equal(32 % period("flow"), 0);
  assert.equal(30 % period("air"), 0);
  assert.ok(src.includes(".air.run { animation-name: airdash; }"));
  // a render pins running animations to the timeline's zero, so re-rendering doesn't restart them
  const c = card({}, hassFor(system("family_room"), STATES.heat));
  const anims = [{ playState: "running", startTime: 812 }, { playState: "paused", startTime: null }];
  c.shadowRoot.getAnimations = () => anims;
  c._render();
  assert.deepEqual(anims.map((a) => a.startTime), [0, null]);
});

// ---- equipment-aware layouts ----
// Communicating units (System type sensor + equipment entities), or 24 V (System type + output calls)
function equip(sysAttrs, st, { v24 = false } = {}) {
  const sys = system("family_room");
  const add = (id, tk) => { sys.entities[id] = { entity_id: id, device_id: "dev1", platform: "aciq_thermostat", translation_key: tk }; sys.ids.push(id); };
  add("sensor.family_room_system_type", "system_type");
  const hass = hassFor(sys, st);
  const s = (state, attributes = {}) => ({ state: String(state), attributes });
  hass.states["sensor.family_room_system_type"] = s(sysAttrs.system ?? "x", { wiring: v24 ? "24v" : "communicating", blower: true, ...sysAttrs });
  if (v24) {
    // 24 V units have no equipment-memory entities
    for (const k of ["compressor", "furnace", "blower", "fault", "fault_codes", "compressor_frequency", "blower_stage",
      "outdoor_temperature", "outdoor_coil_temperature", "indoor_coil_temperature", "indoor_unit_t1_temperature"]) {
      const id = sys.ids.find((i) => i.endsWith(`_${k}`));
      delete sys.entities[id]; delete hass.states[id];
    }
    for (const [k, tk] of [["compressor_call", "compressor_call"], ["compressor_stage_2_call", "compressor_stage_2"],
      ["aux_heat_call", "aux_heat_call"], ["fan_call", "fan_call"]]) {
      add(`binary_sensor.family_room_${k}`, tk);
      hass.states[`binary_sensor.family_room_${k}`] = s(st[tk] ? "on" : "off");
    }
  }
  return card({}, hass).shadowRoot.innerHTML;
}
const head = (html) => html.match(/<span class="head">([^<]*)<\/span>/)[1];
const sysChip = (html) => html.match(/<span class="chip sys hit" data-k="system_type">([^<]*)<\/span>/)?.[1];
const parts = (html) => ({
  outdoor: html.includes('transform-origin:56px 134px'), furnace: html.includes(">Furnace"),
  strips: html.includes("<polyline"), water: html.includes("M176 "), boiler: html.includes(">Boiler"),
  radiator: html.includes('x="300" y="170"'), ducts: html.includes('class="duct"'),
  blower: html.includes('transform-origin:205px'),
});

test("communicating heat pump with no backup heat (#8, #13, #16): air handler, no furnace", () => {
  const html = equip({ system: "heat_pump", outdoor_unit: "heat_pump", heat_source: "none" }, STATES.heat);
  assert.deepEqual(parts(html), { outdoor: true, furnace: false, strips: false, water: false, boiler: false,
    radiator: false, ducts: true, blower: true });
  assert.equal(sysChip(html), "Heat pump");
  assert.equal(head(html), "Heating · heat pump 58 Hz");
  assert.ok(html.includes('d="M104 158 H170"'));                // refrigerant into the air handler's coil
});

test("dual fuel (the author's ACiQ): coil on a furnace", () => {
  const html = equip({ system: "heat_pump_furnace", outdoor_unit: "heat_pump", heat_source: "furnace" }, STATES.furnace);
  assert.equal(sysChip(html), "Heat pump + furnace");
  assert.equal(head(html), "Heating · furnace");
  assert.ok(parts(html).furnace && html.includes('class="flame"'));
});

test("electric heat strips set on the card: strips glow with the backup call", () => {
  const sys = system("family_room");
  const hass = hassFor(sys, { ...STATES.heat, furn: true });
  const html = card({ heat_source: "electric" }, hass).shadowRoot.innerHTML;
  assert.equal(head(html), "Heating · heat pump 58 Hz + electric heat");
  assert.ok(parts(html).strips && !parts(html).furnace && html.includes('class="glow"'));
  assert.equal(sysChip(html), "Heat pump + electric heat");
});

test("24 V two-stage heat pump with a hydronic boiler (#5)", () => {
  const sysAttrs = { system: "heat_pump_boiler", outdoor_unit: "heat_pump", heat_source: "boiler", compressor_stages: 2 };
  let html = equip(sysAttrs, { mode: "heat", action: "heating", compressor_call: true, compressor_stage_2: true }, { v24: true });
  assert.equal(head(html), "Heating · heat pump stage 2");
  assert.ok(html.includes(">Stage 2<"));
  assert.ok(parts(html).water && parts(html).boiler && parts(html).blower);
  assert.ok(!html.includes("Outdoor"));                            // no outdoor sensors on 24 V
  html = equip(sysAttrs, { mode: "heat", action: "heating", aux_heat_call: true }, { v24: true });
  assert.equal(head(html), "Heating · boiler");
  assert.ok(html.includes('class="flame"') && html.includes('class="flow run" style="stroke-width:2"'));
  assert.ok(/class="spin" style="transform-origin:205px/.test(html));     // the air handler blows the hot-water coil's heat
});

test("24 V air conditioner + furnace, and air conditioner only", () => {
  let html = equip({ system: "air_conditioner_furnace", outdoor_unit: "air_conditioner", heat_source: "furnace" },
    { mode: "cool", action: "cooling", compressor_call: true }, { v24: true });
  assert.equal(head(html), "Cooling · AC stage 1");
  assert.ok(html.includes(">Air conditioner<") && parts(html).furnace);
  html = equip({ system: "air_conditioner", outdoor_unit: "air_conditioner", heat_source: "none" },
    { mode: "cool", action: "idle" }, { v24: true });
  assert.equal(head(html), "Idle");
  assert.ok(!parts(html).furnace && parts(html).outdoor);
  // an air conditioner never runs its lines red
  html = equip({ system: "air_conditioner", outdoor_unit: "air_conditioner", heat_source: "none" },
    { mode: "heat", action: "idle", compressor_call: true }, { v24: true });
  assert.ok(!html.includes("#ff6b3d"));
});

test("24 V furnace only, electric only, boiler with radiators", () => {
  let html = equip({ system: "furnace", outdoor_unit: "none", heat_source: "furnace" },
    { mode: "heat", action: "heating", aux_heat_call: true }, { v24: true });
  assert.deepEqual(parts(html), { outdoor: false, furnace: true, strips: false, water: false, boiler: false,
    radiator: false, ducts: true, blower: true });
  assert.equal(head(html), "Heating · furnace");
  html = equip({ system: "electric", outdoor_unit: "none", heat_source: "electric" },
    { mode: "heat", action: "heating", aux_heat_call: true }, { v24: true });
  assert.ok(parts(html).strips && html.includes(">Strips on<"));
  html = equip({ system: "boiler", outdoor_unit: "none", heat_source: "boiler", blower: false },
    { mode: "heat", action: "heating", aux_heat_call: true }, { v24: true });
  assert.deepEqual(parts(html), { outdoor: false, furnace: false, strips: false, water: false, boiler: true,
    radiator: true, ducts: false, blower: false });
  assert.equal(head(html), "Heating · boiler");
});

test("emergency heat and an unknown system type", () => {
  const html = equip({ system: "heat_pump_furnace", outdoor_unit: "heat_pump", heat_source: "furnace" },
    { ...STATES.furnace, mode: "emergency_heat" });
  assert.equal(head(html), "Emergency heat · furnace");
  // not reported yet: a communicating heat pump, backup heat drawn only while it runs
  const idle = equip({ system: "unknown", outdoor_unit: "heat_pump", heat_source: "unknown" }, STATES.idle);
  assert.ok(!parts(idle).furnace && parts(idle).outdoor);
});

test("a raw blower speed instead of a stage", () => {
  const html = equip({ system: "heat_pump", outdoor_unit: "heat_pump", heat_source: "none" }, { ...STATES.heat, stage: 118 });
  assert.ok(html.includes(">Blower 118<"));
  assert.ok(html.includes("animation-duration:0.75s"));
});

test("every animation period divides the 3 s media loop", () => {
  const src = readFileSync(new URL("../../custom_components/aciq_thermostat/frontend/aciq-system-card.js", import.meta.url), "utf8");
  // alternate animations take two runs a cycle; the dashes repeat every half run (2 dash periods a run)
  for (const [cls, runs] of [["glow", 2], ["flame", 2], ["drip", 1], ["run", 0.5]]) {
    const d = parseFloat(src.match(new RegExp(`\\.${cls} \\{[^}]*animation: \\w+ ([.\\d]+)s`))[1]);
    assert.equal(Math.round((3 / (d * runs)) * 1000) % 1000, 0, cls);
  }
});
