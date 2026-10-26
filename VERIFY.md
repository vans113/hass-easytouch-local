# Hardware check-out and cutover

Everything here is tested against a fake controller built from njsPC's EasyTouch protocol code and packet captures from the real EasyTouch2 4P. These items depend on the real hardware and need checking. Turn on debug logging first:

```yaml
logger:
  logs:
    custom_components.easytouch_local: debug
```

Every command sent shows up in the log. Download diagnostics from the device page too.

## 1. Listen-only, next to njsPC (no risk)

- [ ] **EW11A allows a second connection.** In the EW11A web page, set the TCP server's *Max Accept* (connection limit) to 2 or more. Note the port njsPC's pool-relay uses; it's the port to enter here.
- [ ] **Add the integration** with Listen only on. It should find status within 10 s.
- [ ] **Compare with njsPC for a day or two:** Pool and High speed circuit states, air temp, salt level, chlorinator output/status, panel mode.
- [ ] **Pump start timer:** turn Pool on alone; `pump_start_timer` should be on for 5 minutes, then off as the motor stops.
- [ ] **Controller schedules sensor** fills in from what njsPC reads (listen-only can't ask for schedules itself).
- [ ] **Link drops:** watch `controller_link` through a Wi-Fi dropout. It should go off, then come back by itself with no reload. The *watchdog_trips* and *reconnects* attributes count them.
- [ ] **Water temperature** shows unknown and `water_temperature_sensor` reports a problem while the sensor reads 242–246.

## 2. Take over (when home)

1. Turn off HA's old automations that use the njsPC entities (*Pool pump schedule*, *Pool pump manual override*, the template switch's users) or repoint them to `switch.pool_controller_pool_pump`.
2. Stop the njsPC, pool-relay and pool-schedule-sync containers on .19. Don't delete them yet.
3. Integration options: turn **Listen only** off. Leave *Who runs the pump schedule* on **Home Assistant** at first.
4. Check:
   - [ ] **Pool pump switch** on: Pool and High speed both on, motor running past 5 minutes; off: both off.
   - [ ] **Schedule sync** goes *in_sync* and the controller schedules sensor shows none (egg timers stay).
   - [ ] **Sync controller clock** button: controller clock matches HA.
   - [ ] **A dropped write:** the log shows "not confirmed; resending" at most occasionally, never a failure.
   - [ ] **Service mode at the panel:** pump switch refuses with a clear message; sync shows *paused*.
5. When ready, set *Who runs the pump schedule* to **Controller** and check:
   - [ ] Four schedules appear (Pool + High speed, Mon–Fri and Sat/Sun) in summer, two daily in winter.
   - [ ] They run the pump at the right times with HA's automation off.
   - [ ] Changing a time on the Pool view rewrites them within seconds.

## Rollback

Turn **Listen only** back on (or remove the integration), start the three containers again, and re-enable the old automations. The containers' config is untouched.

## Nice to find out

- [ ] Does the controller ack commands (action 1)? Either way works; confirmation is by status/read-back.
- [ ] Does it accept address 33 when njsPC is off? If not, try 34 in the options.
- [ ] How often does the controller broadcast chlorinator status (action 25)?
