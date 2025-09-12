# -*- coding: utf-8 -*-
# Un único diccionario con todas las cadenas en castellano
STRINGS = {
    # Menús / ayuda
    "help": (
        "🤖 QXT Bridge – comandos:\n"
        "/help – Muestra este mensaje\n"
        "/status – Estado del puente\n"
        "/to CALLSIGN mensaje – Envía mensaje a indicativo\n"
        "/group @GRUPO mensaje – Envía mensaje a Grupo\n"
        "/last mensaje – Responde al último corresponsal\n"
        "/stations [N] – Últimas estaciones oídas\n"
        "/heartbeat | /hb – Envía Heartbeat a @HB\n"
        "/rescan – Fuerza refresco de estaciones oídas"
    ),

    # Estado
    "status_title": "🔎 Estado del QXT Bridge",
    "status": (
        "🔎 Estado del QXT Bridge\n"
        "JS8: {js8}\n"
        "Último corresponsal: {last}\n"
        "Último error JS8: {err}\n"
        "Grupos vigilados: {groups}"
    ),

    # Stations
    "stations_none": "Aún no he oído ninguna estación.",
    "stations_header": "📋 Oidas Recientemente (top {n}):",
    "stations_line": "{cs:<12} {snr_txt:<10} {grid:<6} hace {age}",

    # Heartbeat
    "hb_sent": "🔴 Heartbeat Enviado:\n @HB {text}",
    "hb_usage": "Uso: /heartbeat o /hb",

    # Comandos varios
    "to_usage": "Uso: /to CALLSIGN mensaje",
    "group_usage": "Uso: /group @GRUPO mensaje",
    "group_needs_at": "El grupo debe empezar por @, p.ej. @QXTNET",
    "last_usage": "Uso: /last mensaje (responde al último corresponsal recibido)",
    "last_none": "No hay corresponsal previo en memoria.",
    "sent_to": "Enviado a {who}: {text}",
    "msg_sent": "🔴 Mensaje Enviado:\n {who}: {text}",
    "err_sending": "Error enviando a {who}: {err}",
    "not_allowed": "Chat no autorizado.",

    # Pasarela
    "rx_generic": "📡 JS8 ⟶ Telegram\nDe: {frm}\nPara: {to}\n\n{txt}",
    "rx_qso_line": "🟢 Mensaje Recibido:\n{line}",
}
