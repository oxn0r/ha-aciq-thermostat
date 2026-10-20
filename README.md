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
| Same thermostat on another firmware build | Readings only; control is blocked automatically (see [Safety](#safety)) |
| TL04-1 wired conventionally (24 V R/C/W/Y/G) | Not verified. Pre-flight warns that communicating mode wasn't detected |
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
| Extra allowed IPs | (empty) | Other hosts allowed to use the API directly (HA's own IP is always allowed) |

**Reconfigure** (⋮ → Reconfigure) changes the IP or ports. It re-runs the checks and refuses if a different thermostat answers.

## Entities
| Entity | Type | Notes |
|---|---|---|
| *Thermostat* | climate | Off / Auto / Heat / Cool / Fan only · preset **Emergency heat** · fan Auto/Low/Medium/High (Heat & Cool) · 60–86 °F |
| Temperature · Humidity | sensor | thermostat's compensated sensor |
| **Outdoor temperature** | sensor | outdoor unit T4 |
| Indoor coil / Outdoor coil temperature | sensor | T2A / T3 |
| **Compressor frequency** | sensor (Hz) | inverter speed; 0 = off |
| Blower stage | sensor | 0 off, 1 low … 4 (high stages seen with the furnace) |
| Setpoint · Mode · Fan setting | sensor | |
| **Furnace** | binary_sensor | **burner on**; verified against the furnace firing |
| Compressor · Blower | binary_sensor | running |
| Fault · Fault codes | binary_sensor / sensor | equipment fault or protection state |
| Power · Aux heat allowed · Emergency heat | binary_sensor | |
| Redeploy daemon · Run pre-flight checks | button | maintenance |
| Firmware · Indoor unit firmware · Local control enabled · Wi-Fi signal · Indoor unit T1 · Auto state (raw) | diagnostic | the last three are disabled by default |

The climate entity's **hvac_action** combines furnace, compressor and blower: *heating*, *cooling*, *fan* (blower only), *idle* or *off*.

## Actions, triggers and conditions
The integration adds no custom actions. Use the standard `climate.*` actions (`set_temperature`, `set_hvac_mode`,
`set_fan_mode`, `set_preset_mode`, `turn_on`, `turn_off`) and `button.press`. Triggers and conditions use the
regular state, numeric-state and device triggers on the entities above.

## Data updates
The integration polls the on-device daemon every **30 s**, plus an immediate refresh 2 s after each command. The daemon reads:
- the thermostat's own state log (mode, setpoint, room conditions), and
- the indoor/outdoor equipment state that `dev_app` keeps in memory. That state updates every few seconds from the HA/HB bus.

Equipment entities go **unavailable** if that data can't be read (for example, on an unverified firmware).

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
- Fahrenheit thermostat setup assumed. Setpoints are whole °F, 60–86.
- **Auto** uses the thermostat's single setpoint with its built-in deadband, not a separate heat/cool range.
- In **Heat**, a setpoint far above room temperature makes the system go straight to the furnace (dual-fuel staging).
  That's the equipment's behaviour, not the integration's.
- Fan speed can't be changed in Auto or Fan-only (the thermostat locks it).
- Schedules, vacation mode and installer settings aren't exposed.
- The thermostat's adbd stalls when **two** ADB hosts connect at once. Keep other ADB tools disconnected.
- A firmware update could close ADB. Block the thermostat's internet access to prevent updates (see [Security](#security)).

## Troubleshooting
| Symptom | Fix |
|---|---|
| Setup says *Could not reach the thermostat over ADB* | Check IP and port 5555 reachability from HA; disconnect any other `adb` session; power-cycle the thermostat if adbd is stuck |
| *Pre-flight checks failed* | Firmware or device differs from the verified build. Open an issue with the diagnostics download |
| Entities unavailable after a thermostat reboot | Wait ~1 minute (auto-redeploy) or press **Redeploy daemon** |
| Repairs: *firmware changed — local control disabled* | The thermostat updated itself. Readings continue. Open an issue so the new build can be verified |
| Commands ignored / 400 errors | See the HA log; the daemon reports why (validation, fan lock, firmware guard) |

**Diagnostics:** device page → ⋮ → *Download diagnostics* (state, versions, pre-flight results, deploy log).
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
ACIQ_LIVE_HOST=<ip> pytest tests/live -s         # end-to-end against a real thermostat
daemon/build.sh                                  # rebuild the daemon (official zig 0.17.0, Linux x86_64 = canonical)
```

## Credits and license
- **[jeffmcfadden/midea-thermostat](https://github.com/jeffmcfadden/midea-thermostat)**: ADB access, IPC protocol, temperature
  frame, original daemon and HA component.
- This project added communicating-system (HBS/AHU3) support, mode/fan/off frames captured from the ACiQ app, the equipment
  data (outdoor temperature, coils, compressor, furnace, blower, faults), the safety guards, and the self-deploying HACS integration.
- Not affiliated with ACiQ, Midea or Home Assistant. "ACiQ" is used only to identify compatible hardware.

Released into the public domain under the [Unlicense](LICENSE), like the upstream project.
