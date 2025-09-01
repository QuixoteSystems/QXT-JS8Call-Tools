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
"""

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Optional, Dict, List, Tuple

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

# ===================== CONFIG =====================

TELEGRAM_BOT_TOKEN = "8438015848:AAHRogkCVZnGyH7PGfgkR1uS9Q9tBhY37Hs" # Puedes obtenerlo el token de tu bot (desde @BotFather).
TELEGRAM_CHAT_ID   = 1065228100  # Reemplaza por tu chat ID (int) el chat ID donde quieres recibir/enviar (p.ej., tu chat privado; puedes obtenerlo hablando >
MY_CALLSIGN        = "30QXT01"               # Tu indicativo JS8
MONITORED_GROUPS   = ["@QXTNET"]            # Ejemplo de grupos JS8 que quieres escuchar

JS8_HOST           = "192.168.1.14"
JS8_PORT           = 2442                   # JS8Call API JSON (normalmente 2442)
TRANSPORT          = "TCP"                  # "TCP" (recomendado) o "UDP"

# Para seguridad: evita bucles reenviando lo que tú mismo transmites
IGNORE_MESSAGES_FROM_SELF = True

# =================================================

logger = logging.getLogger("js8_telegram_bridge")
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s: %(message)s",
)

# ----- Utilidades de JS8 API (JSON line-based) -----


async def js8_set_text(text: str):
    # Coloca el texto en la ventana TX de JS8Call
    obj = {"type": "TX.SET_TEXT", "value": text}
    await BRIDGE.js8.send(obj)


async def js8_send_now(text: str):
    """
    Envía directamente el texto por JS8Call sin depender de la caja TX.
    Varias versiones esperan 'value' con la línea completa.
    """
    obj = {"type": "TX.SEND_MESSAGE", "value": text}
    await BRIDGE.js8.send(obj)


def make_composed_text(to: str, text: str) -> str:
    """
    Formato que JS8Call entiende: destino + espacio + mensaje.
    Ej.: "@QXTNET Hola" o "EA4ABC BTU"
    """
      return f"{to} {text}".strip()


def make_tx_message(to: str, text: str) -> dict:
    """
    Construye el JSON de envío JS8: TX.SEND_MESSAGE
    """
    return {
        "type": "TX.SEND_MESSAGE",
        "value": {
            "TO": to,
            "TEXT": text,
            "CALLSIGN": MY_CALLSIGN,
        }
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

    # JS8Call a veces puede soltar strings/valores simples: ignóralos
    if isinstance(obj, dict):
        return obj
    else:
        return None


def extract_from_to_text(evt: dict) -> Optional[Tuple[str, str, str]]:
      """
    Intenta extraer (FROM, TO, TEXT) de cualquier evento JS8Call que los tenga,
    sin importar evt["type"].
    """
    if not isinstance(evt, dict):
        return None
    v = evt.get("value")
    if not isinstance(v, dict):
        return None

    frm = v.get("FROM")
    to  = v.get("TO")
    txt = v.get("TEXT")

    if isinstance(frm, str) and isinstance(to, str) and isinstance(txt, str):
        return frm, to, txt

    # Algunos builds usan minúsculas o campos alternativos; probamos variantes:
    frm = v.get("from") if not isinstance(frm, str) else frm
    to  = v.get("to")   if not isinstance(to, str)  else to
    txt = v.get("text") if not isinstance(txt, str) else txt

    if isinstance(frm, str) and isinstance(to, str) and isinstance(txt, str):
        return frm, to, txt

    return None



def to_is_me_or_monitored_group(to: str) -> bool:
    if to.upper() == MY_CALLSIGN.upper():
        return True
    if any(to.upper() == g.upper() for g in MONITORED_GROUPS):
        return True
    return False
# --------------- Estados compartidos ----------------

@dataclass
class BridgeState:
    last_from_per_chat: Dict[int, str] = field(default_factory=dict)  # chat_id -> last callsign
    js8_connected: bool = False
    js8_last_error: Optional[str] = None

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
      - Envía comandos JSON a JS8Call al mismo host/puerto.
    Nota: según configuración de JS8Call, puede no enviar eventos por UDP.
    """
    def __init__(self, host: str, port: int, on_event):
        self.host = host
        self.port = port
        self.on_event = on_event
        self.transport = None

    async def connect(self):
        loop = asyncio.get_running_loop()
        logger.info(f"Abriendo socket UDP hacia {self.host}:{self.port} ...")
        # Creamos un endpoint UDP; para recibir, nos bindearíamos a ('0.0.0.0', port_local_distinto)
        # Aquí mantenemos solo envío; para recepción habría que conocer cómo JS8Call emite eventos por UDP.
        # Implementamos un "escucha" opcional si fuese necesario.
              # Por compatibilidad básica, dejaremos solo envío y un receptor 'best-effort' en el mismo puerto (puede fallar si ya está en uso).
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
          # ------------- Bridge principal (glue code) -------------

