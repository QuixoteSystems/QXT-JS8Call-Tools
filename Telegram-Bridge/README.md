## QXT Telegram Bridge

It connects to JS8Call’s TCP JSON API and a Telegram bot, forwards any received JS8Call messages addressed to your callsign or your monitored groups to a chosen Telegram chat, and lets you transmit back from Telegram using simple commands:

```telegram
/to CALLSIGN message — send to a station

/group @GROUP message — send to a JS8 group

/last message — reply to the last station

/stations - reply last stations heard
```

It composes the proper JS8 line and triggers transmit, normalizes callsigns/groups, ignores your own transmissions to prevent loops, auto-reconnects to JS8Call, and includes logging for troubleshooting.


If you like this work:

[![ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/M4M81CV1EX)
