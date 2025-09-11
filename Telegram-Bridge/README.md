## QXT Telegram Bridge

It connects to JS8Call’s TCP JSON API and a Telegram bot, forwards any received JS8Call messages addressed to your callsign or your monitored groups to a chosen Telegram chat, and lets you transmit back from Telegram using simple commands:

Telegram Commands:
```telegram
  /to CALLSIGN mensaje          -> Envía "mensaje" a CALLSIGN
  /group @GRUPO mensaje         -> Envía "mensaje" al grupo (@GRUPO)
  /last mensaje                 -> Responde al último corresponsal recibido
  /status                       -> Estado del puente
  /heartbeat                    -> Send Heartbeat to the General Net
  /hb                           -> Send Heartbeat to the General Net
  /stations                     -> Reply last stations heared
```

It composes the proper JS8 line and triggers transmit, normalizes callsigns/groups, ignores your own transmissions to prevent loops, auto-reconnects to JS8Call, and includes logging for troubleshooting.

### Configuration
To run in your own machine, open config.py file and change and adapt with your data.

### Running

```shell

python3 QXT-Telegram-Bridge.py
```


If you like this work:

[![ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/M4M81CV1EX)
