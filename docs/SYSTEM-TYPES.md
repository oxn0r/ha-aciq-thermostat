# System types

Which equipment a TL04-1 can run, how the integration tells them apart, and what the
[system card](../README.md#system-card) draws for each. The **System type** sensor (diagnostic) reports the result, and
its attributes show the signals it used (`detected_from`).

## How it's detected

| Wiring | Signal | What it tells |
|---|---|---|
| Communicating (HA/HB to an inverter indoor unit) | `aux_heat_open`, the installer's *backup heat* setting | 0: no backup heat, the indoor unit is an **air handler**. 1: backup heat installed, drawn as a **furnace** under the coil |
| Conventional 24 V | wired terminals (`terminal_state`) | **O/B**: heat pump. **Y1** without O/B: air conditioner. **Y2** / **W2**: second stage. **W1/W2**: heat. **G**: a blower (no G: a boiler with radiators) |
| Conventional 24 V | `Taux`, the thermostat's *aux heat type* setting | dev_app's `AUX_TYPE` enum: **1** electric, **2** furnace, **3** boiler. Not reported: W1 reads as a furnace |

Every communicating system seen so far has an inverter heat pump outdoors. On those systems the indoor unit doesn't
say what its backup heat is. A communicating air handler with **electric heat strips** therefore looks like a
furnace system: set `heat_source: electric` on the card (or *Heat source* in its editor).

## Systems

| System type | Outdoor | Indoor | Detected from | Seen on |
|---|---|---|---|---|
| Heat pump + furnace (dual fuel) | heat pump | coil on a furnace | communicating, `aux_heat_open` 1; 24 V O/B + W1, Taux 2 | ACiQ heat pump + gas furnace (communicating) |
| Heat pump | heat pump | air handler | communicating, `aux_heat_open` 0; 24 V O/B, no W | Midea EVOX G3 ([#8](https://github.com/oxn0r/ha-aciq-thermostat/issues/8)), Midea ([#13](https://github.com/oxn0r/ha-aciq-thermostat/issues/13)), [#16](https://github.com/oxn0r/ha-aciq-thermostat/issues/16) (communicating) |
| Heat pump + electric heat | heat pump | air handler with heat strips | 24 V O/B + W1, Taux 1; communicating: card setting | – |
| Heat pump + boiler (hydro-air) | heat pump | air handler with a hot-water coil | 24 V O/B + W1 + G, Taux 3 | two-stage heat pump, hydronic boiler on W1 ([#5](https://github.com/oxn0r/ha-aciq-thermostat/issues/5)) |
| Air conditioner + furnace | air conditioner | coil on a furnace | 24 V Y1 + W1, no O/B | – |
| Air conditioner + electric heat | air conditioner | air handler with heat strips | 24 V Y1 + W1, Taux 1 | – |
| Air conditioner + boiler | air conditioner | air handler with a hot-water coil | 24 V Y1 + W1 + G, Taux 3 | – |
| Air conditioner | air conditioner | air handler | 24 V Y1, no W | – |
| Furnace | – | furnace | 24 V W1, no Y | – |
| Electric heat | – | air handler with heat strips | 24 V W1 + G, Taux 1 | – |
| Boiler | – | boiler heating radiators | 24 V W1, no G, Taux 3 | – |

No ductless mini-splits have turned up: every report so far has been a ducted system.

If the card draws your system wrong, please [open an issue](https://github.com/oxn0r/ha-aciq-thermostat/issues/new/choose)
with a diagnostics download and a short description of your equipment.
