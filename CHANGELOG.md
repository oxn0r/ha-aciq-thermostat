# Changelog

## Unreleased: 24 V groundwork (branch `24v-groundwork`)
### Added
- **Conventional 24 V thermostats are detected** ([#5](https://github.com/oxn0r/ha-aciq-thermostat/issues/5)).
  Instead of retrying setup with "has not seen the thermostat's state yet", a Repairs issue explains that 24 V mode
  isn't supported yet and shows what the thermostat reports; the raw 24 V state is logged once (and again when it
  changes) for that issue. Pre-flight names 24 V mode too.
- Daemon: `comm_mode` (communicating / 24v / unknown) and a read-only `v24` block parsed from `dev_24v_decorder`
  lines: unit, room temperature, setpoint, Auto low/high, and the raw `op_mode`, `heat_mode`, terminal states and
  terminal I/O, kept unmapped until captures show what they mean. Changes wake long-polls (room temperature excluded).
  Nothing is controlled in 24 V mode.

## 1.1.0b1 — 2026-10-26 (pre-release)
### Added
- **Auto with a heat/cool range** ([#7](https://github.com/oxn0r/ha-aciq-thermostat/issues/7)). Thermostats whose Auto
  holds separate heat and cool setpoints (dev_app `temp_zone_control: 1`) now get `target_temp_low/high` in Auto, in
  Home Assistant and HomeKit. Before, the card showed one setpoint that snapped back, because the thermostat ignores the
  single setpoint in that mode. Thermostats whose Auto uses one setpoint are unchanged. The integration follows the
  thermostat's setting automatically; Heat and Cool keep one setpoint.
- Range writes use the same command as the app (keys `0x54` cool / `0x55` heat, captured from a user's log and
  verified on a TL04-1). The daemon refuses ranges narrower than the thermostat's deadband, and the integration
  confirms the thermostat reports the new range within a few seconds, with an error showing the values it kept if not.
- Diagnostic sensors: **Auto setpoint type** (one setpoint / heat-cool range), **Auto heat setpoint**, **Auto cool
  setpoint** and **Deadband**.
- Diagnostics download: the last 10 commands with the daemon's reply and range readback, and an `auto_range` summary.
- Logging: each command and the daemon's reply at debug level; rejected commands and unapplied ranges as warnings; a
  log line when the thermostat's Auto setting changes (reload HomeKit Bridge then).
- Daemon: reports `zone_control`, `deadband` and `temp_zone_mode`; Auto range changes wake long-polls.

## 1.0.7 — 2026-10-24
### Fixed
- **Celsius thermostats** ([#4](https://github.com/oxn0r/ha-aciq-thermostat/issues/4)). With the thermostat set to °C
  (on the wall unit or in NetHome Plus) its setpoint is reported in °C, but the integration always assumed °F, so 20 °C
  showed as −7 °C. The daemon now reports the thermostat's unit (`temp_unit`) and the climate entity, its 16–30 °C /
  60–86 °F range and the *Setpoint* sensor follow it; room temperature is converted as needed. Switching the unit on the
  thermostat is picked up within ~1–2 s.
- Setting a temperature on a °C thermostat could switch it back to °F: the setpoint command always carried a unit byte,
  and the firmware changes the display unit when that byte differs. In °C mode the daemon now sends the unit-free
  setpoint command (verified on a TL04-1: applied in °C, unit untouched). Requests may include `"unit": "C"|"F"` and are
  converted to the thermostat's unit.
- After a thermostat reboot in °C mode, the cold-start state read from memory rejected °C setpoints.

## 1.0.6 — 2026-10-20
### Changed
- **Cloud-traffic block self-heals its contents, not just its presence.** The 30-minute check (also after reboots and
  redeploys) now compares the live rule set with the expected one and rebuilds it on any drift: missing, extra or
  reordered lines in `ACIQ_EGRESS` (e.g. a chain built by an older version), a duplicated jump, or a jump that is no
  longer first in `OUTPUT` (where an earlier rule could bypass it). Only our chain and our single jump are managed;
  other rules are never touched. Drift is logged ("had drifted (chain differs; missing 1); rebuilt") and shown as the
  switch's `last_repair` attribute. Verified on a TL04-1: a v1.0.4-built chain was detected and repaired, then
  reported ok.
- Turning the block off removes every jump into the chain (heals duplicates).

## 1.0.5 — 2026-10-20
### Added
- **Thermostat clock kept in sync with Home Assistant** (option, on by default). With its internet blocked the
  thermostat can't use NTP (its DNS for the NTP pool is blocked too), so its clock drifts, and after a reboot it
  starts from a hardware clock that was found ~10 h off. Home Assistant measures the offset over ADB (RTT-compensated)
  at setup, right after any reboot/redeploy and every 6 h, and corrects it with `date -s` when it's ≥ 2 s off.
  Runtime only; never touches the hardware clock or any file; refuses to act if HA's own clock looks unset.
  Verified on a TL04-1: −1.6 s → +0.2 s.
- **Clock offset** diagnostic sensor (attributes: measured offset, last check, last correction).
- *Block cloud traffic* now lets NTP (UDP 123) through, so a router-side NTP intercept (e.g. Firewalla) can answer if
  you allow DNS for the NTP pool.

## 1.0.4 — 2026-10-20
### Fixed
- **Toggling *Block cloud traffic* reloaded the integration** (≈3 s unavailable) and restarted the daemon. The switch
  now applies/removes the rule live over ADB and only persists the option; no reload, daemon untouched. If the
  thermostat can't be reached, the toggle shows an error and the state stays unchanged.
- **Healthy daemon restarted on every reload/restart (v1.0.3).** With long-polling, a waiting child process shares
  the daemon's name, so `pidof` returned two PIDs and the daemon looked "not running". The daemon is now identified
  as the process listening on the API port (matched by PID, robust to BusyBox's truncated names).
- Pre-flight's API-port check failed for the same reason while a long-poll was waiting.
- A custom API port was not passed to the daemon (`-p`); default 8080 unaffected.

### Added
- **Self-healing of extra processes:** stray `thermostat_api` processes that are neither the listening daemon nor its
  long-poll children are removed; if nothing listens, all are cleared and exactly one daemon is started and confirmed.
- **Plain-language deploy reasons** in the log, diagnostics and on the *Daemon started* sensor (attributes
  `last_deploy`, `last_deploy_reason`, `cause`, `thermostat_uptime_at_deploy_s`), e.g. "thermostat rebooted 1m35s ago
  (power loss or restart); daemon reinstalled", "daemon had stopped although the thermostat has been up 2h00m;
  restarted", "upgraded to the bundled daemon build", "restarted with a new allowlist".

### Changed
- Option changes reload only when needed (connection, auto-deploy, extra allowed IPs). *Block cloud traffic* changed
  from the Configure dialog is also applied live.

## 1.0.3 — 2026-10-20
### Added
- **Near-instant updates.** Changes made on the wall unit (setpoint, mode, fan) and equipment state changes reach
  Home Assistant in ~1–2 s instead of up to 30 s. The daemon supports long-polling
  (`GET /api/state?wait=<state_id>&timeout=25`); the integration keeps the 30 s poll as a watchdog and falls back to
  it automatically with an older daemon.
- **Outage diagnosis.** When the API stops answering, quick TCP checks classify it as *network down* (thermostat
  Wi-Fi drop / power loss — no ADB attempt), *daemon gone* (thermostat rebooted — redeploy at once) or *daemon hung*
  (forced restart). One log line per outage with the reason, and the duration on recovery. Recovery re-checks every
  ≤10 s instead of waiting for the next 30 s poll.
- Thermostat uptime recorded at every deploy (log + diagnostics), new diagnostic sensors **Last thermostat boot** and
  **Daemon started**, and an outage/deploy event history in diagnostics.
- Option **Block the thermostat's internet at the source** (off by default): a runtime firewall rule on the
  thermostat drops everything except LAN/Home Assistant traffic, so the ACiQ cloud client's endless DNS retries
  (~40/s with the internet blocked upstream) no longer reach your router. Re-applied after reboots; removed when off.

- **Block cloud traffic** switch on the device page (same as the option), with *rule active*, *dropped packets* and
  *last verified* attributes. Local traffic is never filtered (LAN, all private 10/172.16/192.168 ranges, link-local,
  100.64/10 VPN/Tailscale, Home Assistant, broadcast/multicast). The rule is verified after every reboot/redeploy and
  every 30 min, and re-applied if missing.

### Changed
- Daemon reads the thermostat log incrementally (only new lines, pre-filtered to the two processes it reads) and keeps
  dev_app's memory handle open. Measured on a TL04-1: a state read went from ~0.58 s to ~0.02 s; a waiting long-poll
  costs ~0.85 % of one core (≈ the old 30 s polling average), ~0.15 % with *Block cloud traffic* on.
- With *Block cloud traffic* on, the ACiQ cloud client's CPU use dropped from ~23 % to ~2 % of a core and its outbound
  DNS from ~40/s to 0 (measured), lowering overall thermostat load.
- *Auto state* diagnostic reports *None* outside Auto (the thermostat keeps its last Auto decision while in Heat/Cool;
  climate never used it outside Auto).
- Automatic redeploy back-off 5 min → 1 min; post-command refresh 2 s → 1 s.

### Fixed
- Outage log message had an empty reason (`Daemon unreachable ()`).

## 1.0.2 — 2026-10-20
### Changed
- "Aux heat allowed" moved to the device's diagnostic section (still enabled). It shows backup/furnace heat is
  *permitted* in the current mode (on in Heat), not that the furnace is burning; use the Furnace sensor for that.
### Fixed
- Furnace sensor showed "Normal"/"Hot" (Home Assistant's generic *heat* device class). It now reads **Off** / **Burning**.

## 1.0.1 — 2026-10-20
### Fixed
- **hvac_action showed "cooling" while heating in Auto.** v1.0.0 guessed Auto's direction from room vs setpoint,
  which flips whenever the room drifts across the setpoint inside Auto's deadband. The action now comes from the
  equipment: the thermostat's own Auto state (`auto_state_t`: 1 cool, 2 heat, 3 dry, 4 fan, decoded from the
  firmware and confirmed live), then the indoor coil vs room temperature (≥15 °F above → heating, ≥10 °F below → cooling),
  then the last confirmed direction. Never room vs setpoint. Reported with readings from 2026-10-20 01:14–01:17 UTC.

### Changed
- "Auto state" diagnostic sensor now shows None / Cooling / Heating / Drying / Fan instead of a raw number.

## 1.0.0 — 2026-10-19
First release.
- Config flow with read-only ADB pre-flight checks, **DHCP discovery**, reconfigure and options.
- Self-deploying on-device API daemon (RAM disk only; auto-redeploy after thermostat reboot; atomic swap; checksum-verified).
- Climate entity: Off / Auto / Heat / Cool / Fan only, Emergency heat preset, fan speeds (Heat/Cool), 60–86 °F.
- Equipment sensors: outdoor temperature, indoor/outdoor coil temperatures, compressor frequency and running state,
  **furnace burning**, blower state and stage, fault state and codes.
- Firmware guard with Repairs issue, translated errors, icon translations, diagnostics.
- Cold-start state from `dev_app` memory, so entities are available right after a thermostat reboot.
- Built on [jeffmcfadden/midea-thermostat](https://github.com/jeffmcfadden/midea-thermostat).