class JS8TelegramBridge:
    def __init__(self):
        self.js8 = None  # JS8ClientTCP | JS8ClientUDP

    async def start_js8(self):
        if TRANSPORT.upper() == "TCP":
            self.js8 = JS8ClientTCP(JS8_HOST, JS8_PORT, self.on_js8_event)
            await self.js8.connect()
        else:
            self.js8 = JS8ClientUDP(JS8_HOST, JS8_PORT, self.on_js8_event)
            await self.js8.connect()

    async def on_js8_event(self, evt: dict):
        # Log básico para ver qué llega (sube a DEBUG si molesta)
        evt_type = evt.get("type") if isinstance(evt, dict) else None
        logger.debug(f"RX ← JS8 event type={evt_type!r}")

        triplet = extract_from_to_text(evt)
        if not triplet:
            # Si quieres ver el contenido crudo cuando no extrae nada:
            logger.debug(f"Evento sin FROM/TO/TEXT util: {evt!r}")
            return

        frm, to, txt = triplet

        # Evita eco si lo envías tú
        if IGNORE_MESSAGES_FROM_SELF and isinstance(frm, str) and frm.upper() == MY_CALLSIGN.upper():
            return

        # Solo reenvía si va a ti o a un grupo que vigilas
        if not isinstance(to, str):
            return
        if not to_is_me_or_monitored_group(to):
                     # Si quieres, comenta esta línea para reenviar TODO lo que oigas
            return

        STATE.last_from_per_chat[TELEGRAM_CHAT_ID] = frm
        message = f"📡 JS8 ⟶ Telegram\nDe: {frm}\nPara: {to}\n\n{txt}"
        await send_to_telegram(message)



    async def tx_message(self, to: str, text: str):
        if not self.js8 or not STATE.js8_connected:
            raise ConnectionError("JS8Call no conectado (TCP).")
        line = make_composed_text(to, text)  # p.ej. "@QXTNET Hola a todos"
        logger.info(f"TX → JS8: {line!r}")
        await js8_send_now(line)


BRIDGE = JS8TelegramBridge()

# --------------- Telegram Bot Handlers -----------------

async def restricted_chat(update: Update) -> bool:
    # Solo aceptamos mensajes del chat configurado
    chat_id = update.effective_chat.id if update.effective_chat else None
    if chat_id != TELEGRAM_CHAT_ID:
        # Silencioso: ignora otros chats
        return False
    return True

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await restricted_chat(update):
        return
    js8_ok = "✅" if STATE.js8_connected else "❌"
    last = STATE.last_from_per_chat.get(TELEGRAM_CHAT_ID, "—")
    err = STATE.js8_last_error or "—"
    msg = (
          f"🔎 Estado del puente\n"
        f"JS8: {js8_ok}\n"
        f"Último corresponsal: {last}\n"
        f"Último error JS8: {err}\n"
        f"Grupos vigilados: {', '.join(MONITORED_GROUPS) if MONITORED_GROUPS else '—'}"
    )
    await update.effective_message.reply_text(msg)

async def cmd_to(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await restricted_chat(update):
        return
    if len(context.args) < 2:
        await update.effective_message.reply_text("Uso: /to CALLSIGN mensaje")
        return
    callsign = context.args[0].upper()
    text = " ".join(context.args[1:])
    try:
        await BRIDGE.tx_message(callsign, text)
        await update.effective_message.reply_text(f"Enviado a {callsign}: {text}")
    except Exception as e:
        await update.effective_message.reply_text(f"Error enviando a {callsign}: {e}")

async def cmd_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await restricted_chat(update):
        return
    if len(context.args) < 2:
        await update.effective_message.reply_text("Uso: /group @GRUPO mensaje")
        return
    group = context.args[0]
    if not group.startswith("@"):
        await update.effective_message.reply_text("El grupo debe empezar por @, p.ej. @QXTNET")
        return
    text = " ".join(context.args[1:])
    try:
        await BRIDGE.tx_message(group, text)
              await update.effective_message.reply_text(f"Enviado a {group}: {text}")
    except Exception as e:
        await update.effective_message.reply_text(f"Error enviando a {group}: {e}")

async def cmd_last(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await restricted_chat(update):
        return
    if not context.args:
        await update.effective_message.reply_text("Uso: /last mensaje   (responde al último corresponsal recibido)")
        return
    last = STATE.last_from_per_chat.get(TELEGRAM_CHAT_ID)
    if not last:
        await update.effective_message.reply_text("No hay corresponsal previo en memoria.")
        return
    text = " ".join(context.args)
    try:
        await BRIDGE.tx_message(last, text)
        await update.effective_message.reply_text(f"Enviado a {last}: {text}")
    except Exception as e:
        await update.effective_message.reply_text(f"Error enviando a {last}: {e}")

# (Opcional) también permitir mensajes de texto “libres” como /last:
async def plain_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await restricted_chat(update):
        return
    # Enviar texto suelto al último corresponsal, si existe
    text = (update.effective_message.text or "").strip()
    if not text:
        return
    last = STATE.last_from_per_chat.get(TELEGRAM_CHAT_ID)
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
        await APP.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text)
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
    # Arranca tarea de conexión JS8
    asyncio.create_task(background_js8_connector())
    logger.info("Puente iniciado. Esperando eventos...")

def build_application() -> Application:
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(on_startup).build()
    application.add_handler(CommandHandler("status", cmd_status))
    application.add_handler(CommandHandler("to", cmd_to))
    application.add_handler(CommandHandler("group", cmd_group))
    application.add_handler(CommandHandler("last", cmd_last))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, plain_text_handler))

    return application

# Application global (para send_to_telegram)
APP: Application = build_application()

def main():
    APP.run_polling(close_loop=False)  # usamos el loop global para nuestras tareas

if __name__ == "__main__":
    main()
