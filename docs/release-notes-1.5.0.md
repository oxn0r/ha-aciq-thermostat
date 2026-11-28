## The system card now draws your own equipment

Until now the System Card drew every system as a heat pump with a gas furnace. In 1.5.0 it draws what your
thermostat actually runs, adding and removing parts to match:

- **Outdoor:** a heat pump, an air conditioner, or nothing (furnace or boiler alone)
- **Indoor:** a coil on a **furnace**, an **air handler** (with or without **electric heat strips** or a
  **boiler's hot-water coil**), or a **boiler heating radiators**
- **Animations:** the outdoor fan follows compressor speed (or stage 1 / 2 on 24 V). Refrigerant runs red in heat
  and blue in cool. Flames burn in the furnace or boiler, heat strips glow, hot water flows to the coil or radiators,
  and the blower spins. Light and dark themes, desktop and phone.
- A chip names the detected system, e.g. *Heat pump + furnace*.

**Nothing to set up:** after updating, reload your browser once. Existing card YAML keeps working.

### Every system, in every state it shows
#### Heat pump + furnace (dual fuel)
![Heat pump + furnace (dual fuel)](https://raw.githubusercontent.com/oxn0r/ha-aciq-thermostat/main/docs/images/systems/hp-furnace.gif)

#### Heat pump + air handler
![Heat pump + air handler](https://raw.githubusercontent.com/oxn0r/ha-aciq-thermostat/main/docs/images/systems/hp.gif)

#### Heat pump + electric heat strips
![Heat pump + electric heat strips](https://raw.githubusercontent.com/oxn0r/ha-aciq-thermostat/main/docs/images/systems/hp-electric.gif)

#### Heat pump + boiler (hydro-air), 24 V two-stage
![Heat pump + boiler (hydro-air), 24 V two-stage](https://raw.githubusercontent.com/oxn0r/ha-aciq-thermostat/main/docs/images/systems/hp-boiler.gif)

#### Heat pump + air handler, 24 V
![Heat pump + air handler, 24 V](https://raw.githubusercontent.com/oxn0r/ha-aciq-thermostat/main/docs/images/systems/hp-24v.gif)

#### Heat pump + furnace, 24 V
![Heat pump + furnace, 24 V](https://raw.githubusercontent.com/oxn0r/ha-aciq-thermostat/main/docs/images/systems/hp-furnace-24v.gif)

#### Air conditioner + furnace
![Air conditioner + furnace](https://raw.githubusercontent.com/oxn0r/ha-aciq-thermostat/main/docs/images/systems/ac-furnace.gif)

#### Air conditioner + electric heat
![Air conditioner + electric heat](https://raw.githubusercontent.com/oxn0r/ha-aciq-thermostat/main/docs/images/systems/ac-electric.gif)

#### Air conditioner + boiler (hydro-air)
![Air conditioner + boiler (hydro-air)](https://raw.githubusercontent.com/oxn0r/ha-aciq-thermostat/main/docs/images/systems/ac-boiler.gif)

#### Air conditioner only
![Air conditioner only](https://raw.githubusercontent.com/oxn0r/ha-aciq-thermostat/main/docs/images/systems/ac.gif)

#### Furnace only
![Furnace only](https://raw.githubusercontent.com/oxn0r/ha-aciq-thermostat/main/docs/images/systems/furnace.gif)

#### Electric heat only
![Electric heat only](https://raw.githubusercontent.com/oxn0r/ha-aciq-thermostat/main/docs/images/systems/electric.gif)

#### Boiler with radiators
![Boiler with radiators](https://raw.githubusercontent.com/oxn0r/ha-aciq-thermostat/main/docs/images/systems/boiler.gif)

<details><summary>Dark theme</summary>

#### Heat pump + furnace (dual fuel)
![Heat pump + furnace (dual fuel), dark](https://raw.githubusercontent.com/oxn0r/ha-aciq-thermostat/main/docs/images/systems/hp-furnace-dark.gif)

#### Heat pump + air handler
![Heat pump + air handler, dark](https://raw.githubusercontent.com/oxn0r/ha-aciq-thermostat/main/docs/images/systems/hp-dark.gif)

#### Heat pump + electric heat strips
![Heat pump + electric heat strips, dark](https://raw.githubusercontent.com/oxn0r/ha-aciq-thermostat/main/docs/images/systems/hp-electric-dark.gif)

#### Heat pump + boiler (hydro-air), 24 V two-stage
![Heat pump + boiler (hydro-air), 24 V two-stage, dark](https://raw.githubusercontent.com/oxn0r/ha-aciq-thermostat/main/docs/images/systems/hp-boiler-dark.gif)

#### Heat pump + air handler, 24 V
![Heat pump + air handler, 24 V, dark](https://raw.githubusercontent.com/oxn0r/ha-aciq-thermostat/main/docs/images/systems/hp-24v-dark.gif)

#### Heat pump + furnace, 24 V
![Heat pump + furnace, 24 V, dark](https://raw.githubusercontent.com/oxn0r/ha-aciq-thermostat/main/docs/images/systems/hp-furnace-24v-dark.gif)

#### Air conditioner + furnace
![Air conditioner + furnace, dark](https://raw.githubusercontent.com/oxn0r/ha-aciq-thermostat/main/docs/images/systems/ac-furnace-dark.gif)

#### Air conditioner + electric heat
![Air conditioner + electric heat, dark](https://raw.githubusercontent.com/oxn0r/ha-aciq-thermostat/main/docs/images/systems/ac-electric-dark.gif)

#### Air conditioner + boiler (hydro-air)
![Air conditioner + boiler (hydro-air), dark](https://raw.githubusercontent.com/oxn0r/ha-aciq-thermostat/main/docs/images/systems/ac-boiler-dark.gif)

#### Air conditioner only
![Air conditioner only, dark](https://raw.githubusercontent.com/oxn0r/ha-aciq-thermostat/main/docs/images/systems/ac-dark.gif)

#### Furnace only
![Furnace only, dark](https://raw.githubusercontent.com/oxn0r/ha-aciq-thermostat/main/docs/images/systems/furnace-dark.gif)

#### Electric heat only
![Electric heat only, dark](https://raw.githubusercontent.com/oxn0r/ha-aciq-thermostat/main/docs/images/systems/electric-dark.gif)

#### Boiler with radiators
![Boiler with radiators, dark](https://raw.githubusercontent.com/oxn0r/ha-aciq-thermostat/main/docs/images/systems/boiler-dark.gif)

</details>

### How your system is recognised
A new diagnostic sensor, **System type**, reports the equipment, read from the thermostat's own settings:
- **Communicating systems** (HA/HB wiring): the installer's *backup heat* setting. Off: a heat pump with an
  air handler. On: a heat pump with a furnace (dual fuel).
- **Conventional 24 V:** the wired terminals (O/B heat pump, Y air conditioner, Y2/W2 two-stage, W heat, G blower)
  and the thermostat's *aux heat type* (electric, furnace or boiler).

Its attributes show the outdoor unit, heat source, indoor unit, stages and the signals used, and diagnostics
include it too. All the details: [System types](https://github.com/oxn0r/ha-aciq-thermostat/blob/main/docs/SYSTEM-TYPES.md).

### Card options
Two new options, also in the card's visual editor, for systems the thermostat can't tell apart:

```yaml
type: custom:aciq-system-card
heat_source: electric        # furnace | electric | boiler | none   (default: from the thermostat)
outdoor_unit: heat_pump      # heat_pump | air_conditioner | none   (default: from the thermostat)
```

The main case: a **communicating air handler with electric heat strips** reports backup heat the same way as a
furnace, so it is drawn as a furnace until you set `heat_source: electric`.

### What changes on an existing card
- **Heat pump + furnace:** same layout as before. The refrigerant lines now run into the coil, and the card says
  *furnace* instead of *gas furnace*, since the thermostat doesn't report the fuel.
- **Heat pump without backup heat:** the furnace is gone; an air handler is drawn instead.
- **24 V systems:** the card now runs, from the compressor, stage 2, aux heat and fan calls.
- **Indoor units reporting a raw blower speed** (e.g. 118) show *Blower 118* instead of a stage.

### Tell us if it's wrong
If the card draws your equipment wrong, or you have a system that isn't here (a ductless mini-split, for example),
please [open an issue](https://github.com/oxn0r/ha-aciq-thermostat/issues/new/choose) with a diagnostics download
and a short description of your equipment.

No change to the service on the thermostat: updating does not restart anything there.
