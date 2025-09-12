# -*- coding: utf-8 -*-
STRINGS = {
    "help": (
        "🤖 QXT Bridge – commands:\n"
        "/help – Show this message\n"
        "/status – Bridge status\n"
        "/to CALLSIGN message – Send to a callsign (e.g., /to EA4ABC Hi)\n"
        "/group @GROUP message – Send to a group (e.g., /group @QXTNET Good morning)\n"
        "/last message – Reply to the last correspondent\n"
        "/stations [N] – Last heard stations (right panel)\n"
        "/heartbeat | /hb – Send Heartbeat to @HB\n"
        "/rescan – Force refresh of heard stations"
    ),
    "status_title": "🔎 QXT Bridge Status",
    "status": (
        "🔎 QXT Bridge Status\n"
        "JS8: {js8}\n"
        "Last correspondent: {last}\n"
        "Last JS8 error: {err}\n"
        "Watched groups: {groups}"
    ),
    "stations_none": "I haven't heard any station yet.",
    "stations_header": "📋 Recently heard (top {n}):",
    "stations_line": "{cs:<10} {snr_txt:<8} {grid:<6} {age} ago",
    "hb_sent": "🔴 Heartbeat sent:\n @HB {text}",
    "hb_usage": "Usage: /heartbeat or /hb",
    "to_usage": "Usage: /to CALLSIGN message",
    "group_usage": "Usage: /group @GROUP message",
    "group_needs_at": "Group must start with @, e.g. @QXTNET",
    "last_usage": "Usage: /last message (replies to last correspondent)",
    "last_none": "No previous correspondent in memory.",
    "sent_to": "Sent to {who}: {text}",
    "msg_sent": "🔴 Message sent:\n {who}: {text}",
    "err_sending": "Error sending to {who}: {err}",
    "not_allowed": "Not allowed in this chat.",
    "rx_generic": "📡 JS8 ⟶ Telegram\nFrom: {frm}\nTo: {to}\n\n{txt}",
    "rx_qso_line": "🟢 Message received:\n{line}",
}
