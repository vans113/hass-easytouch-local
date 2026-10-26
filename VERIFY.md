# Hardware check-out and cutover

Everything here is tested against a fake controller built from njsPC's EasyTouch protocol code and packet captures from the real EasyTouch2 4P. These items depend on the real hardware and need checking. Turn on debug logging first:

```yaml
logger:
  logs:
    custom_components.easytouch_local: debug
```

Every command sent shows up in the log. Download diagnostics from the device page too.

## 1. Listen-only

How you test depends on how the EW11A is set up today:

- **EW11A is a TCP client pointed at the Docker host (socat/pool-relay).** Easiest: add a *second* socket profile in the EW11A web page as a TCP client to Home Assistant's IP, port 8899, and pick **The bridge connects to Home Assistant** when adding the integration. njsPC keeps working on the first socket. If the EW11A won't take a second socket, skip to step 2 when you're ready to cut over and just repoint the one socket.
- **EW11A is a TCP server.** Set its connection limit (*Max Accept*) to 2 or more and pick **Home Assistant connects to the bridge**.

Firewall: the adapter is on the 192.168.5.x network, so allow it to reach Home Assistant on the chosen port (listen mode), or HA to reach the adapter (connect mode).

- [ ] **Add the integration** with Listen only on. It should see status within a minute.
- [ ] **Compare with njsPC for a day or two:** circuit states, air temp, salt level, chlorinator output/status, panel mode.
- [ ] **Circuits**: in listen-only it only learns names when njsPC asks the panel for them; until then circuits show as *Circuit N*. Full names come once control is on.
- [ ] **Pump start timer:** turn Pool on alone; `pump_start_timer` should be on for 5 minutes, then off as the motor stops.
- [ ] **Link drops:** watch `controller_link` through a Wi-Fi dropout. It should go off, then come back by itself with no reload. The *watchdog_trips* and *reconnects* attributes count them.
- [ ] **Water temperature** shows unknown and `water_temperature_sensor` reports a problem while the sensor reads 242–246.

## 2. Take over (when home)

1. Turn off HA's old automations that use the njsPC entities (*Pool pump schedule*, *Pool pump manual override*, the template switch's users) or repoint them to `switch.pool_controller_pool_pump`.
2. Stop the njsPC, pool-relay and pool-schedule-sync containers on .19. Don't delete them yet. If the EW11A only had one socket, repoint it to Home Assistant's IP and port now.
3. Integration options: turn **Listen only** off, set **Pump circuits** to `6, 2`. Leave *Who runs the pump schedule* on **Home Assistant** at first.
4. Check:
   - [ ] **Circuit switches** appear with the panel's names (High Speed, Pool, …) within a minute.
   - [ ] **Pool pump switch** on: Pool and High speed both on, motor running past 5 minutes; off: both off.
   - [ ] **Reconnect re-read:** after a dropout the log shows "link up: re-reading circuits, schedules and clock".
   - [ ] **Schedule sync** goes *in_sync* and the controller schedules sensor shows none (egg timers stay).
   - [ ] **Sync controller clock** button: controller clock matches HA.
   - [ ] **A dropped write:** the log shows "not confirmed; resending" at most occasionally, never a failure.
   - [ ] **Service mode at the panel:** pump switch refuses with a clear message; sync shows *paused*.
5. When ready, set *Who runs the pump schedule* to **Controller** and check:
   - [ ] Four schedules appear (Pool + High speed, Mon–Fri and Sat/Sun) in summer, two daily in winter.
   - [ ] They run the pump at the right times with HA's automation off.
   - [ ] Changing a time on the Pool view rewrites them within seconds.

## Rollback

Turn **Listen only** back on (or remove the integration), point the EW11A back at the Docker host if you moved it, start the three containers again, and re-enable the old automations. The containers' config is untouched.

## Nice to find out

- [ ] Does the controller ack commands (action 1)? Either way works; confirmation is by status/read-back.
- [ ] Does it accept address 33 when njsPC is off? If not, try 34 in the options.
- [ ] How often does the controller broadcast chlorinator status (action 25)?
