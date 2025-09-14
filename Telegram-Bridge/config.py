#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
JS8Call ⇄ Telegram Bridge
Archivo de configuracion donde poner tus parametros y datos
"""

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


LEVEL                      = "INFO"                          # Logging level, it can be: WARNING, CRITICAL, INFO, DEBUG...
IGNORE_MESSAGES_FROM_SELF  = True                            # Para seguridad: evita bucles reenviando lo que tú mismo transmites
FORWARD_QSO_WINDOW         = True                            # activa el sondeo del QSO window
QSO_POLL_SECONDS           = 2.0                             # intervalo de sondeo
QSO_ID_CACHE_SIZE          = 2000                            # cuántos IDs recordamos para no duplicar

# ======= TELEGRAM

TG_CONNECT_TIMEOUT     = 20
TG_READ_TIMEOUT        = 60
TG_WRITE_TIMEOUT       = 60


# =================================================
