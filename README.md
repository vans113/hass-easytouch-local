# EasyTouch Local

Native Home Assistant integration for a **Pentair EasyTouch** pool controller, talking RS-485 straight through a TCP bridge such as an **Elfin EW11**. It replaces nodejs-poolController (njsPC), the socat relay and the schedule-sync container: nothing runs outside Home Assistant, so a normal HA backup and restore brings the pool back with everything else.

Unofficial; not made or endorsed by Pentair.

Built and tested against an EasyTouch2 4P with a two-speed pump. Other EasyTouch models use the same protocol but haven't been tried.

## What you get

| Entity | What it does |
| --- | --- |
| `switch.pool_controller_<circuit>` | **One switch per circuit**, found automatically and named as on the panel (custom names included). Circuits set to *Not Used* are skipped. |
| `switch.pool_controller_pool_pump` | The pool pump. This one isn't a controller circuit: the integration builds it from the **pump circuits** you choose. Its `controls` attribute shows what it switches (e.g. `Pool + High Speed`). Turns the **pump circuits** you choose on together, in order, and off in reverse. On when all of them are on and, if a power sensor is set, the motor is actually drawing power. |
| `switch.pool_controller_super_chlorinate` | IntelliChlor super-chlorinate, run by the controller (it keeps the pump on). On starts it for the hours set in the options (default 8); off cancels. Confirmed by reading it back; `hours_left` attribute. |
| `select.pool_controller_schedule_runs_on` | **Home Assistant** (HA switches the pump) or **Controller** (HA writes the pump schedules into the controller and the controller runs the pump itself). Only acts when *Manage pump schedules* is on; see *Schedules* |
| `binary_sensor.pool_controller_controller_link` | Fresh status is arriving from the controller |
| `binary_sensor.pool_controller_schedule_sync` | Problem: the controller's schedules couldn't be made to match |
| `binary_sensor.pool_controller_panel_service_mode` | Someone has the panel in Service/Timeout |
| `binary_sensor.pool_controller_high_speed_active` | The pump's high-speed relay is on (status byte 12, bit 3; diagnostic) |
| `number.pool_controller_chlorinator_output` | IntelliChlor pool output %, as set on the panel. Changing it leaves a running super-chlorinate alone; confirmed by reading back. |
| `sensor.pool_controller_super_chlorinate_time_left` | Hours of super-chlorinate left (0 when not running) |
| `binary_sensor.pool_controller_freeze_protection`, `..._water_temperature_sensor` | Freeze mode; water sensor fault (open/unplugged) |
| `sensor.pool_controller_water_temperature`, `..._air_temperature` | Temperatures (water shows unknown while its sensor is faulted) |
| `sensor.pool_controller_salt_level`, `..._chlorinator_output`, `..._chlorinator_status` | IntelliChlor salt, output % and flags (low flow, low salt, …) |
| `sensor.pool_controller_panel_mode` | Auto / Service / Timeout |
| `sensor.pool_controller_controller_schedules` | What's actually stored on the controller, read back from it (diagnostic) |
| `sensor.pool_controller_controller_clock`, `..._clock_drift` | The controller's clock and how far it is from HA (diagnostic) |
| `button.pool_controller_sync_controller_clock`, `..._sync_controller_schedules` | Do it now |

## Pump circuits

Most pools run the pump from the **Pool** circuit (6), and that's the default. Some need more than one circuit on at once. On a two-speed pump wired only to the **High Speed** relay, for example, the EasyTouch holds high speed for its 5-minute start timer after Pool turns on and then drops it, expecting the motor to carry on at low speed. With nothing on low speed the motor stops. Setting the pump circuits to `6, 2` (Pool, then High Speed) keeps it running. Whether high speed is on shows up in the status broadcast (byte 12, bit 3), which njsPC doesn't decode; this integration exposes it as `high_speed_active`.

In Controller schedule mode every pump circuit gets its own schedule entry at the same times. The pump circuits are also the only circuits whose schedules the integration will ever touch.

## Connecting to the bridge

Two ways, picked when you add the integration:

- **Home Assistant connects to the bridge**: the bridge (EW11) is a TCP *server*; enter its IP and port.
- **The bridge connects to Home Assistant**: the bridge is a TCP *client*. Point its socket at Home Assistant's IP and the port you choose here (8899 by default), and enter the **bridge's IP address**: connections from any other address are refused and logged, and never disturb the bridge's own connection (give the bridge a DHCP reservation). If the bridge itself reconnects, its newest connection wins, and when the link goes quiet Home Assistant closes the socket so the bridge dials back in by itself.

