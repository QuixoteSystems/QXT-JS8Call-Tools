# QXT Telegram Bridge

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

## Configuration
To adapt to your enviroment and your own machine, edit config.py file and change it with your own data (Language, Callsign, IP, Port...).

### Getting your Telegram Bot Token & Your User ID

1- Open Telegram and chat with @BotFather.

2- Send /newbot, follow the prompts (choose a name and a unique username).

3- BotFather will reply with a bot token like 123456789:AA....

4- Keep it secret—don’t commit it to Git or share with anybody.

### Get your own Telegram User ID

5- Send a message to @userinfobot (or @getidsbot) and it will reply with your numeric ID.

6- Allow the bot to message you

7- Open a chat with your bot and press Start at least once, also you can use this [image](https://github.com/QuixoteSystems/QXT-JS8Call-Tools/blob/main/QXT-JS8Call-Tools-small.png) to your bot.

### Customize config.py

8- Change this fields with your own data:

```python
# ===================== CONFIG =====================

TELEGRAM_BOT_TOKEN = "111111111111111111111111111111111"     # You can get using @BotFather in Telegram.
TELEGRAM_CHAT_ID   = 1065228100                              # Change for your chat ID (int) that is your user chat ID on TG where you want receive/send messages (i.e. private chat, group, channel...)
MY_CALLSIGN        = ["EA1ABC"]                              # Your Callsign in JS8Call
GRID               = "IMHO"                                  # Your Maidenhead Grid location
MY_ALIASES         = [MY_CALLSIGN, "EA2ABC"]                 # Tu indicativo en JS8, si usas mas de uno
MONITORED_GROUPS   = ["@HB","@QXTNET"]                       # Example to receive all messages of Hearbeat and QXTNET Group
JS8_HOST           = "127.0.0.1"                             # IP where is running JS8Call, leave 127.0.0.1 if you are running this script in the same machine
JS8_PORT           = 2442                                    # Port JS8Call API JSON (normalmente 2442)
TRANSPORT          = "TCP"                                   # Protocol "TCP" (recommended) or "UDP"
LANG               = "es"                                    # Write "en" for English strings
```

## Running the script

```shell

python3 QXT-Telegram-Bridge.py

```


If you like this work:

[![ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/M4M81CV1EX)
