
## QXT Frecuency Scheduler



```shell

# Change right now the frequency with a schedule: 20 m in the day and y 40 m in the night
python js8call-scheduler.py \
  --day-start 08:00 --day-end 20:00 \
  --day-freq 14.078 --night-freq 7.078

# Keep watching and change when is the time:
python js8call-scheduler.py \
  --day-start 08:00 --day-end 20:00 \
  --day-freq 14.078MHz --night-freq 7078kHz \
  --watch --interval 60

# If your JS8Call is listenning in a different IP or Port:
python js8call-scheduler.py \
  --day-freq 14.078 --night-freq 7.078 \
  --host 127.0.0.1 --port 2442
```
