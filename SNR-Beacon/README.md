# QXT SNR Beacon

Python Script to send a periodic SNR? to a group (with an immediate first TX), simple relays, and lightweight logging of activity.

1- Download the whole Repo or just the script QXT-SNR-Beacon

2- Simple run with examples parameters:
```python
python3 QXT-SNR-Beacon.py --group QXTNET --minutes 30 --transport TCP --host 127.0.0.1 --port 2442
```

3- Run as a service for Linux:

3.1- Create service file:

```shell
  sudo nano /etc/systemd/system/qxt-snr-beacon.service
```

3.2- Copy & Paste. You have to write your right path or if it is your home foler change YOUR_USER for your uer name:
```shell
[Unit]
Description=QXT JS8Call Tools - SNR Group Beacon
After=network-online.target
Wants=network-online.target
# If you have JS8Call as service in the same machine add: After=js8call.service

[Service]
Type=simple
WorkingDirectory=/home/YOUR_USER/QXT-JS8Call-Tools
ExecStart=/usr/bin/python3 QXT-SNR-Beacon.py --group QXTNET --minutes 30 --transport TCP --host 127.0.0.1 --port 2442
Restart=always
RestartSec=5
# (Optional hardenin)
NoNewPrivileges=yes

[Install]
WantedBy=default.target
```

4- Reload Systemd config files:

```shell
  sudo systemctl daemon-reload
```

5- Start and check if all is working well:

```shell
  sudo systemctl start qxt-snr-beacon.service
  sudo systemctl status qxt-snr-beacon.service
```

6- If all is right you have to see something like this:

```shell
● js8-snr.service - QXT JS8Call Tools - SNR Group Beacon
     Loaded: loaded (/etc/systemd/system/js8-snr.service; enabled; preset: enabled)
     Active: active (running) since Sun 2025-08-31 16:53:30 CEST; 53min ago
   Main PID: 502101 (python3)
      Tasks: 1 (limit: 8673)
     Memory: 6.5M (peak: 6.7M)
        CPU: 155ms
     CGroup: /system.slice/js8-snr.service
             └─502101 /usr/bin/python3 QXT-SNR-Beacon.py --group QXTNET --minutes 30 --transport TCP --host 192.168.1.14 --port 2442

Aug 31 16:53:30 quixote-bbs systemd[1]: Started js8-snr.service - QXT JS8Call Tools - SNR Group Beacon.
Aug 31 16:53:30 quixote-bbs python3[502101]: 2025-08-31 16:53:30,386 INFO Starting QXT SNR Beacon...
Aug 31 16:53:30 quixote-bbs python3[502101]: 2025-08-31 16:53:30,386 INFO Sending '@QXTNET SNR?' now and each 30 min via TCP - 192.168.1.14:2442
Aug 31 16:53:30 quixote-bbs python3[502101]: 2025-08-31 16:53:30,389 INFO TX -> @QXTNET SNR?

```

