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
from i18n import t

from dataclasses import dataclass, field
from typing import Optional, Dict, List, Tuple
from collections import deque

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
from telegram.request import HTTPXRequest


# =================================================

logger = logging.getLogger("js8_telegram_bridge")
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s: %(message)s",
)

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


# ----- Utilidades de JS8 API (JSON line-based) -----

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

CALLSIGN_RE = re.compile(r"^[A-Z0-9/]{3,}(?:-\d{1,2})?$", re.I)

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
    Extrae info de un evento RX.SPOT (panel derecho "heard") y la normaliza.
    Devuelve un dict: {callsign, snr, grid, freq, offset, ts}
    """
    if not isinstance(evt, dict) or evt.get("type") != "RX.SPOT":
        return None
    v = evt.get("value") or {}

    cs = v.get("CALLSIGN") or v.get("STATION") or v.get("from") or v.get("CALL")
    if not isinstance(cs, str):
        return None

    # Sanitiza SNR (puede llegar como str)
    snr = v.get("SNR")
    try:
        snr = int(snr)
    except Exception:
        try:
            snr = round(float(snr))
        except Exception:
            snr = None

    grid = v.get("GRID") or v.get("grid")
    freq = v.get("FREQ") or v.get("freq")
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

def _extract_callsign_from_line(line: str) -> Optional[str]:
    if not isinstance(line, str):
        return None
    tokens = re.findall("[A-Za-z0-9/+-]+", line.upper())
    for tok in tokens:
        if CALLSIGN_RE.match(tok):
            return _base_callsign(tok)
    return None


def update_heard_from_call_activity(value) -> None:
    """Normaliza el contenido de RX.CALL_ACTIVITY (texto o lista) y llena STATE.heard."""
    now = time.time()

    # dict contenedor
    if isinstance(value, dict):
        if isinstance(value.get("list"), list):
            for it in value["list"]:
                update_heard_from_call_activity(it)
            return
        if isinstance(value.get("text"), str):
            update_heard_from_call_activity(value["text"])  # recursivo
            return

    # lista de objetos o líneas
    if isinstance(value, list):
        for it in value:
            update_heard_from_call_activity(it)
        return

    # registro dict por estación
    if isinstance(value, dict):
        v = value
        cs = v.get("CALLSIGN") or v.get("CALL") or v.get("from")
        if isinstance(cs, str):
            csb = _base_callsign(cs)
            snr = v.get("SNR") if isinstance(v.get("SNR"), (int, float)) else None
            grid = v.get("GRID") if isinstance(v.get("GRID"), str) else None
            STATE.heard[csb] = {
                "callsign": csb,
                "snr": int(round(snr)) if isinstance(snr, (int, float)) else None,
                "grid": grid,
                "freq": v.get("FREQ"),
                "offset": v.get("OFFSET"),
                "ts": now,
            }
        return

    # texto multilinea del panel derecho
    if isinstance(value, str):
        lines = [l.strip() for l in value.splitlines() if l.strip()]
        for l in lines:
            csb = _extract_callsign_from_line(l)
            if not csb:
                continue
            STATE.heard[csb] = {
                "callsign": csb,
                "snr": None,
                "grid": None,
                "freq": None,
                "offset": None,
                "ts": now,
            }
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
                await BRIDGE.js8.send({"params":{},"type": "RX.GET_TEXT", "value": ""})
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
                await BRIDGE.js8.send({"params":{},"type":"RX.GET_CALL_ACTIVITY","value":""})
                await asyncio.sleep(0.8)
                # Como respaldo pide la actividad de banda (algunas versiones responden aquí)
                await BRIDGE.js8.send({"params":{},"type": "RX.GET_BAND_ACTIVITY", "value": ""})
        except Exception as e:
            logger.debug(f"poll_call_activity_loop: {e}")
        await asyncio.sleep(max(5, int(interval)))

# ------------- Bridge principal (glue code) -------------

class JS8TelegramBridge:
    def __init__(self):
        self.js8 = None  # JS8ClientTCP | JS8ClientUDP

    async def start_js8(self):
        if config.TRANSPORT.upper() == "TCP":
            self.js8 = JS8ClientTCP(config.JS8_HOST, config.JS8_PORT, self.on_js8_event)
            await self.js8.connect()
        else:
            self.js8 = JS8ClientUDP(config.JS8_HOST, config.JS8_PORT, self.on_js8_event)
            await self.js8.connect()


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
                msg     = (msg     or "").strip()

                # 0) Deduplicación por ID del QSO (si existe)
                qso_id = extract_qso_msg_id(line)
                if qso_id and was_id_forwarded(qso_id):
                    return False

                # 1) No reenviar si el REMITENTE soy yo (comparación estricta base-callsign)
                if _is_me_strict(from_cs):
                    return False
                  
                # 2) Solo si el DESTINO soy yo (alias/base) o uno de mis grupos
                if to_tok.startswith("@"):
                    if _norm_group(to_tok) not in allowed_groups:
                        return False
                else:
                    if _base_callsign(to_tok) not in allowed_calls:
                        return False

                # 3) Anti-eco: si coincide con lo que ACABO de transmitir (mismo TO + mismo cuerpo), ignora
                try:
                    if was_recently_sent(to_tok, msg):
                        return False
                except NameError:
                    pass

                # 4) Evita duplicado inmediato (por seguridad extra)
                if line == self._qso_last_forwarded:
                    return False

                # 5) Reenvia y recuerda ID (si hay)
                self._qso_last_forwarded = line
                await send_to_telegram(t("rx_qso_line", line=line))
                if qso_id:
                    remember_forwarded_id(qso_id)
                return True

            # 1.a) Procesa SOLO las lineas nuevas completas
            for raw in tail_lines:
                line = raw.strip()
                if not line:
                    continue
                await _parse_and_maybe_forward(line, "stable")

            # 1.b) Linea final sin '\n': si ya esta completa (FROM→TO), enviala UNA VEZ cuando se estabilice
            if trailing:
                now = time.time()
                poll = globals().get("config.QSO_POLL_SECONDS", 2.0)
                if QSO_FROMTO_RE.match(trailing) and trailing != self._qso_last_forwarded:
                    await _parse_and_maybe_forward(trailing, "trailing-immediate")
                if trailing == self._qso_pending_text:
                    if now - self._qso_pending_since >= max(0.8 * poll, 1.0):
                        if trailing != self._qso_last_forwarded:
                            await _parse_and_maybe_forward(trailing, "trailing-stable")
                        self._qso_pending_text = ""
                        self._qso_pending_since = 0.0
                else:
                    self._qso_pending_text = trailing
                    self._qso_pending_since = now
            else:
                self._qso_pending_text = ""
                self._qso_pending_since = 0.0

            return  # no procesar RX.TEXT en otras ramas!

        # ====== 2) RX.CALL_ACTIVITY → heard list ======
        if isinstance(evt, dict) and evt.get("type") == "RX.CALL_ACTIVITY":
            try:
                val = evt.get("value")
                update_heard_from_call_activity(val)
                logger.debug(f"CALL_ACTIVITY recibido: heard={len(STATE.heard)}")
            except Exception as ex:
                logger.debug(f"CALL_ACTIVITY parse error: {ex}")
            return

        # ====== 3) RX.BAND_ACTIVITY → heard list (fallback) ======
        if isinstance(evt, dict) and evt.get("type") == "RX.BAND_ACTIVITY":
            try:
                val = evt.get("value")
                update_heard_from_call_activity(val)
                logger.debug(f"BAND_ACTIVITY recibido: heard={len(STATE.heard)}")
            except Exception as ex:
                logger.debug(f"BAND_ACTIVITY parse error: {ex}")
            return

        # ====== 4) RX.SPOT → heard list (/estaciones) ======
        if isinstance(evt, dict) and evt.get("type") == "RX.SPOT":
            try:
                spot = parse_rx_spot(evt)
                if spot and isinstance(spot.get("callsign"), str):
                    STATE.heard[spot["callsign"]] = spot
            except Exception:
                pass
            return

        # ====== 3) Reenvio generico (otros eventos dirigidos) ======
        try:
            triplet = extract_from_to_text(evt)
        except Exception:
            triplet = None
        if not triplet:
            return

        frm, to, txt = triplet
        basef = _base_callsign(frm)
        if any(_base_callsign(a) == basef for a in config.MY_ALIASES):
            return
        if not to_is_me_or_monitored_group(to):
            return
        try:
            if was_recently_sent(to, txt):
                return
        except NameError:
            pass

        await send_to_telegram(t("rx_generic", frm=frm, to=to, txt=txt))



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
            await BRIDGE.js8.send({"type": "RX.GET_CALL_ACTIVITY", "value": "", "params": {}})
            await asyncio.sleep(1.2)
            await BRIDGE.js8.send({"type": "RX.GET_BAND_ACTIVITY", "value": "", "params": {}})
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
    if not await restricted_chat(update):
        return

    # Limite opcional: /estaciones 30
    try:
        limit = int(context.args[0]) if context.args else 20
        limit = max(1, min(limit, 100))
    except Exception:
        limit = 20

    # Fuerza un refresco de la Call Activity del panel derecho y, como fallback, la Band Activity
    try:
        if BRIDGE.js8 and STATE.js8_connected:
            await BRIDGE.js8.send({"params":{},"type":"RX.GET_CALL_ACTIVITY","value":""})
            await asyncio.sleep(1.5)
            await BRIDGE.js8.send({"type": "RX.GET_BAND_ACTIVITY", "value": "", "params": {}})
            await asyncio.sleep(0.7)
    except Exception as e:
        logger.debug(f"No se pudo pedir CALL/BAND_ACTIVITY: {e}")

    if not STATE.heard:
        await update.effective_message.reply_text(t("stations_none"))
        return

    header = t("stations_header", n=min(limit, len(entries)))
    lines_fmt = []
    for e in entries[:limit]:
        cs   = e.get("callsign", "")
        snr  = e.get("snr")
        grid = e.get("grid") or ""
        age_s = max(0, int(now - (e.get("ts") or now)))
        age = f"{age_s//60}m" if age_s < 3600 else f"{age_s//3600}h"
        snr_txt = f"SNR {snr:+d}" if isinstance(snr, int) else ""
        lines_fmt.append(t("stations_line", cs=cs, snr_txt=snr_txt, grid=grid, age=age))
    
    msg = header + "\n" + "\n".join(lines_fmt)
    await update.effective_message.reply_text(msg)


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


# (Opcional) también permitir mensajes de texto “libres” como /last:
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

