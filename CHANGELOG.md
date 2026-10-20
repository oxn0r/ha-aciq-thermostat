# Changelog

## 1.0.2 — 2026-10-20
### Changed
- "Aux heat allowed" moved to the device's diagnostic section (still enabled). It shows backup/furnace heat is
  *permitted* in the current mode (on in Heat), not that the furnace is burning; use the Furnace sensor for that.

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
