## QXT Telegram Bridge

It connects to JS8Call’s TCP JSON API and a Telegram bot, forwards any received JS8Call messages addressed to your callsign or your monitored groups to a chosen Telegram chat, and lets you transmit back from Telegram using simple commands:

Telegram Commands:
```telegram
  /help                         -> Show this menu
  /to CALLSIGN mensaje          -> Send "message" to CALLSIGN
  /group @GRUPO mensaje         -> Send "message" to Group (@GRUPO)
  /last mensaje                 -> Reply to the last station
  /status                       -> Bridge Status
  /heartbeat                    -> Send Heartbeat to the General Net
  /hb                           -> Send Heartbeat to the General Net
  /stations                     -> Reply last stations heared
```

It composes the proper JS8 line and triggers transmit, normalizes callsigns/groups, ignores your own transmissions to prevent loops, auto-reconnects to JS8Call, and includes logging for troubleshooting.

### Configuration
To adapt to your enviroment and your own machine, edit config.py file and change it with your own data (Language, Callsign, IP, Port...).

### Running

```shell

python3 QXT-Telegram-Bridge.py

```


If you like this work:

[![ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/M4M81CV1EX)
