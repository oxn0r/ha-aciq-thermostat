## New: ACiQ System Card

The integration now includes an animated card for your HVAC system: outdoor heat pump, refrigerant lines, indoor coil on the gas furnace, blower, ducts and the house, all driven by the thermostat's live data.

![ACiQ System Card: heat pump heating, cooling, gas furnace and idle](https://raw.githubusercontent.com/oxn0r/ha-aciq-thermostat/main/docs/images/system-card-all.gif)

![The four states as a still image](https://raw.githubusercontent.com/oxn0r/ha-aciq-thermostat/main/docs/images/system-card.png)

- The outdoor fan spins faster with compressor frequency. Refrigerant flows red when heating and blue when cooling. A flame shows when the furnace is on, and the blower turns with its stage.
- Chips for mode, blower stage, faults, and optionally power and filter use.
- **Nothing to install:** the integration loads the card in every dashboard. Add *ACiQ System Card* from the card picker; it finds your thermostat by itself, and has a visual editor.
- Works in light and dark themes.

**Using the standalone card already?** Remove its dashboard resource and `/config/www/aciq/aciq-system-card.js`. Your card YAML keeps working unchanged. See [Upgrading](https://github.com/oxn0r/ha-aciq-thermostat#upgrading-from-the-standalone-card).

The card shows equipment data, so it's most useful on firmware with equipment data (see Supported devices in the README).
