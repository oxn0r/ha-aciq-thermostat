<p align="center">
  <img src="custom_components/aciq_thermostat/brand/icon@2x.png" alt="ACiQ Thermostat (Local)" width="128">
</p>

<h1 align="center">ACiQ Thermostat (Local)</h1>

<p align="center">
  <a href="https://github.com/hacs/integration"><img src="https://img.shields.io/badge/HACS-Custom-41BDF5.svg" alt="HACS Custom"></a>
  <a href="https://github.com/oxn0r/ha-aciq-thermostat/releases"><img src="https://img.shields.io/github/v/release/oxn0r/ha-aciq-thermostat" alt="Release"></a>
  <a href="https://github.com/oxn0r/ha-aciq-thermostat/actions/workflows/validate.yml"><img src="https://github.com/oxn0r/ha-aciq-thermostat/actions/workflows/validate.yml/badge.svg" alt="Validate"></a>
  <img src="https://img.shields.io/badge/quality%20scale-platinum%20rules-e5e4e2" alt="Follows the Platinum quality-scale rules">
  <img src="https://img.shields.io/badge/iot__class-local__polling-brightgreen" alt="Local polling">
  <a href="LICENSE"><img src="https://img.shields.io/github/license/oxn0r/ha-aciq-thermostat" alt="MIT License"></a>
</p>

**Control your ACiQ thermostat from Home Assistant: locally, without the cloud.**

## In short
**The problem:** the ACiQ / Midea **TL04-1** Wi-Fi thermostat (also sold as "AC Pro") only works with
the ACiQ cloud app. It has no Home Assistant integration and no local API, and existing Midea integrations
(`midea_ac_lan`, msmart) don't support it.

**The solution:** the thermostat runs Linux with an open debug port (ADB). This integration uses that port
to put a tiny local API on the thermostat, then talks to it directly. No cloud, no account, no extra hardware.

**What you get in Home Assistant:**
- 🌡️ A normal thermostat card: Off / Auto / Heat / Cool / Fan, emergency heat, fan speed, setpoint
- 🔥 **Is the gas furnace burning? Is the heat pump running, and how hard?** (compressor Hz)
- 🌤️ **Outdoor temperature** from the outdoor unit, plus coil temperatures, blower state and fault codes
- Works with HomeKit, automations, dashboards, energy tracking

