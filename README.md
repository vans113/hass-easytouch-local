# EasyTouch Local

Native Home Assistant integration for a **Pentair EasyTouch** pool controller, talking RS-485 straight through a TCP bridge such as an **Elfin EW11**. It replaces nodejs-poolController (njsPC), the socat relay and the schedule-sync container: nothing runs outside Home Assistant, so a normal HA backup and restore brings the pool back with everything else.

Unofficial; not made or endorsed by Pentair.

Built and tested against an EasyTouch2 4P with a two-speed pump. Other EasyTouch models use the same protocol but haven't been tried.

## What you get

| Entity | What it does |
| --- | --- |
| `switch.pool_controller_pool_pump` | The pool pump. Turns **Pool and High speed on together** (and off, high speed first). Shows **on** only while the high-speed relay is on and, if a power sensor is set, the motor is actually drawing power. |
| `switch.pool_controller_pool`, `switch.pool_controller_high_speed` | The raw circuits, plus any extra circuits you name in the options |
| `select.pool_controller_schedule_runs_on` | **Home Assistant** (HA switches the pump; the controller keeps no schedules) or **Controller** (HA writes the schedules into the controller and the controller runs the pump itself) |
| `binary_sensor.pool_controller_controller_link` | Fresh status is arriving from the controller |
| `binary_sensor.pool_controller_schedule_sync` | Problem: the controller's schedules couldn't be made to match |
| `binary_sensor.pool_controller_panel_service_mode` | Someone has the panel in Service/Timeout |
| `binary_sensor.pool_controller_pump_start_timer` | The controller's 5-minute high-speed start timer (diagnostic) |
| `binary_sensor.pool_controller_freeze_protection`, `..._water_temperature_sensor` | Freeze mode; water sensor fault (open/unplugged) |
| `sensor.pool_controller_water_temperature`, `..._air_temperature` | Temperatures (water shows unknown while its sensor is faulted) |
| `sensor.pool_controller_salt_level`, `..._chlorinator_output`, `..._chlorinator_status` | IntelliChlor salt, output % and flags (low flow, low salt, …) |
| `sensor.pool_controller_panel_mode` | Auto / Service / Timeout |
| `sensor.pool_controller_controller_schedules` | What's actually stored on the controller, read back from it (diagnostic) |
| `sensor.pool_controller_controller_clock`, `..._clock_drift` | The controller's clock and how far it is from HA (diagnostic) |
| `button.pool_controller_sync_controller_clock`, `..._sync_controller_schedules` | Do it now |

## Why the pump switch drives two circuits

On this pool the motor is wired to the **high-speed relay (AUX 1)**. When the Pool circuit turns on, the EasyTouch holds the high-speed relay for a 5-minute start timer and then drops it, expecting the motor to carry on at low speed. Nothing is wired to low speed, so the motor stops. Turning Pool **and** High speed on together keeps it running. The start timer shows up in the status broadcast (byte 12, bit 3), which njsPC doesn't decode; this integration exposes it as `pump_start_timer`.

## Reliability

- **Dead links are detected and fixed automatically.** The EasyTouch broadcasts its status about every 2 seconds, so a healthy link is never quiet. If no valid frame arrives for 10 seconds the connection is treated as dead, even if the socket still looks open, and is reopened with back-off (1, 2, 5, 10, 30 s). No reload is ever needed. After 30 seconds without status, entities go **unavailable** instead of showing stale values.
- **Every command is confirmed against the controller.** Circuit changes wait for the next status broadcasts to show them, schedule writes are read back from the controller slot by slot, and clock writes are checked in the broadcasts. Anything that doesn't show up is resent (3 tries), then the call fails with an error instead of failing silently.
- **One command at a time**, at least a second apart, sent into a quiet moment on the bus. The EasyTouch silently drops writes that arrive too close together.
- **Restarts never move anything.** Starting HA or the integration only listens; it doesn't switch circuits.
- **Setup doesn't fail when the bridge is down.** Entities stay unavailable and come back on their own.
- **Panel in Service/Timeout:** commands, schedule writes and clock sets are refused until it's back in Auto.

## Schedules

The schedule mode select decides who runs the pump:

- **Home Assistant**: your own automation switches `switch.pool_controller_pool_pump`. The integration keeps the controller's schedule slots empty (anything added at the panel is removed and reported). Egg timers are never touched.
- **Controller**: the integration builds the schedules from your time helpers and writes them into the controller: Pool + High speed, Mon–Fri and Sat/Sun windows in the summer months, one daily window otherwise. It reads every slot back to prove they stuck, re-checks every 30 minutes, after reconnecting and whenever a helper changes, and raises a repair issue if they won't stick. The controller then runs the pump on its own clock, so the pool keeps its schedule even if Wi-Fi, HA or the server is down.

The time helpers and the schedule on/off helper are set in the options (defaults: the `input_datetime.pool_*` and `input_boolean.pool_pump_schedule_enabled` helpers).

## Clock

The controller's clock is set from Home Assistant every night at 03:00 (which also lands the DST change), and whenever it drifts 2 minutes or more (at most every 30 minutes). Clock writes go out right after a minute rolls over so the seconds line up.

## Installation

1. HACS → ⋮ → **Custom repositories** → add this repository as an *Integration*, then install **EasyTouch Local** and restart Home Assistant.
2. Settings → Devices & services → **Add integration** → *EasyTouch Local*. Enter the bridge's IP and TCP port.
3. Leave **Listen only** on for the first install. In this mode nothing is ever sent to the controller, so it can run next to njsPC (the bridge must allow two TCP connections; see VERIFY.md).

## Options

| Option | Default | Meaning |
| --- | --- | --- |
| Listen only | on | Never send anything to the controller |
| Who runs the pump schedule | Home Assistant | See *Schedules* |
| Pool circuit / High-speed circuit | 6 / 2 | Circuit numbers on the EasyTouch |
| Extra circuit switches | — | `3:AUX 2, 4:Lights` style list |
| Pump power sensor | SPAN pool equipment sensor | Optional; vetoes "on" when the relay is on but the motor draws nothing |
| Running if power is at least | 300 W | Threshold for the above |
| Summer months | 4-10 | Months that use the summer weekday/weekend windows |
| Time helpers, schedule on/off helper | `input_datetime.pool_*`, `input_boolean.pool_pump_schedule_enabled` | Used in Controller mode |
| RS-485 address to send from | 33 | Leave alone unless another remote uses it |

## Cutover from njsPC

See [VERIFY.md](VERIFY.md) for the step-by-step test and cutover, including rollback.

## Development

```bash
pip install -r requirements_test.txt
ruff check . && ruff format --check .
pytest -q
```

The tests run against a fake EasyTouch (`tests/fake_controller.py`) that broadcasts real captured frames, keeps egg timers in schedule slots like a real panel, and can drop writes or go silent with the socket still open.
