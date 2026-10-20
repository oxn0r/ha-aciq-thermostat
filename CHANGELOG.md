# Changelog

## 1.0.0 — 2026-10-19
First release.
- Config flow with read-only ADB pre-flight checks, **DHCP discovery**, reconfigure and options.
- Self-deploying on-device API daemon (RAM disk only; auto-redeploy after thermostat reboot; atomic swap; checksum-verified).
- Climate entity: Off / Auto / Heat / Cool / Fan only, Emergency heat preset, fan speeds (Heat/Cool), 60–86 °F.
- Equipment sensors: outdoor temperature, indoor/outdoor coil temperatures, compressor frequency and running state,
  **furnace burning**, blower state and stage, fault state and codes.
- Firmware guard with Repairs issue, translated errors, icon translations, diagnostics.
- Built on [jeffmcfadden/midea-thermostat](https://github.com/jeffmcfadden/midea-thermostat).
