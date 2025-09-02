
## QXT Frecuency Scheduler

It is a small Python utility that automatically changes your JS8Call operating frequency depending on the time of day. It connects to the JS8Call API (default: 127.0.0.1:2442) and sends RIG.SET_FREQ commands to switch between a daytime frequency and a nighttime frequency.

This is useful if you want to automate band changes (for example: 40 m at night, 20 m during the day) so that JS8Call follows your schedule without manual intervention.

### Features

- Time-based control
Decides whether the current time falls into the daytime or nighttime window (--day-start / --day-end).

- Automatic frequency switching
Sends a RIG.SET_FREQ command to JS8Call to change the radio to either the day or night frequency.

- Flexible frequency input
Accepts values in Hz, kHz, or MHz (e.g. 7078000, 7078kHz, 7.078MHz).

### Two operating modes

- One-shot mode → run once, apply the right frequency, and exit.

- Watch mode → run continuously, check time every --interval seconds, and switch automatically at the thresholds.

Usage Examples:

```shell
# Switch to 14.078 MHz during the day (08:00–20:00) and 7.078 MHz at night
python js8call-scheduler.py \
  --day-start 08:00 --day-end 20:00 \
  --day-freq 14.078MHz --night-freq 7.078MHz

# Same, but stay running and auto-switch when the threshold is crossed
python js8call-scheduler.py \
  --day-start 08:00 --day-end 20:00 \
  --day-freq 14.078 --night-freq 7.078 \
  --watch --interval 60

# Custom host/port for the JS8Call API
python js8call-scheduler.py \
  --day-freq 14.078 --night-freq 7.078 \
  --host 127.0.0.1 --port 2442
```

✅ With this script running, your station will always be on the right band depending on the time of day, leaving you free to focus on QSOs, relays, or beaconing without manually changing bands.


If you like this work:

[![ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/M4M81CV1EX)
