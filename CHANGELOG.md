# Changelog

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

### Changed
- Daemon reads the thermostat log incrementally (only new lines) instead of re-parsing ~1 MB per request.
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
