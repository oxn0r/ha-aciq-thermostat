// ACiQ System Card logic, without a browser: node --test tests/frontend/
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
    [n("_blower")]: s(st.blow ? "on" : "off"), [n("_fault")]: s("off"), [n("_fault_codes")]: s("none"),
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
};

for (const [name, st] of Object.entries(STATES)) {
  test(`renders ${name}`, () => {
    const c = card({ entities: { filter: "sensor.furnace_filter_life_used" } }, hassFor(system("family_room"), st));
    const html = c.shadowRoot.innerHTML;
    const head = html.match(/<span class="head">([^<]*)<\/span>/)[1];
    assert.equal(head, { heat: "Heating · heat pump 58 Hz", cool: "Cooling · heat pump 47 Hz",
      furnace: "Heating · gas furnace", idle: "Idle" }[name]);
    assert.equal(/class="spin" style="transform-origin:56px 134px/.test(html), !!st.comp);     // outdoor fan
    assert.equal(/class="spin" style="transform-origin:205px 193px/.test(html), !!st.blow);    // blower
    assert.equal(html.includes('class="flame"'), !!st.furn);
    if (st.comp) {
      assert.ok(html.includes(`class="flow ${name === "cool" ? "runrev" : "run"}" d="M104 150 H170"`));
      assert.ok(html.includes(`stroke="${name === "cool" ? "#2b8cff" : "#ff6b3d"}" d="M104 150 H170"`));
    }
    assert.ok(html.includes("Filter 42%"));
  });
}

test("editor maps the form to the card config", () => {
  const ed = new (customElements.get("aciq-system-card-editor"))();
  ed.setConfig({ type: "custom:aciq-system-card", prefix: "family_room", entities: { filter: "sensor.f" } });
  ed.hass = { entities: {}, states: {} };
  assert.deepEqual(ed._form.data, { device_id: undefined, title: undefined, power: undefined, filter: "sensor.f" });
  ed._changed({ device_id: "dev1", title: "HVAC", power: "sensor.p", filter: "" });
  assert.deepEqual(ed.events.at(-1).detail.config,
    { type: "custom:aciq-system-card", device_id: "dev1", title: "HVAC", entities: { power: "sensor.p" } });
});
