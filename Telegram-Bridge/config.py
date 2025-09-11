#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
JS8Call ⇄ Telegram Bridge
Archivo de configuracion donde poner tus parametros y datos
"""

# ===================== CONFIG =====================

TELEGRAM_BOT_TOKEN = "111111111111111111111111111111111"     # Puedes obtenerlo el token de tu bot (desde @BotFather).
TELEGRAM_CHAT_ID   = 1065228100                              # Reemplaza por tu chat ID (int) el chat ID donde quieres recibir/enviar (p.ej., tu chat privado; puedes obtenerlo hablando >
MY_CALLSIGN        = "EA1ABC"                                # Tu indicativo en JS8
GRID               = "IMHO"                                  # Tu localizacion del Maidenhead Grid
MY_ALIASES         = [MY_CALLSIGN, "EA2ABC"]                 # Tu indicativo en JS8, si usas mas de uno
MONITORED_GROUPS   = ["@QXTNET"]                             # Ejemplo de grupos JS8 que quieres escuchar
JS8_HOST           = "127.0.0.1"                             # IP de la maquina donde esta corriendo JS8Call
JS8_PORT           = 2442                                    # Puerto JS8Call API JSON (normalmente 2442)
TRANSPORT          = "TCP"                                    # Protocolo del Puerto "TCP" (recomendado) o "UDP"




IGNORE_MESSAGES_FROM_SELF = True           # Para seguridad: evita bucles reenviando lo que tú mismo transmites
FORWARD_QSO_WINDOW = True                  # activa el sondeo del QSO window
QSO_POLL_SECONDS   = 2.0                   # intervalo de sondeo
QSO_ID_CACHE_SIZE = 2000                   # cuántos IDs recordamos para no duplicar


# =================================================
