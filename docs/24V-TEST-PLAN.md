# 24 V test plan (issue #5)

This build shows a conventionally wired (24 V) TL04-1 in Home Assistant: mode, setpoint or Auto range, room
temperature, what the thermostat is calling for, and its wiring. By default it sends **nothing** to the thermostat.
Control (mode, setpoint, Auto range) is an opt-in test (parts E and F).

## Install
1. Install **v1.1.0b5** (or newer): in HACS enable *Show beta versions* for this repository and update, or copy
   `custom_components/aciq_thermostat` from the release zip into `/config/custom_components/`.
2. Restart Home Assistant. The daemon on the thermostat updates itself.
3. You should see a Repairs note, *24 V thermostat: test build*. That's expected.
4. Optional, for more detail: in the integration's menu, choose **Enable debug logging**.

## What you'll see
| Entity | Shows |
|---|---|
| Thermostat (climate) | Mode (Off / Auto / Heat / Cool, Aux as Heat + *Emergency heat* preset), setpoint, Auto low/high, room temperature, action (heating / cooling / fan / idle) |
| 24 V outputs | Outputs the thermostat drives now, e.g. `O/B, G, Y2, Y1` (attribute: raw `terminal_config`) |
| Compressor call · Compressor stage 2 call · Aux heat call · Fan call · Reversing valve (O/B) | One per output |
| 24 V wiring | Wired terminals, plus the raw 24 V fields as attributes |
| Mode · Setpoint · Auto heat/cool setpoint · Auto setpoint type · Deadband · Temperature · Humidity | As on the screen |

Every change of mode, setpoints, outputs or 24 V state is logged at info level as
`24 V state on <ip> changed: ...`, and the last 60 changes are in the **diagnostics download** (`v24_events`).

## Tests
Write down the time of each step. Wait about 30 s between steps unless a step says otherwise.

**A. Readings (5 min)**
1. Compare the thermostat card with the screen in each mode: Off, Heat, Cool, Aux/Emergency heat, Auto.
2. In Auto, change the heat and cool setpoints on the screen and check the card's low/high follow.
3. Change a setpoint in Heat and in Cool on the screen and check the card follows.

**B. Real equipment runs (the important part)**
Hold each call until the equipment has clearly run for **at least 10 minutes**, and note when you hear or see it
start and stop:
1. **Heat-pump heat:** Heat, setpoint about 2° above the room. Note when the outdoor unit starts and if it stages up.
2. **Cooling:** Cool, setpoint about 2° below the room.
3. **Aux / boiler:** Aux/Emergency heat, setpoint above the room. Note when the boiler starts.
4. **Auto:** Auto with the heat setpoint above the room, then (after the run) the cool setpoint below it.
5. **Fan:** if you use a continuous-fan setting, turn it on and off once.

For each run, check that *Compressor call / Aux heat call / Fan call* turn on and off when the equipment does, and
that the card's action says heating / cooling / fan / idle at the right times.

**C. Edge cases (optional)**
- Power the thermostat off and on (or reboot it) and check the readings come back.
- Switch the thermostat to °C once and back.

**D. After the first runs (v1.1.0b3)**
1. Repeat **B2 (cooling)** once the equipment is checked or on a warm day. If an HVAC tech is out anyway, ask them
   whether the air handler gets the cool call (Y, G) from the thermostat while it's calling.
2. Do **part C**.
3. **App capture for control:** with the integration entry disabled, capture the log as before while changing, from
   the **phone app** (not the screen): the setpoint in Heat and in Cool, the Auto low and high, then each mode
   (Off, Heat, Cool, Auto, Fan, Aux/Emergency heat), about 30 s apart, noting the time of each.

**E. Setpoint control (v1.1.0b4, opt-in; passed 2026-10-28)**
1. In the integration's options (⚙ *Configure*), turn on **24 V thermostats: allow control (test)**.
2. In **Heat**, change the setpoint from Home Assistant by 1–2° up, then back. Check the thermostat screen follows
   within a few seconds.
3. Same in **Cool**.
4. In **Auto**, change the low, then the high, from Home Assistant.
5. Within a few minutes of the last step, download diagnostics. Each command is in `commands` with `readback`
   (`applied` / `not_applied`), and `recent_log` has the thermostat's own lines for it (`parse_tlv_24v_cmd`,
   `set24VTemp`, `Thigh` / `Tlow`). No ADB capture is needed for this.

**F. Mode control (v1.1.0b5, opt-in)**
Same option as part E. Fan speed can't be set on 24 V (single fan output). Make every change from **Home Assistant**,
about 30 s apart, and check the thermostat screen follows within a few seconds:
1. Heat → Cool → Auto → Fan only → Off.
2. Turn on from Off (it returns to the last mode).
3. Heat, then the *Emergency heat* preset on, then off again (back to Heat).
4. In Heat, change mode and setpoint together (e.g. the more-info card's mode and target), if your dashboard does that.
5. Download diagnostics within a few minutes. Each command has `readback` and `reported_mode`; `recent_log` has
   `MS_DEV_KEY_MODE tmpState`, `set24VMode` and `powerState` for each. Long equipment runs aren't needed for this.

## Report back on issue #5
1. The **diagnostics download** (Settings → Devices & services → ACiQ Thermostat → ⋮ → Download diagnostics), taken
   within a few minutes of the steps, **attached as a file** (it's too long to paste since it includes `recent_log`).
2. Your timestamps, and for each run when the equipment actually started and stopped.
3. Anything on the card that disagreed with the screen or the equipment.
4. The Home Assistant log lines containing `aciq_thermostat` (debug log if you enabled it).

## What this tells us before control can be added
| Question | Answered by |
|---|---|
| Does `terminal_config` stay on for the whole run, through compressor and stage delays? | B1–B4 timeline vs when the equipment ran |
| Y1 → Y2 → W1 staging timing (how *Compressor stage 2* and *Aux heat call* behave) | B1, B4 |
| O/B direction with this wiring (`OB_dir=2`: on in heat) | B1 vs B2 |
| What `op_mode`, `heat_mode`, `wind_fun` and the fan setting mean | B5, and whether they ever change |
| Does the thermostat really power a cool call (Y, G)? | D1 |
| The exact commands the app sends for setpoints and modes | D3 (done: same keys as on communicating units) |
| Do Home Assistant's setpoint commands land like the app's? | E (yes) |
| Do Home Assistant's mode commands land like the app's? | F |

Once F checks out, 24 V control goes into a regular release (opt-in until cooling has been seen running).