## Reliability

- **Every reconnect gets a full re-read.** Whenever the link comes back, even after a blip too short to make anything unavailable, the integration re-reads the circuit setup, re-checks the schedules and checks the clock: everything a reload would do, without tearing entities down.
- **Dead links are detected and fixed automatically.** The EasyTouch broadcasts its status about every 2 seconds, so a healthy link is never quiet. If no valid frame arrives for 10 seconds the connection is treated as dead, even if the socket still looks open, and is reopened with back-off (1, 2, 5, 10, 30 s). No reload is ever needed. After 30 seconds without status, entities go **unavailable** instead of showing stale values.
- **Every command is confirmed against the controller.** Circuit changes wait for the next status broadcasts to show them, schedule writes are read back from the controller slot by slot, and clock writes are checked in the broadcasts. Anything that doesn't show up is resent (3 tries), then the call fails with an error instead of failing silently.
- **One command at a time**, at least a second apart, sent into a quiet moment on the bus. The EasyTouch silently drops writes that arrive too close together.
- **Restarts never move anything.** Starting HA or the integration only listens; it doesn't switch circuits.
- **Setup doesn't fail when the bridge is down.** Entities stay unavailable and come back on their own.
- **Panel in Service/Timeout:** commands, schedule writes and clock sets are refused until it's back in Auto.

## Schedules

**Nothing on the controller's schedules is changed unless you turn on *Manage pump schedules on the controller* in the options.** It's off by default, including when you turn off *Listen only*. With it off the integration only reads the schedules (for the diagnostic sensor) and the sync button refuses.

With it on, the integration manages schedules **for the pump circuits only**. Schedules for any other circuit (lights, heater, AUX) and egg timers are never cleared, moved or overwritten, and only empty slots or the pump's own slots are used. The schedule mode select then decides who runs the pump:

- **Home Assistant**: your own automation switches `switch.pool_controller_pool_pump`. The integration keeps pump schedules off the controller (one added at the panel for a pump circuit is removed), so the two never fight.
- **Controller** (needs all six time helpers set): the integration builds the schedules from your time helpers and writes them into the controller: Pool + High speed, Mon–Fri and Sat/Sun windows in the summer months, one daily window otherwise. It reads every slot back to prove they stuck, re-checks every 30 minutes, after reconnecting and whenever a helper changes, and raises a repair issue if they still won't stick after 30 minutes of retries. A sync cut short by a link dropout isn't counted as a failure: it shows as *waiting_for_link* and runs again when the link is back. The controller then runs the pump on its own clock, so the pool keeps its schedule even if Wi-Fi, HA or the server is down.

The time helpers (`input_datetime` or `time` entities) and the optional schedule on/off helper are set in the options; none are set by default. Choosing Controller without schedule management on, or without all six time helpers, is refused with a message saying what's missing.

## Clock

The controller's clock is set from Home Assistant every night at 03:00 (which also lands the DST change), and whenever it drifts 2 minutes or more (at most every 30 minutes). Clock writes go out right after a minute rolls over so the seconds line up.

## Installation

1. HACS → ⋮ → **Custom repositories** → add this repository as an *Integration*, then install **EasyTouch Local** and restart Home Assistant. (Once it is in the HACS default store, just search for **EasyTouch Local**.)
2. Settings → Devices & services → **Add integration** → *EasyTouch Local*, then pick how the bridge connects (see above).
3. Leave **Listen only** on for the first install. In this mode nothing is ever sent to the controller.
4. In the options, set **pump circuits** if your pump needs more than Pool (the options page lists the circuits it found).

## Options

| Option | Default | Meaning |
| --- | --- | --- |
| Listen only | on | Never send anything to the controller |
| Manage pump schedules on the controller | off | Opt-in. Off: schedules on the controller are never written or cleared. On: only the pump circuits' schedules are managed |
| Who runs the pump schedule | Home Assistant | See *Schedules* |
| Pump circuits | 6 | Circuits the pool pump switch turns on together, in order (`6, 2` for Pool + High Speed) |
| Rename circuits | — | Optional `2:Pump High Speed, 3:Lights` overrides for the panel's names |
| Pump power sensor | — | Optional; vetoes "on" when the relay is on but the motor draws nothing |
| Running if power is at least | 300 W | Threshold for the above |
| Summer months | 4-10 | Months that use the summer weekday/weekend windows |
| Time helpers, schedule on/off helper | — | The six time helpers are required for Controller mode; the on/off helper is optional |
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