**Is this for me?** Yes, if your thermostat says **TL04-1** on the box or in the ACiQ app, and it's wired to a Midea or ACiQ
indoor unit (communicating HA/HB wiring, typical for ACiQ heat pump, air handler and furnace systems).
Other setups: see [Supported devices](#supported-devices).

### Quick start (5 minutes)
1. Give the thermostat a fixed IP in your router (its hostname starts with `midea_`).
2. In Home Assistant: **HACS → ⋮ → Custom repositories →** `https://github.com/oxn0r/ha-aciq-thermostat`
   (type *Integration*) → find **ACiQ Thermostat (Local)** → **Download** → restart Home Assistant.
3. **Settings → Devices & services.** The thermostat usually appears as *Discovered*. If not, use **Add integration →
   ACiQ Thermostat (Local)** and enter its IP.
4. Review the safety checks it runs and press **Submit**. Done.

> Recommended: block the thermostat's internet access in your router afterwards. The debug port it relies on is open to your
> whole network, and blocking the internet also stops firmware updates that might close it. See [Security](#security).

Want the details? Everything below explains how it works, what's verified, and how to troubleshoot.

> **Credit:** this project exists because of [jeffmcfadden/midea-thermostat](https://github.com/jeffmcfadden/midea-thermostat).
> It discovered the TL04-1's open root ADB access, reverse-engineered the `dev_app` IPC/msmart message
> format, and wrote the original `thermostat_api` daemon this integration's daemon is derived from.

---

## Contents
- [Supported devices](#supported-devices)
- [How it works](#how-it-works)
- [Installation](#installation)
- [Configuration](#configuration)
- [Entities](#entities)
- [Actions, triggers and conditions](#actions-triggers-and-conditions)
- [Data updates](#data-updates)
- [System card](#system-card)
- [Examples and use cases](#examples-and-use-cases)
- [Known limitations](#known-limitations)
- [Troubleshooting](#troubleshooting)
- [Security](#security)
- [Removal](#removal)
- [Quality](#quality)
- [Development](#development)
- [Credits and license](#credits-and-license)

## Supported devices
| Device | Status |
|---|---|
| ACiQ / Midea **TL04-1**, Tina Linux build `tina.tangly.20260314.152301`, communicating (HA/HB) wiring to a Midea indoor unit | **Verified**: full control and equipment data |
| Same thermostat on build `tina.tangly.20260821.063106` (e.g. Midea-branded) | **Supported** since 1.1.1 ([#8](https://github.com/oxn0r/ha-aciq-thermostat/issues/8)): control, mode, setpoints, temperature, humidity; equipment data (outdoor unit, coils, compressor, blower, furnace, faults) since 1.1.3. Each command is checked against what the thermostat reports |
| Same thermostat on another firmware build | New setups stop at pre-flight (open an issue with its output); a thermostat that updates itself after setup keeps readings, with control blocked (see [Safety](#safety)) |
| TL04-1 wired conventionally (24 V R/C/W/Y/G), e.g. Carrier KSACN1701AAA | **Supported** since 1.1.0 ([#5](https://github.com/oxn0r/ha-aciq-thermostat/issues/5)): mode (Off, Heat, Cool, Auto, Fan only, Emergency heat), setpoint, Auto low/high, room temperature and the 24 V outputs it drives. No fan speed (single fan output) and no equipment data. Tested on a heat pump with hydronic aux heat; other 24 V setups, please report |
| Other Midea / NetHome / Cooper & Hunter thermostats | Not supported |

Verified hardware: TL04-1 hardware V4.3, screen firmware LD:1.2.60, indoor unit firmware
`00AC000111171171000066111008`, with an ACiQ dual-fuel system (heat pump + gas furnace).

## How it works
The TL04-1 runs Linux and exposes an unauthenticated root **ADB** shell on TCP 5555. The integration:

1. **Pre-flight (read-only):** connects over ADB (pure Python, works on Home Assistant OS) and checks
   - the root shell
   - the firmware build and the `dev_app` binary hash
   - the IPC queues
   - communicating mode
2. **Deploy:** copies a 65 KB daemon (`thermostat_api`, built reproducibly from [`daemon/`](daemon/)) to the
   thermostat's **RAM disk** (`/tmp`). It verifies the checksum, then starts it with an IP allowlist of Home Assistant only.
   **Nothing persistent on the thermostat changes.**
3. **Poll and control:** reads the daemon's local HTTP API every 30 s. Commands are the same msmart frames the ACiQ app sends
   through the cloud. Mode, fan and power frames are byte-for-byte captures from the app.
4. **Self-heal:** a thermostat reboot clears `/tmp`, so the integration redeploys automatically.

### Safety
- **Firmware guard:** control works only on the verified build. On any other build, the daemon serves readings, rejects
  commands, and HA raises a **Repairs** issue.
- **All-or-nothing commands:** a request is fully validated before anything is sent.
- **Fan lock:** fan speed changes only in Heat or Cool, matching the thermostat's own UI.
- `dev_app`, `msmart`, `meiju` and the firmware are **never** modified, restarted or replaced.

## Installation
**Before you start:** give the thermostat a DHCP reservation, and make sure Home Assistant can reach it on
TCP **5555** (ADB) and **8080** (API).

### HACS (recommended)
1. HACS → ⋮ → **Custom repositories** → `https://github.com/oxn0r/ha-aciq-thermostat`, category **Integration**.
2. Search for **ACiQ Thermostat (Local)** → **Download** → restart Home Assistant.
3. The thermostat is usually **discovered automatically** (DHCP hostname `midea_*`). Otherwise:
   **Settings → Devices & services → Add integration → ACiQ Thermostat (Local)**.

### Manual
Copy `custom_components/aciq_thermostat` into your `config/custom_components/`, then restart Home Assistant.

## Configuration
### Setup fields
| Field | Default | Description |
|---|---|---|
| IP address | (required) | Thermostat IP (hostname `midea_xx_xxxx` in your router) |
| Name | Thermostat | Device name, e.g. *Family Room* |
| API port | 8080 | Port the on-device daemon listens on |
| ADB port | 5555 | Thermostat's ADB port |

You'll then see the pre-flight results. Submitting deploys the daemon.

### Options (Settings → Devices & services → ACiQ Thermostat → Configure)
| Option | Default | Description |
|---|---|---|
| Automatically (re)deploy the daemon | on | Deploy at startup and after thermostat reboots |
| Keep the thermostat clock in sync with Home Assistant | on | Corrects drift ≥ 2 s after reboots and every 6 h (the thermostat can't use NTP with its internet blocked) |
| Block the thermostat's internet at the source | off | Runtime firewall rule on the thermostat: only LAN/HA traffic leaves it (see [Router flooded](#router-flooded-with-dns-lookups)) |
| Extra allowed IPs | (empty) | Other hosts allowed to use the API directly (HA's own IP is always allowed) |

**Reconfigure** (⋮ → Reconfigure) changes the IP or ports. It re-runs the checks and refuses if a different thermostat answers.

## Entities
| Entity | Type | Notes |
|---|---|---|
| *Thermostat* | climate | Off / Auto / Heat / Cool / Fan only · preset **Emergency heat** · fan Auto/Low/Medium/High (Heat & Cool) · 60–86 °F or 16–30 °C (follows the thermostat's unit) · Auto shows one setpoint or a heat/cool range, following the thermostat (see below) |
| Temperature · Humidity | sensor | thermostat's compensated sensor |
| **Outdoor temperature** | sensor | outdoor unit T4 |
| Indoor coil / Outdoor coil temperature | sensor | T2A / T3 |
| **Compressor frequency** | sensor (Hz) | inverter speed; 0 = off. Attributes: raw indoor-unit bits for mapping defrost ([#9](https://github.com/oxn0r/ha-aciq-thermostat/issues/9)) |
| Blower stage | sensor | 0 off, 1 low … 4 (high stages seen with the furnace) |
| Setpoint · Mode · Fan setting | sensor | |
| **Furnace** | binary_sensor | **burner on**; verified against the furnace firing |
| Compressor · Blower | binary_sensor | running |
| Fault · Fault codes | binary_sensor / sensor | equipment fault or protection state |
| Power · Emergency heat | binary_sensor | |
| Redeploy daemon · Collect debug report | button | maintenance; *Collect debug report* runs pre-flight plus the build details below and adds them to the next diagnostics download (takes ~10 s; the page shows no pop-up) |
| Last debug report | diagnostic | when *Collect debug report* last finished; attributes: pre-flight result, failed checks, whether `dev_app` has debug info |
| Firmware · Indoor unit firmware · Last thermostat boot · Daemon started · Clock offset · Local control enabled · Aux heat allowed · Wi-Fi signal · Indoor unit T1 · Auto state | diagnostic | *Aux heat allowed* = backup (furnace) heat permitted in the current mode (on in Heat), not burning; the last three are disabled by default |
| Auto setpoint type · Auto heat setpoint · Auto cool setpoint · Deadband | diagnostic | *One setpoint* or *Heat/cool range*; the heat/cool setpoints are shown only while Auto holds a range |

### Auto: one setpoint or a heat/cool range
The thermostat's Auto either holds **one setpoint** (with its deadband) or a **heat/cool range**, depending on its own
setting. The integration follows it automatically, with nothing to configure: in Auto the card shows one target, or
low/high targets, and switches within a second or two if the setting changes. Heat and Cool always use one setpoint.
Range writes are checked: if the thermostat doesn't report the new range (it clamps values to its limits), you get an
error with the values it kept. Apple Home builds its controls when HomeKit Bridge starts, so **reload HomeKit Bridge**
after changing this setting on the thermostat.

The climate entity's **hvac_action** combines furnace, compressor and blower: *heating*, *cooling*, *fan* (blower only), *idle* or *off*.

## Actions, triggers and conditions
The integration adds no custom actions. Use the standard `climate.*` actions (`set_temperature`, `set_hvac_mode`,
`set_fan_mode`, `set_preset_mode`, `turn_on`, `turn_off`) and `button.press`. Triggers and conditions use the
regular state, numeric-state and device triggers on the entities above.

## Data updates
**Push-style:** the integration long-polls the on-device daemon (`/api/state?wait=…`), which answers as soon as the
mode, setpoints, fan, auxiliary/emergency heat, Auto decision or equipment run state changes, so edits made on the wall
unit show up in ~1–2 s. Room temperature and humidity arrive with every answer (at least every 25 s). A 30 s watchdog
poll handles outages and older daemons. The daemon reads:
- the thermostat's own state log (mode, setpoint, room conditions), and
- the indoor/outdoor equipment state that `dev_app` keeps in memory. That state updates every few seconds from the HA/HB bus.

Equipment entities go **unavailable** if that data can't be read (for example, on an unverified firmware).

## System card

The integration includes the **ACiQ System Card**, an animated schematic of a ducted heat pump system with gas furnace backup: outdoor unit, refrigerant lines, indoor coil on the furnace, blower, ducts and the house.

![ACiQ System Card: heat pump heating, cooling, gas furnace and idle](https://raw.githubusercontent.com/oxn0r/ha-aciq-thermostat/main/docs/images/system-card-all.gif)

![The four states as a still image](https://raw.githubusercontent.com/oxn0r/ha-aciq-thermostat/main/docs/images/system-card.png)

<details><summary>Dark theme, and one state per image</summary>

![System card, dark theme](https://raw.githubusercontent.com/oxn0r/ha-aciq-thermostat/main/docs/images/system-card-all-dark.gif)

| Heat pump heating | Cooling | Gas furnace |
|---|---|---|
| ![Heating](https://raw.githubusercontent.com/oxn0r/ha-aciq-thermostat/main/docs/images/system-card-heat.gif) | ![Cooling](https://raw.githubusercontent.com/oxn0r/ha-aciq-thermostat/main/docs/images/system-card-cool.gif) | ![Gas furnace](https://raw.githubusercontent.com/oxn0r/ha-aciq-thermostat/main/docs/images/system-card-furnace.gif) |

</details>

- The outdoor fan spins while the compressor runs, faster at higher compressor frequency.
- Refrigerant lines turn red when heating and blue when cooling, with the flow reversing in cool.
- A flame shows when the gas furnace is on, and the blower spins while it runs.
- The house shows room temperature, setpoint and humidity. Chips show mode, blower stage, fault, and optionally power and filter use.
- Tap any part to open that entity.

**No Lovelace resource is needed.** The integration serves the card and loads it in every dashboard. After an update, reload the browser once to get the new card. Add it from the dashboard's card picker (*ACiQ System Card*, with a visual editor), or in YAML:

```yaml
type: custom:aciq-system-card
title: HVAC system                  # optional
device_id: 0123456789abcdef         # optional: needed only with more than one ACiQ thermostat
entities:                           # optional overrides; null hides one
  power: sensor.heat_pump_power     # power chip (any power sensor)
  filter: sensor.furnace_filter_life_used   # filter chip, % used (warns at 90, red at 100)
```

The card finds the thermostat's entities through the device, so it works whatever the device or its entities are named. How each part is chosen, first match wins:
1. `entities:` override for that part
2. `device_id`
3. `prefix:` (entity ids like `sensor.<prefix>_temperature`, as in earlier card versions)
4. the only ACiQ thermostat, if there's exactly one
5. prefix `family_room`

Override keys: `climate`, `room`, `humidity`, `setpoint`, `mode`, `outdoor`, `outdoor_coil`, `indoor_coil`, `return_air`, `hz`, `blower_stage`, `compressor`, `furnace`, `blower`, `fault`, `fault_codes`, `power`, `filter`.

The schematic shows what the equipment data reports, so it needs firmware with equipment data (see [Supported devices](#supported-devices)). Without it, the house and mode still show, and the equipment stays idle.

### Upgrading from the standalone card

If you loaded `aciq-system-card.js` yourself (for example from `/local/aciq/aciq-system-card.js`):
1. Remove that dashboard resource (Settings → Dashboards → ⋮ → Resources).
2. Delete the file from `/config/www/aciq/`.
3. Keep your dashboard YAML. `type: custom:aciq-system-card` with `prefix:`, `title:` and `entities:` works unchanged.

If the old resource stays, whichever copy loads first is used and the other is ignored, so nothing breaks. You just may not get the bundled version.

## Examples and use cases
**Know which heat source is running (dual fuel):**
```yaml
template:
  - sensor:
      - name: Heat source
        state: >
          {% if is_state('binary_sensor.family_room_furnace', 'on') %}Gas furnace
          {% elif is_state('binary_sensor.family_room_compressor', 'on') %}Heat pump
          {% else %}Idle{% endif %}
```

**Notify on an equipment fault:**
```yaml
automation:
  - alias: HVAC fault
    triggers:
      - trigger: state
        entity_id: binary_sensor.family_room_fault
        to: "on"
    actions:
      - action: notify.notify
        data:
          message: "HVAC fault: {{ states('sensor.family_room_fault_codes') }}"
```

**Night setback:**
```yaml
automation:
  - alias: Night setback
    triggers:
      - trigger: time
        at: "22:30:00"
    actions:
      - action: climate.set_temperature
        target:
          entity_id: climate.family_room
        data:
          temperature: 66
```

Other uses: chart compressor Hz against outdoor temperature to see how the heat pump modulates; count furnace
runtime (*history_stats*) to estimate gas use; alert when the outdoor coil stays near freezing (defrost behaviour).

## Known limitations
- Setpoints are whole degrees: 60–86 °F, or 16–30 °C when the thermostat is set to Celsius (both supported; the integration follows the thermostat's unit).
- **Auto heat/cool range** support is new in 1.1.0: verified from a user's logs, a write test on a single-setpoint
  TL04-1 and on a 24 V unit running Auto with a range; not yet confirmed on a communicating unit running a range.
- **24 V thermostats** are new in 1.1.0 and tested on one setup (heat pump with hydronic aux heat). A cooling run on
  real equipment hasn't been seen yet. `recent_log` in diagnostics covers only the thermostat's own log window
  (a few minutes): download diagnostics soon after the problem.
- In **Heat**, a setpoint far above room temperature makes the system go straight to the furnace (dual-fuel staging).
  That's the equipment's behaviour, not the integration's.
- Fan speed can't be changed in Auto or Fan-only (the thermostat locks it).
- Schedules, vacation mode and installer settings aren't exposed.
- The thermostat's adbd stalls when **two** ADB hosts connect at once. Keep other ADB tools disconnected.
- A firmware update could close ADB. Block the thermostat's internet access to prevent updates (see [Security](#security)).

### Outages
If the API stops answering, the integration checks why and logs one line, for example:
`Thermostat API on 10.0.0.65 unavailable: TimeoutError; TCP 8080 timeout, ADB 5555 timeout -> thermostat unreachable on the network (Wi-Fi drop or power loss)`.
*Network down:* waits for the thermostat to come back (re-checks every ≤10 s). *Daemon gone* (thermostat rebooted):
redeploys immediately. *Daemon hung:* forces a restart. Diagnostics show the last outages and deploys, and the
*Last thermostat boot* sensor makes reboots obvious. The TL04-1's Wi-Fi radio (XR819) occasionally faults and recovers
on its own within a few minutes; a stable 2.4 GHz access point helps.

### Router flooded with DNS lookups?
With the thermostat's internet blocked, the ACiQ cloud client (`msmart`) retries DNS endlessly (8.8.8.8, 1.1.1.1, …),
around 40 lookups per second. Turn on **Configure → Block the thermostat's internet at the source**: a temporary
firewall rule on the thermostat drops everything except your LAN and Home Assistant, so the traffic never reaches your
router. It's re-applied after reboots and removed when you turn it off. There's also a **Block cloud traffic** switch on
the device page for one-tap on/off.

Only public internet destinations are dropped (NTP on UDP 123 is let through so a router-side NTP intercept can answer). Local traffic is never filtered: your LAN, every private range
(10/8, 172.16/12, 192.168/16), link-local, 100.64/10 (VPNs such as Tailscale), Home Assistant, broadcast and multicast.
The rule lives in the thermostat's memory only, so a power cycle always clears it. The integration verifies it after
every reboot and every 30 minutes.

Measured on a TL04-1: the cloud client's CPU use dropped from ~23 % to ~2 % of a core, its DNS traffic from ~40
packets/s to zero, and system load fell noticeably.

## Troubleshooting
| Symptom | Fix |
|---|---|
| Setup says *Could not reach the thermostat over ADB* | Check IP and port 5555 reachability from HA; disconnect any other `adb` session; power-cycle the thermostat if adbd is stuck |
| *Pre-flight checks failed* | Firmware or device differs from the verified build. A debug report was saved to `/config`: [open a new-firmware issue](https://github.com/oxn0r/ha-aciq-thermostat/issues/new?template=new_firmware.yml) and attach it (see [Supporting a new firmware](#supporting-a-new-firmware-or-reporting-a-problem)) |
| Reporting a problem | Make the change, then right away press **Collect debug report** and download diagnostics, and attach the file to a [bug report](https://github.com/oxn0r/ha-aciq-thermostat/issues/new?template=bug_report.yml). No ADB capture is needed |
| Entities unavailable after a thermostat reboot | Wait ~1 minute (auto-redeploy) or press **Redeploy daemon** |
| Repairs: *firmware changed — local control disabled* | The thermostat updated itself. Readings continue. Press **Collect debug report**, download diagnostics and [open a new-firmware issue](https://github.com/oxn0r/ha-aciq-thermostat/issues/new?template=new_firmware.yml) |
| Commands ignored / 400 errors | See the HA log; the daemon reports why (validation, fan lock, firmware guard) |

**Diagnostics:** device page → ⋮ → *Download diagnostics* (state, versions, pre-flight results, deploy log,
command log, recent thermostat log lines, and the debug report if one was collected).

### Supporting a new firmware or reporting a problem
One file is meant to be enough, without ADB commands or back and forth:
1. **Thermostat set up:** on its device page press **Collect debug report**. It takes about 10 seconds and shows no
   pop-up; when it's done, *Last debug report* shows the time and a notification appears under the **bell**
   (Notifications) in the sidebar. Then ⋮ → **Download diagnostics**.
   **Setup failed at pre-flight:** the integration already saved `/config/aciq_thermostat_debug_report_<date>.json`.
2. [Open a new-firmware issue](https://github.com/oxn0r/ha-aciq-thermostat/issues/new?template=new_firmware.yml)
   (or a [bug report](https://github.com/oxn0r/ha-aciq-thermostat/issues/new?template=bug_report.yml)) and attach the file.

The debug report is read-only and leaves out IP addresses and the serial number. It contains the firmware and
binary hashes, kernel and memory, the thermostat processes, the log window and which functions log how often, whether
Home Assistant is behind NAT (Docker), and, from a copy of `dev_app` parsed in Home Assistant, its symbols, the layout
of its state block (`g_self_state`) and a dump of that block with the serial zeroed. That's what's needed to map
equipment data and new features (like defrost) on a new build. The `dev_app` binary itself isn't included.
Debug logging:
```yaml
logger:
  logs:
    custom_components.aciq_thermostat: debug
```

## Security
ADB on 5555 is a **root shell with no password** for anyone who can reach it. That's a device property, not something this integration adds. Recommended:
- Block the thermostat's internet access (this also stops OTA updates). Optionally allow NTP (UDP 123).
- Allow only Home Assistant → thermostat TCP 5555 and 8080. Same-subnet traffic often bypasses router firewalls,
  so test from another device that `nc -z <thermostat-ip> 5555` **fails**, or put IoT devices on their own VLAN.
- The API daemon accepts only allowlisted client IPs.

## Removal
1. **Settings → Devices & services → ACiQ Thermostat (Local) → ⋮ → Delete.** This also stops the daemon on the thermostat.
   It lived in RAM only, so nothing remains after the next thermostat reboot either.
2. Remove the repository from HACS (HACS → the integration → ⋮ → Remove) and restart Home Assistant.

## Quality
This custom integration follows every rule of Home Assistant's
[Integration Quality Scale](https://developers.home-assistant.io/docs/core/integration-quality-scale/) through
**Platinum**. The status of each rule is in [`quality_scale.yaml`](custom_components/aciq_thermostat/quality_scale.yaml).
Official tiers are awarded only to core integrations. Highlights:
- config flow with DHCP discovery, reconfigure and options
- runtime data and unique IDs, diagnostics, Repairs issues
- entity, exception and icon translations
- `mypy --strict`, async dependencies and an injected web session
- **>95 % test coverage**, with a simulated thermostat for the ADB layer, plus an optional live end-to-end test

## Development
```sh
pip install pytest-homeassistant-custom-component "adb-shell[async]>=0.4.4" mypy pytest-cov
pytest --ignore=tests/live --cov                 # unit tests (>95 % coverage required)
mypy                                             # strict typing
node --test tests/frontend/card.test.mjs         # system card logic
ACIQ_LIVE_HOST=<ip> pytest tests/live -s         # end-to-end against a real thermostat
daemon/build.sh                                  # rebuild the daemon (official zig 0.17.0, Linux x86_64 = canonical)
```

Card images in `docs/images/` are generated from the card's own code: see [tools/card_media](tools/card_media/README.md).

## Credits and license
- **[jeffmcfadden/midea-thermostat](https://github.com/jeffmcfadden/midea-thermostat)**: ADB access, IPC protocol, temperature
  frame, original daemon and HA component.
- This project added communicating-system (HBS/AHU3) support, mode/fan/off frames captured from the ACiQ app, the equipment
  data (outdoor temperature, coils, compressor, furnace, blower, faults), the safety guards, and the self-deploying HACS integration.
- Not affiliated with ACiQ, Midea or Home Assistant. "ACiQ" is used only to identify compatible hardware.

Licensed under the [MIT License](LICENSE). Portions are derived from jeffmcfadden/midea-thermostat, which is public domain (Unlicense).
