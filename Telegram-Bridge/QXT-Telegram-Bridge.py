#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
JS8Call ⇄ Telegram Bridge
- Reenvía a Telegram los mensajes JS8 dirigidos a tu indicativo o a tus grupos.
- Permite contestar desde Telegram hacia estaciones o grupos en JS8Call.

Requiere: python-telegram-bot==21.6
Probado con Python 3.10+

Comandos en Telegram:
  /to CALLSIGN mensaje          -> Envía "mensaje" a CALLSIGN
  /group @GRUPO mensaje         -> Envía "mensaje" al grupo (@GRUPO)
  /last mensaje                 -> Responde al último corresponsal recibido
  /status                       -> Estado del puente
  /heartbeat                    -> Send Heartbeat to the General Net
  /hb                           -> Send Heartbeat to the General Net
  /stations                     -> Reply last stations heared
"""
import time
import asyncio
import json
import logging
import re
import config
import math

from i18n import t
from dataclasses import dataclass, field
from typing import Optional, Dict, List, Tuple
from collections import deque

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
from telegram.request import HTTPXRequest


# =================================================

import logging
import config

# =========== LOGGING ========================

def _resolve_log_level():
    lv = getattr(config, "LEVEL", "INFO")
    # si ya viene numérico
    if isinstance(lv, int):
        return lv
    # si viene como texto o número en string
    if isinstance(lv, str):
        s = lv.strip().upper()
        if s.isdigit():
            return int(s)
        # Acepta: CRITICAL, ERROR, WARNING, INFO, DEBUG, NOTSET
        level = getattr(logging, s, None)
        if isinstance(level, int):
            return level
    # fallback
    return logging.INFO

LOG_LEVEL = _resolve_log_level()

logger = logging.getLogger("js8_telegram_bridge")
logging.basicConfig(
    level=LOG_LEVEL,
    format="[%(asctime)s] %(levelname)s: %(message)s",
    force=True,  # reconfigura aunque basicConfig ya se hubiese llamado
)
logger.setLevel(LOG_LEVEL)  # opcional; útil si cambias niveles por logger

# ===============================================

# Silencia httpx/httpcore salvo que estés en DEBUG
for name in ("httpx", "httpcore"):
    logging.getLogger(name).setLevel(logging.DEBUG if LOG_LEVEL <= logging.DEBUG else logging.WARNING)


# Silenciar verbosidad de httpx/httpcore en INFO:
NOISY_LIBS = ("httpx", "httpcore")
if logging.getLogger().getEffectiveLevel() > logging.DEBUG:
    # Root está en INFO (o más alto) → oculta INFO de httpx
    for name in NOISY_LIBS:
        logging.getLogger(name).setLevel(logging.WARNING)                                                                                                   
else:
    # Root en DEBUG → muestra DEBUG de httpx cuando quieras inspeccionar
    for name in NOISY_LIBS:
        logging.getLogger(name).setLevel(logging.DEBUG)


# -----  JS8 API Utils (JSON line-based) -----

def _to_int_safe(x):
    try:
        return int(x)
    except Exception:
        try:
            return int(round(float(x)))
        except Exception:
            return None

def update_heard_from_params_calls_map(params: dict) -> int:
    """
    Soporta el formato:
    evt = {
      "type": "RX.CALL_ACTIVITY",
      "params": {
        "30AT120": {"GRID": " JN11", "SNR": -19, "UTC": 1757695538529},
        "2AE2331": {"GRID": "EM96",  "SNR": -17, "UTC": 1757695674291},
        ...
        "_ID": 258397311316
      },
      "value": ""
    }
    Devuelve cuántas entradas añadió/actualizó.
    """
    if not isinstance(params, dict):
        return 0

    count = 0
    for cs_key, info in params.items():
        if not isinstance(cs_key, str):
            continue
        if cs_key.startswith("_"):  # p.ej. "_ID"
            continue
        if not isinstance(info, dict):
            continue

        cs = cs_key.strip().upper()
        # Asegura al menos UNA letra (evita que "995" pase como indicativo)
        if not re.match(r'^(?=.*[A-Z])[A-Z0-9/]{3,}(?:-\d{1,2})?$', cs):
            continue

        grid = info.get("GRID") or info.get("grid") or info.get("LOC") or info.get("locator")
        if isinstance(grid, str):
            grid = grid.strip().upper() or None

        snr  = _to_int_safe(info.get("SNR"))
        freq = info.get("FREQ") or info.get("DIAL")
        off  = info.get("OFFSET")

        utc_ms = info.get("UTC")
        utc_ts = None
        if isinstance(utc_ms, (int, float)):
            utc_ts = (utc_ms / 1000.0) if utc_ms > 1e12 else float(utc_ms)

        base = _base_callsign(cs)
        now = time.time()
        prev = STATE.heard.get(base, {})
        STATE.heard[base] = {
            "callsign": base,
            "snr": snr if isinstance(snr, int) else prev.get("snr"),
            "grid": grid if isinstance(grid, str) else prev.get("grid"),
            "freq": freq if freq is not None else prev.get("freq"),
            "offset": off if off is not None else prev.get("offset"),
            "utc": utc_ts if utc_ts else prev.get("utc"),
            "ts": utc_ts if utc_ts else now,
            "text": prev.get("text"),  # aquí no viene TEXT; conservamos si ya había
        }
        count += 1

    return count


def _dump_json(path: str, obj) -> None:
    try:
        import json as _json
        with open(path, "w", encoding="utf-8") as f:
            if isinstance(obj, (dict, list)):
                _json.dump(obj, f, ensure_ascii=False, indent=2)
            else:
                f.write(str(obj))
        logger.debug(f"dump -> {path}")
    except Exception as e:
        logger.debug(f"dump error {path}: {e}")

def _safe_preview(val, maxlen: int = 500) -> str:
    try:
        if isinstance(val, (dict, list)):
            return json.dumps(val, ensure_ascii=False)[:maxlen]
        if isinstance(val, (bytes, bytearray)):
            return bytes(val).decode("utf-8", "ignore")[:maxlen]
        return str(val)[:maxlen]
    except Exception:
        return repr(val)[:maxlen]


def _safe_preview(val, maxlen: int = 800) -> str:
    try:
        if isinstance(val, (dict, list)):
            return json.dumps(val, ensure_ascii=False)[:maxlen]
        if isinstance(val, (bytes, bytearray)):
            return bytes(val).decode("utf-8", "ignore")[:maxlen]
        return str(val)[:maxlen]
    except Exception:
        return repr(val)[:maxlen]

def _dump_activity_debug(val) -> None:
    try:
        path = "/tmp/js8_call_activity_last.txt"
        with open(path, "w", encoding="utf-8") as f:
            if isinstance(val, (dict, list)):
                json.dump(val, f, ensure_ascii=False, indent=2)
            else:
                f.write(str(val))
        logger.debug(f"CALL/BAND activity dump -> {path}")
    except Exception as e:
        logger.debug(f"dump_activity_debug error: {e}")


_QSO_ID_RE = re.compile(r'[-–—]\s*\((\d+)\)\s*[-–—]')  # busca "- (1234) -" en la línea

def extract_qso_msg_id(line: str) -> str | None:
    """Devuelve el ID del QSO (string) si la línea contiene '- (n) -', si no None."""
    if not isinstance(line, str):
        return None
    m = _QSO_ID_RE.search(line)
    return m.group(1) if m else None

def was_id_forwarded(qso_id: str) -> bool:
    return qso_id in STATE.qso_forwarded_id_set

def remember_forwarded_id(qso_id: str):
    if not qso_id:
        return
    # purga si nos pasamos del límite
    if len(STATE.qso_forwarded_ids) >= config.QSO_ID_CACHE_SIZE:
        old = STATE.qso_forwarded_ids.popleft()
        STATE.qso_forwarded_id_set.discard(old)
    STATE.qso_forwarded_ids.append(qso_id)
    STATE.qso_forwarded_id_set.add(qso_id)


# Caché de TX propios recientes (no toca BridgeState)
_SENT_RECENT = deque(maxlen=200)
_SENT_TTL_SEC = 300  # 5 minutos


def _norm_to_token(s: str) -> str:
    s = (s or "").strip()
    if s.startswith("@"):
        # usa tu helper existente
        return _norm_group(s)
    return _base_callsign(s)


def _clean_msg(s: str) -> str:
    # quita símbolos finales típicos del QSO (diamantes, etc.) y normaliza espacios/mayúsculas
    t = (s or "").strip()
    t = re.sub(r"[♢◇♦♧♤♥]+$", "", t).strip()
    t = re.sub(r"\s+", " ", t)
    return t.upper()


def remember_sent(to: str, msg: str) -> None:
    _SENT_RECENT.append((_norm_to_token(to), _clean_msg(msg), time.time()))


def was_recently_sent(to: str, msg: str, ttl: int = _SENT_TTL_SEC) -> bool:
    now = time.time()
    # purga
    while _SENT_RECENT and now - _SENT_RECENT[0][2] > ttl:
        _SENT_RECENT.popleft()
    sig_to = _norm_to_token(to)
    sig_msg = _clean_msg(msg)
    return any((t == sig_to and m == sig_msg) for (t, m, ts) in _SENT_RECENT)


async def js8_send_now(callsign: str, text: str):
    """
    Envía directamente el texto por JS8Call sin depender de la caja TX.
    Varias versiones esperan 'value' con la línea completa.
    """
    payload = make_tx_message(callsign, text)
    logger.debug(payload)
    await BRIDGE.js8.send(payload)


def make_composed_text(to: str, text: str) -> str:
    """
    Formato que JS8Call entiende: destino + espacio + mensaje.
    Ej.: "@QXTNET Hola" o "EA4ABC BTU"
    """
    return f"{to} {text}".strip()


def make_tx_message(callsign: str, text: str) -> dict:
    """
    Construye el JSON de envío JS8: TX.SEND_MESSAGE
    """
    return {
        "params": {},
        "type": "TX.SEND_MESSAGE",
        "value": callsign +" "+ text
    }

def parse_js8_line(line: bytes):
    """
    Devuelve:
      - dict si es un objeto JSON válido
      - None si no es útil (string, null, lista, etc.)
    """
    try:
        obj = json.loads(line.decode("utf-8", errors="ignore"))
    except Exception:
        return None

    # JS8Call a veces puede soltar strings/valores simples: ignorarlos
    if isinstance(obj, dict):
        return obj
    else:
        return None
    

def extract_from_to_text(evt: dict) -> Optional[tuple[str, str, str]]:
    """
    Extrae (FROM, TO, TEXT) de cualquier evento.
    - Si el TEXT empieza con '@GRUPO' o 'CALLSIGN[:]' lo usamos como TO preferente.
    - Aunque value['TO'] venga relleno, si el inicio del TEXT nombra un destino, lo priorizamos.
    """
    if not isinstance(evt, dict):
        return None
    v = evt.get("value")
    if not isinstance(v, dict):
        return None

    frm = v.get("FROM") or v.get("from")
    to  = v.get("TO")   or v.get("to")
    txt = v.get("TEXT") or v.get("text")
    if not isinstance(txt, str):
        return None
    txt = txt.strip()

    # 1) Intenta sacar destino del propio texto SIEMPRE (prioritario)
    inferred_to, stripped = _parse_leading_destination(txt)

    # 2) Decide el mejor 'to' a usar:
    #    - Si el destino inferido del texto apunta a mi o a mis grupos, Lo usamos sino lo quitamos del texto
    #    - Si no, usa el TO del evento (si existe)
    chosen_to = None
    if inferred_to:
        # Normalizamos para comparar
        me_base = _base_callsign(MY_CALLSIGN)
        if inferred_to.startswith("@"):
            if any(_norm_group(inferred_to) == _norm_group(g) for g in config.MONITORED_GROUPS):
                chosen_to = inferred_to
                txt = stripped
        else:
            if _base_callsign(inferred_to) == me_base:
                chosen_to = inferred_to
                txt = stripped

    if not chosen_to and isinstance(to, str) and to.strip():
        chosen_to = to.strip()

    if isinstance(frm, str) and isinstance(chosen_to, str) and isinstance(txt, str):
        return frm.strip(), chosen_to.strip(), txt.strip()

    return None

CALLSIGN_RE = re.compile(r'^(?=.*[A-Z])[A-Z0-9/]{3,}(?:-\d{1,2})?$', re.I)

def _base_callsign(s: str) -> str:
    return (s or "").strip().split()[0].split("-")[0].upper()

def _norm_group(s: str) -> str:
    s = (s or "").strip()
    if s.startswith("@"):
        s = s.rstrip(":;,. ")
        return s.upper()
    return ""

def is_me(callsign: str) -> bool:
    base = _base_callsign(callsign)
    return any(_base_callsign(x) == base for x in config.MY_CALLSIGN if isinstance(x, str))

def to_is_me_or_monitored_group(to: str) -> bool:
    if not isinstance(to, str):
        return False
    to = to.strip()
    if is_me(to):
        return True
    g = _norm_group(to)
    if g and any(g == _norm_group(x) for x in config.MONITORED_GROUPS):
        return True
    return False


RAW_PATTERN = re.compile(
    r'^\s*([@A-Za-z0-9/+-]+)\s*[:>]\s*(@?[A-Za-z0-9/+-]{3,})\b\s*(.*)$'
)
# Matchea: "EA4ABC: EA4XXX-10 msg", "EA4ABC>EA4XXX msg", "EA4ABC> @GRUPO msg", etc.


def is_own_qso_line(line: str) -> bool:
    """
    True si la línea del QSO window es mía (cubre ':' o '>' con o sin espacio,
    y separadores raros). Vale para TX manual o enviado por el bot.
    """
    if not isinstance(line, str):
        return False
    s = line.strip()
    if not s:
        return False

    # 1) Intento “formal”: FROM[:|>]TO ...
    trip = parse_raw_line_to_triplet(s)
    if trip:
        frm, _to, _txt = trip
        if is_me(frm):
            return True

    # 2) Heuristica robusta: empieza por mi indicativo + separador
    up = s.upper().lstrip()
    for a in MY_CALLSIGN:
        base = _base_callsign(a)
        if up.startswith(base):
            # carácter siguiente aceptado como separador típico
            next_ch = up[len(base):len(base)+1]
            if next_ch in (":", ">", " ", "-", "—", "–", "\t"):
                return True
    return False


def parse_raw_line_to_triplet(text: str):
    """
    Si JS8Call no manda JSON y solo llega una línea de texto,
    intenta extraer (FROM, TO, TEXT).
    """
    if not isinstance(text, str):
        return None
    s = text.strip()
    if not s:
        return None
    m = RAW_PATTERN.match(s)
    if not m:
        return None
    frm, to, rest = m.groups()
    frm = frm.strip().rstrip(":>").upper()
    to  = to.strip().rstrip(":;,. ").upper()
    msg = (rest or "").strip()
    return frm, to, msg


def _parse_leading_destination(txt: str) -> tuple[str, str]:
    """
    Detecta destino en las primeras posiciones del texto.
    Soporta:
      1) "@GRUPO Mensaje"
      2) "EA4ABC Mensaje"
      3) "FROM: TO Mensaje"   ← caso como "3BC001: 30QXT01 SNR -10"
      4) "FROM TO Mensaje"    ← sin dos puntos
    Devuelve (destino, resto_sin_destino) o ("", txt) si no lo halla.
    """
    s = (txt or "").strip()
    if not s:
        return "", txt

    tokens = s.split()
    if not tokens:
       return "", txt

    first = tokens[0].rstrip(":;,.")
    # Caso 1: grupo al principio
    if first.startswith("@"):
        dest = first.upper()
        rest = " ".join(tokens[1:]).strip()
        return dest, rest

    # Caso 3: "FROM: TO ..."
    if tokens[0].endswith(":") and len(tokens) >= 2:
        cand = tokens[1].rstrip(":;,.")
        if cand.startswith("@") or CALLSIGN_RE.match(cand):
            dest = cand.upper()
            rest = " ".join(tokens[2:]).strip()
            return dest, rest

    # Caso 4: "FROM TO Mensaje" (sin dos puntos)
    if CALLSIGN_RE.match(first) and len(tokens) >= 2:
        cand = tokens[1].rstrip(":;,.")
        if cand.startswith("@") or CALLSIGN_RE.match(cand):
            dest = cand.upper()
            rest = " ".join(tokens[2:]).strip()
            return dest, rest

    # Caso 2: "CALLSIGN Mensaje"
    if CALLSIGN_RE.match(first):
        dest = first.upper()
        rest = " ".join(tokens[1:]).strip()
        return dest, rest

    return "", txt


def extract_from_to_text(evt: dict):
    """
    Extrae (FROM, TO, TEXT) de cualquier evento JS8Call que traiga value y TEXT.
    - Si el inicio del TEXT nombra un destino (@GRUPO o CALLSIGN), LO PRIORIZA.
    """
    if not isinstance(evt, dict):
        return None
    v = evt.get("value")
    if not isinstance(v, dict):
        return None

    frm = v.get("FROM") or v.get("from")
    to  = v.get("TO")   or v.get("to")
    txt = v.get("TEXT") or v.get("text")
    if not isinstance(txt, str):
        return None
    txt = txt.strip()

    # Prioriza el destino detectado en el propio texto
    inferred_to, stripped = _parse_leading_destination(txt)
    chosen_to = inferred_to or (to.strip() if isinstance(to, str) else "")

    if inferred_to:
        txt = stripped  # quitamos @GRUPO/CALLSIGN del cuerpo

    if isinstance(frm, str) and isinstance(chosen_to, str) and isinstance(txt, str):
        return frm.strip(), chosen_to.strip(), txt.strip()
    return None


def parse_rx_spot(evt: dict) -> Optional[dict]:
    """
    Extrae info de un RX.SPOT “estación oída”.
    Campos típicos: CALLSIGN, SNR, GRID, FREQ, OFFSET.
    """
    if not isinstance(evt, dict) or evt.get("type") != "RX.SPOT":
        return None
    v = evt.get("value") or {}
    cs = v.get("CALLSIGN") or v.get("STATION") or v.get("from") or v.get("CALL") or v.get("call")
    if not isinstance(cs, str):
        return None

    # SNR a int
    snr = v.get("SNR")
    try:
        snr = int(snr)
    except Exception:
        try:
            snr = int(round(float(snr)))
        except Exception:
            snr = None

    grid   = v.get("GRID")   or v.get("grid")   or v.get("LOC") or v.get("locator")
    freq   = v.get("FREQ")   or v.get("freq")   or v.get("DIAL") or v.get("dial")
    offset = v.get("OFFSET") or v.get("offset")

    return {
        "callsign": _base_callsign(cs),
        "snr": snr,
        "grid": grid if isinstance(grid, str) else None,
        "freq": freq,
        "offset": offset,
        "ts": time.time(),
    }





# ---- Helpers: Call Activity → heard -----------------


def maidenhead_to_latlon(grid: str):
    """Convierte un locator Maidenhead (2-10 chars) al centro de la celda (lat, lon)."""
    if not isinstance(grid, str):
        return None
    g = grid.strip()
    if len(g) < 2:
        return None
    g = g.upper()
    try:
        # Campo (2 letras)
        lon = (ord(g[0]) - ord('A')) * 20 - 180
        lat = (ord(g[1]) - ord('A')) * 10 - 90
        res_lon, res_lat = 20.0, 10.0

        if len(g) >= 4 and g[2].isdigit() and g[3].isdigit():
            # Cuadrado (2 dígitos)
            lon += int(g[2]) * 2
            lat += int(g[3]) * 1
            res_lon, res_lat = 2.0, 1.0

        if len(g) >= 6 and g[4].isalpha() and g[5].isalpha():
            # Subcuadrado (2 letras)
            lon += (ord(g[4]) - ord('A')) * (2.0 / 24.0)
            lat += (ord(g[5]) - ord('A')) * (1.0 / 24.0)
            res_lon, res_lat = 2.0 / 24.0, 1.0 / 24.0  # 5' lon, 2.5' lat

        if len(g) >= 8 and g[6].isdigit() and g[7].isdigit():
            # Extendido (2 dígitos)
            lon += int(g[6]) * (res_lon / 10.0)
            lat += int(g[7]) * (res_lat / 10.0)
            res_lon /= 10.0
            res_lat /= 10.0

        # centro de la celda
        lon += res_lon / 2.0
        lat += res_lat / 2.0
        return (lat, lon)
    except Exception:
        return None

def haversine_km(lat1, lon1, lat2, lon2):
    R = 6371.0088
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1))*math.cos(math.radians(lat2))*math.sin(dlon/2)**2
    c = 2 * math.asin(min(1, math.sqrt(a)))
    return R * c

def grid_distance_km(grid1: str, grid2: str):
    p1 = maidenhead_to_latlon(grid1) if grid1 else None
    p2 = maidenhead_to_latlon(grid2) if grid2 else None
    if not p1 or not p2:
        return None
    return round(haversine_km(p1[0], p1[1], p2[0], p2[1]))

def _extract_callsign_from_line(line: str) -> Optional[str]:
    if not isinstance(line, str):
        return None
    tokens = re.findall("[A-Za-z0-9/+-]+", line.upper())
    for tok in tokens:
        if CALLSIGN_RE.match(tok):
            return _base_callsign(tok)
    return None


def update_heard_from_call_activity(value):
    """
    Normaliza la 'pantalla derecha' a STATE.heard.
    Acepta:
      - list[dict] con claves típicas
      - dict con lista dentro (stations/list/items/activity/...)
      - dict mapeando CALLSIGN -> dict(info)
      - str multilinea o JSON en str
    """
    GRID_RE = re.compile(r'\b([A-R]{2}\d{2}(?:[A-X]{2})?(?:\d{2})?)\b', re.I)

    def _to_int(x):
        try:
            return int(x)
        except Exception:
            try:
                return int(round(float(x)))
            except Exception:
                return None

    def _push(cs, snr=None, grid=None, freq=None, offset=None):
        if not isinstance(cs, str):
            return
        base = _base_callsign(cs)
        if not base or not CALLSIGN_RE.match(base):
            return
        now = time.time()
        prev = STATE.heard.get(base, {})
        entry = {
            "callsign": base,
            "snr": snr if isinstance(snr, int) else prev.get("snr"),
            "grid": grid if isinstance(grid, str) else prev.get("grid"),
            "freq": freq if freq is not None else prev.get("freq"),
            "offset": offset if offset is not None else prev.get("offset"),
            "ts": now,
        }
        STATE.heard[base] = entry

    if value is None:
        return

    if isinstance(value, (bytes, bytearray)):
        try:
            value = bytes(value).decode("utf-8", "ignore")
        except Exception:
            value = str(value)

    # str → intenta JSON y luego texto
    if isinstance(value, str):
        s = value.strip()
        if s.startswith("{") or s.startswith("["):
            try:
                decoded = json.loads(s)
                return update_heard_from_call_activity(decoded)
            except Exception:
                pass
        for line in s.splitlines():
            line = line.strip()
            if not line:
                continue
            cs = next((tok for tok in line.split() if CALLSIGN_RE.match(tok)), None)
            if not cs:
                continue
            m_snr = re.search(r'\bSNR\s*([+-]?\d{1,2})\b', line, re.I)
            snr = _to_int(m_snr.group(1)) if m_snr else None
            m_grid = GRID_RE.search(line)
            grid = m_grid.group(1).upper() if m_grid else None
            _push(cs, snr, grid)
        return

    # list
    if isinstance(value, list):
                # 4.b) Mapa de offsets (claves numéricas en 'params'): {"930": {...}, "950": {...}}
        #     Cada item suele traer: DIAL/FREQ/OFFSET/SNR/TEXT/UTC
        keys = list(value.keys())
        is_offset_map = keys and all(isinstance(k, str) and (k.isdigit() or k.startswith("_")) for k in keys)
        if is_offset_map:
            for k, d in value.items():
                if not isinstance(d, dict):
                    continue
                text = (d.get("TEXT") or "").strip()
                # Indicativo: al inicio de TEXT antes de ':' o '>'
                m_cs = re.match(r'\s*([A-Z0-9/]{3,})\s*[:>]', text, re.I)
                if m_cs:
                    cs = m_cs.group(1)
                else:
                    # Fallback: primer token con pinta de indicativo
                    cs = None
                    for tok in text.split():
                        if CALLSIGN_RE.match(tok):
                            cs = tok
                            break
                snr  = _to_int(d.get("SNR"))
                m_g  = GRID_RE.search(text)
                grid = m_g.group(1).upper() if m_g else None
                freq = d.get("FREQ") or d.get("DIAL")
                off  = d.get("OFFSET")
                _push(cs, snr, grid, freq, off)
            return

    # dict
    if isinstance(value, dict):
        # a) nombres comunes de lista
        for key in ("stations","STATIONS","list","LIST","items","ITEMS","activity","ACTIVITY","values","VALUES"):
            lst = value.get(key)
            if isinstance(lst, list):
                update_heard_from_call_activity(lst)
                return
            if isinstance(lst, dict):
                # por si anidan otra lista dentro
                for _k, _v in lst.items():
                    if isinstance(_v, list):
                        update_heard_from_call_activity(_v)
                        return
                                # 4.b) Mapa de offsets (claves numéricas en 'params'): {"930": {...}, "950": {...}, "_ID": ...}
                keys = list(value.keys())
                is_offset_map = keys and all(isinstance(k, str) and (k.isdigit() or k.startswith("_")) for k in keys)
                if is_offset_map:
                    GRID_RE = re.compile(r'\b([A-R]{2}\d{2}(?:[A-X]{2})?(?:\d{2})?)\b', re.I)
        
                    def _to_int(x):
                        try:
                            return int(x)
                        except Exception:
                            try:
                                return int(round(float(x)))
                            except Exception:
                                return None
        
                    for k, d in value.items():
                        if not isinstance(d, dict):
                            continue
                        text = (d.get("TEXT") or "").strip()
        
                        # Indicativo en TEXT: "EA1ABC: ..." o "EA1ABC> ..."
                        m_cs = re.match(r'\s*([A-Z0-9/]{3,})\s*[:>]', text, re.I)
                        if m_cs and CALLSIGN_RE.match(m_cs.group(1)):
                            cs = m_cs.group(1).upper()
                        else:
                            cs = next((tok.upper() for tok in text.split() if CALLSIGN_RE.match(tok)), None)
        
                        snr  = _to_int(d.get("SNR"))
                        m_g  = GRID_RE.search(text)
                        grid = m_g.group(1).upper() if m_g else None
                        freq = d.get("FREQ") or d.get("DIAL")
                        off  = d.get("OFFSET")
                        utc_ms = d.get("UTC")
                        utc_ts = None
                        if isinstance(utc_ms, (int, float)):
                            utc_ts = (utc_ms / 1000.0) if utc_ms > 1e12 else float(utc_ms)
        
                        if cs:
                            base = _base_callsign(cs)
                            now = time.time()
                            prev = STATE.heard.get(base, {})
                            STATE.heard[base] = {
                                "callsign": base,
                                "snr": snr if isinstance(snr, int) else prev.get("snr"),
                                "grid": grid if isinstance(grid, str) else prev.get("grid"),
                                "freq": freq if freq is not None else prev.get("freq"),
                                "offset": off if off is not None else prev.get("offset"),
                                "utc": utc_ts if utc_ts else prev.get("utc"),
                                "ts": utc_ts if utc_ts else now,   # usa tiempo real si viene
                                "text": text or prev.get("text"),
                            }
                    return


        # b) mapa CALLSIGN -> dict(info)
        keys = list(value.keys())
        looks = [k for k in keys if isinstance(k, str) and CALLSIGN_RE.match(k)]
        if looks and len(looks) >= max(1, int(0.6 * len(keys))):
            for cs, info in value.items():
                if not isinstance(cs, str):
                    continue
                if isinstance(info, dict):
                    snr  = _to_int(info.get("SNR"))
                    grid = info.get("GRID") or info.get("grid") or info.get("LOC") or info.get("locator")
                    freq = info.get("FREQ") or info.get("freq") or info.get("DIAL") or info.get("dial")
                    off  = info.get("OFFSET") or info.get("offset")
                    _push(cs, snr, grid, freq, off)
                else:
                    _push(cs)
            return

        # c) un solo objeto estación
        cs = value.get("CALLSIGN") or value.get("STATION") or value.get("from") or value.get("CALL") or value.get("call")
        if cs:
            snr  = _to_int(value.get("SNR"))
            grid = value.get("GRID") or value.get("grid") or value.get("LOC") or value.get("locator")
            freq = value.get("FREQ") or value.get("freq") or value.get("DIAL") or value.get("dial")
            off  = value.get("OFFSET") or value.get("offset")
            _push(cs, snr, grid, freq, off)
            return

        # d) campos de texto
        for k in ("text","TEXT","raw","RAW","dump","DUMP","value","VALUE"):
            txt = value.get(k)
            if isinstance(txt, (str, list, dict)):
                update_heard_from_call_activity(txt)
                return
        return



# --------------- Estados compartidos ----------------

@dataclass
class BridgeState:
    last_from_per_chat: Dict[int, str] = field(default_factory=dict)  # chat_id -> last callsign
    js8_connected: bool = False
    js8_last_error: Optional[str] = None
    heard: Dict[str, dict] = field(default_factory=dict)   # NEW: callsign -> info
    qso_last_text: str = ""   # ← NUEVO: última copia del QSO window
    qso_forwarded_ids: deque = field(default_factory=deque)  # cola de IDs enviados
    qso_forwarded_id_set: set = field(default_factory=set)   # set para O(1) contains

STATE = BridgeState()

# ------------- Cliente JS8 (TCP/UDP) Async ----------

class JS8ClientTCP:
    def __init__(self, host: str, port: int, on_event):
        self.host = host
        self.port = port
        self.on_event = on_event
        self.reader: Optional[asyncio.StreamReader] = None
        self.writer: Optional[asyncio.StreamWriter] = None
        self.task: Optional[asyncio.Task] = None

    async def connect(self):
        logger.info(f"Conectando a JS8Call TCP {self.host}:{self.port} ...")
        self.reader, self.writer = await asyncio.open_connection(self.host, self.port)
        STATE.js8_connected = True
        STATE.js8_last_error = None
        logger.info("Conectado a JS8Call (TCP).")
        self.task = asyncio.create_task(self.read_loop())

    async def read_loop(self):
        try:
            while True:
                line = await self.reader.readline()
                if not line:
                    raise ConnectionError("Conexión cerrada por JS8Call.")
                evt = parse_js8_line(line)
                if evt:
                    await self.on_event(evt)
                    continue  # ← importante, ya procesado como JSON

                # Fallback: intenta parsear la línea como texto crudo
                text_line = line.decode("utf-8", errors="ignore").strip()
                triplet = parse_raw_line_to_triplet(text_line)
                if triplet:
                    frm, to, txt = triplet
                    logger.debug(f"RAW match ← JS8: FROM={frm} TO={to} TEXT={txt}")
                    # usa el manejador crudo
                    await on_raw_triplet(frm, to, txt)
                else:
                    logger.debug(f"Frame no-JSON/no-RAW: {text_line!r}")

        except Exception as e:
            STATE.js8_connected = False
            STATE.js8_last_error = str(e)
            logger.error(f"JS8 TCP desconectado: {e}")

    async def send(self, obj: dict):
        if not self.writer:
            raise ConnectionError("No conectado a JS8 (TCP).")
        data = (json.dumps(obj) + "\n").encode("utf-8")
        self.writer.write(data)
        await self.writer.drain()

class JS8ClientUDP:
    """
    Cliente simple UDP:
      - Recibe datagramas JSON desde JS8Call (si JS8Call los emite por UDP).
      - Envia comandos JSON a JS8Call al mismo host/puerto.
    Nota: segun configuracion de JS8Call, puede no enviar eventos por UDP.
    """
    def __init__(self, host: str, port: int, on_event):
        self.host = host
        self.port = port
        self.on_event = on_event
        self.transport = None

    async def connect(self):
        loop = asyncio.get_running_loop()
        logger.info(f"Abriendo socket UDP hacia {self.host}:{self.port} ...")
       # Creamos un endpoint UDP; para recibir, nos bindeariamos a ('0.0.0.0', port_local_distinto)
        # Aqui mantenemos solo envio; para recepcion habria que conocer como JS8Call emite eventos por UDP.
        # Implementamos un "escucha" opcional si fuese necesario.
        # Por compatibilidad basica, dejaremos solo envio y un receptor 'best-effort' en el mismo puerto (puede fallar si ya esta en uso).
        try:
            self.transport, _ = await loop.create_datagram_endpoint(
                lambda: _UDPProtocol(self.on_event),
                local_addr=("0.0.0.0", self.port),
            )
            STATE.js8_connected = True
            STATE.js8_last_error = None
            logger.info("Socket UDP listo (intento de escucha).")
        except Exception as e:
            # Si no podemos bindear (porque JS8Call ya usa ese puerto), abrimos socket sin bind local fijo para enviar.
            logger.warning(f"No se pudo bindear UDP {self.port} para escuchar ({e}). Se usará solo envío.")
            self.transport, _ = await loop.create_datagram_endpoint(
                lambda: _UDPProtocol(self.on_event),
                remote_addr=(self.host, self.port),
            )
            STATE.js8_connected = True
            STATE.js8_last_error = None

    async def send(self, obj: dict):
        if not self.transport:
            raise ConnectionError("No conectado a JS8 (UDP).")
        data = (json.dumps(obj) + "\n").encode("utf-8")
        # Enviamos al host/puerto objetivo
        self.transport.sendto(data, (self.host, self.port))

class _UDPProtocol(asyncio.DatagramProtocol):
    def __init__(self, on_event):
        self.on_event = on_event

    def datagram_received(self, data, addr):
        evt = parse_js8_line(data)
        if evt:
            # Ejecutamos on_event en el loop
          asyncio.create_task(self.on_event(evt))

async def on_raw_triplet(frm: str, to: str, txt: str):
    # Evita eco propio
    if config.IGNORE_MESSAGES_FROM_SELF and is_me(frm):
        return
    # Solo si el destino soy yo o un grupo vigilado
    if not to_is_me_or_monitored_group(to):
        return
    STATE.last_from_per_chat[config.TELEGRAM_CHAT_ID] = frm
    await send_to_telegram(t("rx_generic", frm=frm, to=to, txt=txt))

async def poll_qso_text_loop():
    """
    Pide periodicamente el QSO window (pantalla central).
    """
    while True:
        try:
            if config.FORWARD_QSO_WINDOW and STATE.js8_connected and BRIDGE.js8:
                # Pide el texto del QSO window
                await BRIDGE.js8.send({"type": "RX.GET_TEXT", "params":{}})
        except Exception as e:
            logger.error(f"QSO poll error: {e}")
        await asyncio.sleep(config.QSO_POLL_SECONDS)


# ---- Sondeo periódico del panel derecho (CALL/BAND ACTIVITY) ----
async def poll_call_activity_loop():
    """Sondea periódicamente la "pantalla derecha" para poblar STATE.heard.
    Funciona mejor sobre TCP; en UDP depende de si JS8Call emite las respuestas.
    """
    interval = getattr(config, 'CALL_ACTIVITY_POLL_SECONDS', 30)
    while True:
        try:
            if STATE.js8_connected and BRIDGE and BRIDGE.js8:
                # Solicita la lista de estaciones oídas
                await BRIDGE.js8.send({"type":"RX.GET_CALL_ACTIVITY","params":{}})
                await asyncio.sleep(0.8)
                # Como respaldo pide la actividad de banda (algunas versiones responden aquí)
                await BRIDGE.js8.send({"type": "RX.GET_BAND_ACTIVITY", "params":{}})
        except Exception as e:
            logger.debug(f"poll_call_activity_loop: {e}")
        await asyncio.sleep(max(5, int(interval)))

# ------------- Bridge principal (glue code) -------------

class JS8TelegramBridge:
    def __init__(self):
        self.js8 = None  # JS8ClientTCP | JS8ClientUDP
        self._waiters: dict[str, list[asyncio.Future]] = {}

    async def start_js8(self):
        if config.TRANSPORT.upper() == "TCP":
            self.js8 = JS8ClientTCP(
                config.JS8_HOST,
                config.JS8_PORT,
                self.on_js8_event,   # ← aquí estaba el error
            )
            await self.js8.connect()
        else:
            self.js8 = JS8ClientUDP(
                config.JS8_HOST,
                config.JS8_PORT,
                self.on_js8_event,   # ← y aquí también, por si acaso
            )
            await self.js8.connect()


    def _notify_waiters(self, event_type: str, value):
        lst = self._waiters.pop(event_type, [])
        for fut in lst:
            if not fut.done():
                fut.set_result(value)


    async def get_heard_snapshot(self, timeout: float = 3.5) -> bool:
        if not self.js8 or not STATE.js8_connected:
            return False
        await self.js8.send({"type": "RX.GET_CALL_ACTIVITY", "params": {}, "value": ""})
        await asyncio.sleep(0)
        await self.js8.send({"type": "RX.GET_BAND_ACTIVITY", "params": {}, "value": ""})
    
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if STATE.heard:
                return True
            await asyncio.sleep(0.2)
        return bool(STATE.heard)


    async def on_js8_event(self, evt: dict):

        # Patrones: prefijos "HH:MM:SS - (n) -" y formato FROM [:|>] TO MENSAJE
        QSO_FROMTO_RE = re.compile(
            r'^\s*'
            r'(?:\[\d{2}:\d{2}:\d{2}\]\s*|\d{2}:\d{2}:\d{2}\s*)?'   # [11:22:12] o 11:22:12
            r'(?:[-–—]\s*\(\d+\)\s*[-–—]\s*)?'                     # - (1546) - (opcional)
            r'([@A-Za-z0-9/+-]+)\s*[:>]\s*'                        # FROM
            r'(@?[A-Za-z0-9/+-]{3,})\b\s*'                         # TO
            r'(.*)$'                                               # MENSAJE (puede ser vacío)
        )

        # ====== 1) QSO window (RX.TEXT) ======
        if isinstance(evt, dict) and evt.get("type") == "RX.TEXT":
            full_text = evt.get("value") or ""
            if not isinstance(full_text, str):
                return

            # Solo líneas COMPLETAS hasta el último '\n'; guarda aparte la línea en construcción
            last_nl = full_text.rfind('\n')
            stable_text = full_text[: last_nl + 1] if last_nl != -1 else ""
            trailing = full_text[last_nl + 1:] if last_nl != -1 else full_text
            trailing = trailing.strip()

            # Buffers para trailing sin '\n' y para duplicados
            if not hasattr(self, "_qso_pending_text"):
                self._qso_pending_text = ""
                self._qso_pending_since = 0.0
            if not hasattr(self, "_qso_last_forwarded"):
                self._qso_last_forwarded = ""

            # ===== dif por líneas (procesar SOLO lo nuevo) =====
            old = getattr(STATE, "qso_last_text", "") or ""
            old_lines = old.splitlines(keepends=True)
            new_lines = stable_text.splitlines(keepends=True)
            i = 0
            while i < len(old_lines) and i < len(new_lines) and old_lines[i] == new_lines[i]:
                i += 1
                tail_lines = new_lines[i:]  # ← solo líneas nuevas completas
    
                # Actualiza snapshot DESPUÉS de calcular el tail
                STATE.qso_last_text = stable_text
    
                # Conjuntos para decidir destino permitido y detectar “yo” (estricto)
                allowed_calls  = { _base_callsign(a) for a in config.MY_ALIASES if isinstance(a, str) and a.strip() }
                allowed_groups = { _norm_group(g)    for g in config.MONITORED_GROUPS if _norm_group(g) }

            def _is_me_strict(tok: str) -> bool:
                base = _base_callsign(tok)
                return any(_base_callsign(a) == base for a in config.MY_ALIASES if isinstance(a, str) and a.strip())

            async def _parse_and_maybe_forward(line: str, source: str) -> bool:
                """Parsea una línea del QSO y la reenvía si procede; devuelve True si se envió."""
                m = QSO_FROMTO_RE.match(line)
                if not m:
                    return False

                from_tok, to_tok, msg = m.groups()
                from_cs = (from_tok or "").strip().upper()
                to_tok  = (to_tok  or "").strip()
                raw_msg = (msg     or "")

                # Limpia adornos finales (diamantes) y espacios
                msg_clean = re.sub(r"[♢◇♦♧♤♥]+$", "", raw_msg).strip()

                # ID del QSO (si existe en la línea)
                qso_id = extract_qso_msg_id(line)

                # No reenviar "trailing-immediate" si aún no hay cuerpo (solo FROM→TO)
                if source == "trailing-immediate" and not msg_clean:
                    return False

                # No reenviar nunca si no hay cuerpo
                if not msg_clean:
                    return False

                # Memoria local por-ID para deduplicar (ID + contenido)
                if not hasattr(self, "_qso_last_by_id"):
                    self._qso_last_by_id = {}
                if qso_id:
                    prev = self._qso_last_by_id.get(qso_id)
                    if prev and prev == msg_clean:
                        return False  # mismo contenido ya enviado para este ID

                # No reenviar si el remitente soy yo (comparación estricta base-callsign)
                if _is_me_strict(from_cs):
                    return False

                # Solo si el destino soy yo o uno de mis grupos (usa los sets del closure)
                if to_tok.startswith("@"):
                    if _norm_group(to_tok) not in allowed_groups:
                        return False
                else:
                    if _base_callsign(to_tok) not in allowed_calls:
                        return False

                # Anti-eco: si coincide con lo que acabo de transmitir (mismo TO + mismo cuerpo limpio), ignora
                try:
                    if was_recently_sent(to_tok, msg_clean):
                        return False
                except NameError:
                    pass

                # Evita duplicado inmediato literal
                if line == self._qso_last_forwarded:
                    return False

                # ✅ Reenvía (con fallback si falta la clave i18n)
                self._qso_last_forwarded = line
                try:
                    await send_to_telegram(t("rx_qso_line", line=line))
                except Exception:
                    await send_to_telegram(f"🟢 Mensaje Recibido:\n{line}")

                # Actualiza memoria por-ID y marca reenviado SOLO si la línea ya es estable
                if qso_id:
                    self._qso_last_by_id[qso_id] = msg_clean
                    if source in ("stable", "trailing-stable"):
                        remember_forwarded_id(qso_id)

                return True




    async def tx_message(self, to: str, text: str):
        remember_sent(to, text)
        if not self.js8 or not STATE.js8_connected:
            raise ConnectionError("JS8Call no conectado (TCP).")
        logger.info(f"TX → JS8: {to}: {text}")
        await js8_send_now(to,text)


BRIDGE = JS8TelegramBridge()

# --------------- Telegram Bot Handlers -----------------


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Unhandled exception in handler", exc_info=context.error)

async def restricted_chat(update: Update) -> bool:
    # Solo aceptamos mensajes del chat configurado
    chat_id = update.effective_chat.id if update.effective_chat else None
    if chat_id != config.TELEGRAM_CHAT_ID:
        # Silencioso: ignora otros chats
        return False
    return True

# --------------- Telegram Commands  -----------------

async def cmd_rescan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await restricted_chat(update):
        return
    try:
        if BRIDGE.js8 and STATE.js8_connected:
            await BRIDGE.js8.send({"type": "RX.GET_CALL_ACTIVITY", "params": {}})
            await asyncio.sleep(1.2)
            await BRIDGE.js8.send({"type": "RX.GET_BAND_ACTIVITY", "params": {}})
            await asyncio.sleep(0.6)
    except Exception as e:
        logger.debug(f"rescan error: {e}")
    await update.effective_message.reply_text(
        f"Heard en memoria: {len(STATE.heard)} estaciones."
    )


async def cmd_heartbeat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = 'HEARTBEAT ' + config.GRID
    js8_send_now('@HB',text)
    logger.info(f"TX → JS8: @HB {text}")

    if not await restricted_chat(update):
        return
    if len(context.args) > 0:
        await update.effective_message.reply_text(t("hb_usage"))
        return
    try:
        callsign = "@HB"
        await BRIDGE.tx_message(callsign, text)
        logger.info(f"TX → JS8: @HB {text}")
        await update.effective_message.reply_text(t("hb_sent", text=text))
    except Exception as e:
        await update.effective_message.reply_text(f"Error sending HEARTBEAT: {e}")


async def cmd_stations(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.debug("/stations invoked")
    if not await restricted_chat(update):
        return

    try:
        # 1) Límite
        try:
            limit = int(context.args[0]) if context.args else 20
            limit = max(1, min(limit, 200))
        except Exception:
            limit = 20

        # 2) Snapshot (espera a datos)
        try:
            if BRIDGE and STATE.js8_connected:
                timeout = getattr(config, "CALL_ACTIVITY_TIMEOUT", 2.5)
                ok = await BRIDGE.get_heard_snapshot(timeout)
                logger.debug(f"/stations snapshot ok={ok}, heard={len(STATE.heard)}")
        except Exception as e:
            logger.debug(f"/stations snapshot error: {e}")

        # 3) Datos en memoria
        total = len(STATE.heard)
        if total == 0:
            try:
                none_msg = t("stations_none")
            except Exception:
                none_msg = "Aún no he oído ninguna estación."
            await update.effective_message.reply_text(none_msg)
            return

        now = time.time()
        my_grid = getattr(config, "GRID", None)

        # Ordena por timestamp real (UTC si lo tenemos)
        entries = sorted(
            STATE.heard.values(),
            key=lambda e: (e.get("utc") or e.get("ts") or 0),
            reverse=True,
        )

        def _age(ts: float) -> str:
            if not ts:
                return "—"
            delta = max(0, int(now - ts))
            if delta < 3600:
                return f"{delta//60}m"
            if delta < 86400:
                return f"{delta//3600}h"
            return f"{delta//86400}d"

        def _derive_callsign(e: dict) -> str | None:
            cs = e.get("callsign") or ""
            if CALLSIGN_RE.match(cs):
                return cs
            # intenta sacarlo del TEXT original
            txt = (e.get("text") or "").strip()
            m = re.match(r'\s*([A-Z0-9/]{3,})\s*[:>]', txt, re.I)
            if m and CALLSIGN_RE.match(m.group(1)):
                return m.group(1).upper()
            for tok in txt.split():
                if CALLSIGN_RE.match(tok):
                    return tok.upper()
            return None

        def _derive_grid(e: dict) -> str:
            grid = e.get("grid") or ""
            if grid:
                return grid
            txt = (e.get("text") or "")
            m = re.search(r'\b([A-R]{2}\d{2}(?:[A-X]{2})?(?:\d{2})?)\b', txt, re.I)
            return m.group(1).upper() if m else ""

        # --- Plantillas i18n (con fallback) ---
        try:
            header_tpl = t("stations_header")
        except Exception:
            header_tpl = "📋 Recently heard (top {n} / total {total}):"

        try:
            line_tpl = t("stations_line")
        except Exception:
            line_tpl = "{cs:<9} {dist:<8} SNR {snr:<4} {grid:<6} {age} ago"

        # --- Construcción de líneas usando i18n ---
        lines = []
        count = 0
        for e in entries:
            cs = _derive_callsign(e)
            if not cs:
                continue  # descarta offsets u objetos raros
            snr = e.get("snr")
            grid = _derive_grid(e)

            # distancia en km, si tenemos ambos grids
            dist = None
            if my_grid and grid:
                dist = grid_distance_km(my_grid, grid)

            line_kwargs = {
                "cs": cs,
                "dist": f"{dist} km" if isinstance(dist, (int, float)) else "—",
                "snr": f"{snr:+d}" if isinstance(snr, int) else "—",
                "grid": grid or "",
                "age": _age(e.get("utc") or e.get("ts")),
            }

            try:
                line = line_tpl.format(**line_kwargs)
            except Exception:
                # Fallback por si la plantilla no cuadra
                #line = f"🗼 {cs:<10.10} {line_kwargs['dist']:<10.10} SNR:{line_kwargs['snr']:<4} GRID:{line_kwargs['grid']:<6} {line_kwargs['age']} ago"
                # NO GRID info
                line = f"🗼 {cs:<10.10} {line_kwargs['dist']:<10.10} SNR:{line_kwargs['snr']:<4} {line_kwargs['age']} ago"

            lines.append(line)
            count += 1
            if count >= limit:
                break

        try:
            header = header_tpl.format(n=count, total=total)
        except Exception:
            header = f"📋 Recently heard (top {count} / total {total}):"

        msg = header + "\n" + "\n".join(lines) if lines else header + "\n—"
        for i in range(0, len(msg), 3500):
            await update.effective_message.reply_text(msg[i:i+3500])

    except Exception as ex:
        logger.error(f"/stations error: {ex}", exc_info=ex)
        try:
            await update.effective_message.reply_text("Error mostrando estaciones. Revisa el log.")
        except Exception:
            pass



async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await restricted_chat(update):
        return
    await update.effective_message.reply_text(t("help"))


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await restricted_chat(update):
        return
    js8_ok = "✅" if STATE.js8_connected else "❌"
    last = STATE.last_from_per_chat.get(config.TELEGRAM_CHAT_ID, "—")
    err = STATE.js8_last_error or "—"
    groups = ", ".join(config.MONITORED_GROUPS) if config.MONITORED_GROUPS else "—"
    await update.effective_message.reply_text(
        t("status", js8=js8_ok, last=last, err=err, groups=groups)
    )



async def cmd_to(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await restricted_chat(update):
        return
    if len(context.args) < 2:
        await update.effective_message.reply_text(t("to_usage"))
        return
    callsign = context.args[0].upper()
    text = " ".join(context.args[1:])
    try:
        await BRIDGE.tx_message(callsign, text)
        await update.effective_message.reply_text(t("msg_sent", who=callsign, text=text))
    except Exception as e:
        await update.effective_message.reply_text(t("err_sending", who=callsign, err=e))


async def cmd_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await restricted_chat(update):
        return
    if len(context.args) < 2:
        await update.effective_message.reply_text(t("group_usage"))
        return
    group = context.args[0]
    if not group.startswith("@"):
        await update.effective_message.reply_text(t("group_needs_at"))
        return
    text = " ".join(context.args[1:])
    try:
        await BRIDGE.tx_message(group, text)
        await update.effective_message.reply_text(t("msg_sent", who=group, text=text))
    except Exception as e:
        await update.effective_message.reply_text(t("err_sending", who=group, err=e))


async def cmd_last(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await restricted_chat(update):
        return
    if not context.args:
        await update.effective_message.reply_text(t("last_usage"))
        return
    last = STATE.last_from_per_chat.get(config.TELEGRAM_CHAT_ID)
    if not last:
        await update.effective_message.reply_text(t("last_none"))
        return
    text = " ".join(context.args)
    try:
        await BRIDGE.tx_message(last, text)
        await update.effective_message.reply_text(t("sent_to", who=last, text=text))
    except Exception as e:
        await update.effective_message.reply_text(t("err_sending", who=last, err=e))

async def cmd_rescan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await restricted_chat(update):
        return
    try:
        if BRIDGE and STATE.js8_connected:
            timeout = getattr(config, "CALL_ACTIVITY_TIMEOUT", 2.5)
            ok = await BRIDGE.get_heard_snapshot(timeout)
            logger.debug(f"/rescan -> ok={ok}, heard={len(STATE.heard)}")
        else:
            logger.debug("/rescan: JS8 no conectado")
    except Exception as e:
        logger.debug(f"/rescan error: {e}")
    await update.effective_message.reply_text(
        f"Heard en memoria: {len(STATE.heard)} estaciones."
    )

# =================== END Telegram Commands ======================

# (Optional) allow free text messages  /last:
async def plain_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await restricted_chat(update):
        return
    # Enviar texto suelto al último corresponsal, si existe
    text = (update.effective_message.text or "").strip()
    if not text:
        return
    last = STATE.last_from_per_chat.get(config.TELEGRAM_CHAT_ID)
    if not last:
        await update.effective_message.reply_text("No hay corresponsal previo. Usa /to o /group.")
        return
    try:
        await BRIDGE.tx_message(last, text)
        await update.effective_message.reply_text(f"Enviado a {last}: {text}")
    except Exception as e:
        await update.effective_message.reply_text(f"Error enviando a {last}: {e}")


# --------------- Telegram <-> JS8 bootstrap ---------------

async def send_to_telegram(text: str):
    # Usamos el contexto global del Application
    try:
        await APP.bot.send_message(chat_id=config.TELEGRAM_CHAT_ID, text=text)
    except Exception as e:
        logger.error(f"No se pudo enviar a Telegram: {e}")

async def background_js8_connector():
    """
    Mantiene la conexión con JS8. Si se cae TCP, reintenta cada 5s.
    """
    while True:
        try:
            await BRIDGE.start_js8()
            # Si es TCP, BRIDGE.start_js8 crea un read_loop que se mantiene.
            # Esperamos a que caiga la conexión:
            while STATE.js8_connected:
                await asyncio.sleep(2)
        except Exception as e:
            STATE.js8_connected = False
            STATE.js8_last_error = str(e)
            logger.error(f"Fallo conectando JS8: {e}")
        await asyncio.sleep(5)

async def on_startup(app: Application):
    # Arranca tareas en segundo plano
    asyncio.create_task(background_js8_connector())
    asyncio.create_task(poll_qso_text_loop())
    asyncio.create_task(poll_call_activity_loop())
    logger.info("Puente iniciado. Esperando eventos...")

def build_application() -> Application:

    req = HTTPXRequest(
        connect_timeout=getattr(config, "TG_CONNECT_TIMEOUT", 20),
        read_timeout=getattr(config, "TG_READ_TIMEOUT", 60),
        write_timeout=getattr(config, "TG_WRITE_TIMEOUT", 60),
    )
    application = (
        Application.builder()
        .token(config.TELEGRAM_BOT_TOKEN)
        .request(req)
        .post_init(on_startup)
        .build()
    )
    #application = Application.builder().token(config.TELEGRAM_BOT_TOKEN).post_init(on_startup).build()
  
    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(CommandHandler("status", cmd_status))
    application.add_handler(CommandHandler("to", cmd_to))
    application.add_handler(CommandHandler("group", cmd_group))
    application.add_handler(CommandHandler("last", cmd_last))
    application.add_handler(CommandHandler(["stations","estaciones"], cmd_stations))
    application.add_handler(CommandHandler(["heartbeat","hb"], cmd_heartbeat))
    application.add_handler(CommandHandler(["rescan","scan"], cmd_rescan))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, plain_text_handler))
    application.add_error_handler(error_handler)

    return application

# Application global (para send_to_telegram)
APP: Application = build_application()

def main():
    APP.run_polling(close_loop=False)  # usamos el loop global para nuestras tareas

if __name__ == "__main__":
    main()

