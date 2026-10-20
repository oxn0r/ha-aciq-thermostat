# ACiQ Thermostat (Local) for Home Assistant

Local, cloud-free control and monitoring of the **ACiQ / Midea TL04-1** Wi-Fi thermostat
(sold as ACiQ and "AC Pro"), installed through HACS. Built for **communicating (HA/HB bus) dual-fuel
systems**: Midea air handler or A-coil, heat pump and gas furnace.

> **Credit:** this project exists because of [jeffmcfadden/midea-thermostat](https://github.com/jeffmcfadden/midea-thermostat).
> It discovered the TL04-1's open root ADB access, reverse-engineered the `dev_app` SysV IPC/msmart
> message format, and wrote the original `thermostat_api` daemon this integration's daemon is derived from.

## How it works
The TL04-1 runs Tina Linux and exposes an unauthenticated root ADB shell on TCP 5555.
On setup the integration:

1. Connects over ADB (pure Python, no adb binary needed on Home Assistant OS) and runs
   **read-only pre-flight checks**: root shell, firmware build, `dev_app` binary hash, IPC queues, and communicating mode.
2. Copies a small bundled daemon (`thermostat_api`, 65 KB, built reproducibly from `daemon/`) to
   the thermostat's **RAM disk** (`/tmp`) and starts it, with an IP allowlist containing only Home Assistant
   (plus any extra IPs you add).
3. Polls the daemon's local HTTP API every 30 s. If the thermostat reboots, `/tmp` is cleared, so the integration
   **re-deploys automatically**. Nothing persistent on the thermostat is modified.

The daemon only does two things:
- **Control:** injects the same msmart frames the ACiQ app sends through the cloud into `dev_app`'s queue.
  The mode and fan frames are byte-for-byte captures from the app.
- **Read:** parses the thermostat's log and reads `dev_app`'s equipment state from memory, read-only.

### Safety
- **Firmware guard:** control is enabled only on the verified build (`tina.tangly.20260314.152301`, `dev_app`
  sha256 `63ff55d0…`). On any other build, readings keep working, control returns an error, and a Repairs issue appears.
- **No half-applied commands:** requests are validated in full before anything is sent.
- **Fan lock:** fan speed only changes in Heat or Cool, matching the thermostat's own UI lock.
- **Never** modifies, restarts or replaces `dev_app`, `msmart`, `meiju`, or the firmware.

## Entities
| Entity | Notes |
|---|---|
| Climate | Off / Auto / Heat / Cool / Fan only; preset **Emergency heat**; fan Auto/Low/Medium/High (Heat & Cool only); setpoint 60–86 °F |
| Temperature, Humidity | thermostat's compensated sensor |
| **Outdoor temperature** | outdoor unit T4 sensor |
| Indoor coil / outdoor coil temperature | T2A / T3 |
| **Compressor frequency** (Hz), **Compressor** (running) | heat pump |
| **Furnace** (burning) | verified against the burners firing |
| Blower (running), Blower stage | indoor fan (stage 1 = low, confirmed as low airflow at the vents) |
| Fault, Fault codes | equipment fault and protection state |
| Setpoint, Mode, Fan setting, Power, Aux heat allowed, Emergency heat | |
| Diagnostics | firmware, indoor unit firmware, Wi-Fi signal, local control enabled, T1 |
| Buttons | Redeploy daemon, Run pre-flight checks |

`hvac_action` is derived from the furnace, compressor and blower states (heating / cooling / fan / idle / off).

## Install
1. HACS → Integrations → ⋮ → Custom repositories → add this repo (category *Integration*) → install.
2. Restart Home Assistant.
3. Settings → Devices & services → Add integration → **ACiQ Thermostat (Local)** → enter the thermostat's IP.
4. Review the pre-flight results and submit.

Options: auto-deploy on/off, and extra IPs allowed to use the API (for example, a workstation).

## Network / security
ADB on 5555 is a **root shell with no password** to anyone who can reach it. Recommended:
- DHCP reservations for the thermostat and Home Assistant.
- Block the thermostat's internet access. This also prevents firmware updates that could close ADB. Optionally allow NTP (UDP 123).
- Allow only Home Assistant → thermostat TCP 5555 and 8080. Check from another device that `nc -z <ip> 5555` **fails**.
  Same-subnet traffic often bypasses router firewalls.
- Only one ADB host at a time: this thermostat's adbd can stall a second client.

## Development
```sh
daemon/build.sh            # zig 0.17.0: build daemon + update bundled binary/sha
daemon/build.sh --check    # CI: bundled binary must match the source build
pytest -q --ignore=tests/live
ACIQ_LIVE_HOST=<ip> pytest tests/live -s   # end-to-end against a real thermostat
```
Protocol notes, struct offsets and test logs live in the parent research project.

## Credits
- [jeffmcfadden/midea-thermostat](https://github.com/jeffmcfadden/midea-thermostat): ADB access, IPC protocol,
  temperature frame, original daemon and HA component (Unlicense).
- This project added communicating-system (HBS/AHU3) support, mode/fan/off frames captured from the ACiQ app,
  the equipment data (outdoor temperature, coils, compressor, furnace, blower, faults), the safety guards, and the
  self-deploying HACS integration.

Public domain (Unlicense), like the upstream project.
