#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""

JS8tastic
Quixote Systems 2025

JS8Call ⇄ Meshtastic BRIDGE (bidireccional, multi-ruta, ACK opcional)
con heartbeat/reconexión automática del sender JS8Call

"""

import argparse
import json
import logging
import re
import socket
import sys
import threading
import time
from datetime import datetime
from typing import Optional, Tuple, Dict, Any, List


# ───────────── Utilidades ─────────────

def normalize_text(s: str) -> str:
    if not isinstance(s, str):
        return s
    s = s.replace("\u00A0", " ")
    s = s.replace("\u200B", "").replace("\u200C", "").replace("\u200D", "")
    return s.strip()


CALLPREFIX_RE = re.compile(r"^\s*[A-Z0-9/]{3,}:\s*")


def strip_leading_callsign(s: str) -> str:
    return CALLPREFIX_RE.sub("", s, count=1)


AT_RE_STRICT = re.compile(r"^@(?P<tag>\S+)(?:\s+(?P<body>.*))?$")
AT_RE_LOOSE = re.compile(r"@(?P<tag>\S+)(?:\s+(?P<body>.*))?")


def parse_routes(route_items: List[str]) -> Dict[str, List[str]]:
    out: Dict[str, List[str]] = {}
    for item in route_items or []:
        if "=" not in item:
            logging.getLogger("bridge").warning("Ignoring invalid route (TAG=value): %r", item)
            continue
        tag, value = item.split("=", 1)
        tag = (tag or "").strip().lower()
        value = (value or "").strip()
        if tag and value:
            out.setdefault(tag, []).append(value)
    return out


def now_hms() -> str:
    return datetime.now().strftime("%H:%M:%S")


# ───────────── Meshtastic (imports) ─────────────

try:
    from meshtastic.serial_interface import SerialInterface as MSerial
except Exception:
    MSerial = None

try:
    from meshtastic.tcp_interface import TCPInterface as MTCP
except Exception:
    MTCP = None

try:
    from pubsub import pub
except Exception:
    print("ERROR: Falta 'pubsub'. Instala con: pip install -U pubsub", file=sys.stderr)
    raise


def create_tcp_interface(host: str, port: int):
    if MTCP is None:
        raise RuntimeError(
            "Tu paquete 'meshtastic' no expone TCPInterface. Actualiza: pip install -U meshtastic protobuf pubsub")
    last_err = None
    for kwargs in (
            {"hostname": host, "portNumber": port},
            {"hostname": host, "portNum": port},
            {"hostname": host},
            {},
    ):
        try:
            return MTCP(**kwargs)
        except TypeError as e:
            last_err = e
        except Exception as e:
            last_err = e
    raise last_err


# ───────────── ACK Tracker ─────────────

class AckTracker:
    def __init__(self, timeout_sec: int = 30):
        self.timeout = timeout_sec
        self._pending: Dict[int, Dict[str, Any]] = {}
        self._lock = threading.RLock()

    def add(self, request_id: int, text: str):
        with self._lock:
            self._pending[int(request_id)] = {'ts': time.time(), 'text': text}

    def confirm(self, request_id: Optional[int]):
        if request_id is None:
            return None
        rid = int(request_id)
        with self._lock:
            return self._pending.pop(rid, None)

    def sweep_timeouts(self):
        now = time.time()
        expired = []
        with self._lock:
            for rid, info in list(self._pending.items()):
                if now - info['ts'] > self.timeout:
                    expired.append((rid, info))
                    self._pending.pop(rid, None)
        return expired


# ───────────── JS8: RX (listener) ─────────────

class JS8Listener:
    def __init__(self, mode: str, host: str, port: int, buffer_size: int = 65535, logger=None):
        self.mode = mode.lower()
        self.host = host
        self.port = port
        self.buffer_size = buffer_size
        self.sock = None
        self._stop = threading.Event()
        self._thread = None
        self.log = logger or logging.getLogger("js8.listener")

    def start(self, handler):
        if self._thread and self._thread.is_alive():
            return
        target = self._run_udp if self.mode == "udp" else self._run_tcp
        self._thread = threading.Thread(target=target, args=(handler,), daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        try:
            if self.sock:
                self.sock.close()
        except Exception:
            pass
        if self._thread:
            self._thread.join(timeout=2.0)

    def _try_parse_and_handle(self, line: bytes, handler):
        """
        Decodifica una línea (bytes) como JSON UTF-8 y llama al handler(obj).
        Ignora silenciosamente líneas vacías o no-JSON.
        """
        if not line:
            return
        # limpia CR y espacios
        line = line.strip().strip(b"\r")
        if not line:
            return
        try:
            obj = json.loads(line.decode("utf-8", errors="ignore"))
        except Exception as e:
            # si quieres ver qué llegó, sube a DEBUG:
            self.log.debug("JSON parse fail (%s) on: %r", e, line[:200])
            return
        try:
            handler(obj)
        except Exception as e:
            self.log.debug("Handler error: %s", e)

    def _run_udp(self, handler):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((self.host, self.port))
        self.log.info("Listening JS8Call UDP on %s:%d …", self.host, self.port)
        self.sock.settimeout(1.0)
        while not self._stop.is_set():
            try:
                data, _ = self.sock.recvfrom(self.buffer_size)
                # JS8Call suele separar en \n; también limpiamos \r
                for part in data.split(b"\n"):
                    self._try_parse_and_handle(part, handler)
            except socket.timeout:
                continue
            except Exception as e:
                self.log.debug("UDP recv error: %s", e)

    def _run_tcp(self, handler):
        backoff = 1.0
        while not self._stop.is_set():
            try:
                # 1) conectar
                self.sock = socket.create_connection((self.host, self.port), timeout=5.0)
                try:
                    self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                except Exception:
                    pass
                self.log.info("Connected to JS8Call TCP at %s:%d …", self.host, self.port)

                # 2) leer bucles hasta desconexión
                buf = b""
                self.sock.settimeout(1.0)
                backoff = 1.0  # reset al conectar
                while not self._stop.is_set():
                    try:
                        chunk = self.sock.recv(self.buffer_size)
                        if not chunk:
                            # socket cerrado por el otro lado → reconectar
                            self.log.warning("JS8 TCP closed by peer. Reconnecting…")
                            break
                        buf += chunk
                        while b"\n" in buf:
                            line, buf = buf.split(b"\n", 1)
                            self._try_parse_and_handle(line, handler)
                    except socket.timeout:
                        continue
            except Exception as e:
                self.log.warning("JS8 TCP loop error: %s. Reconnecting in %.1fs…", e, backoff)
            finally:
                try:
                    if self.sock:
                        self.sock.close()
                except Exception:
                    pass
                self.sock = None

            # 3) pequeña espera con backoff (máx 10s)
            for _ in range(int(backoff * 10)):
                if self._stop.is_set():
                    break
                time.sleep(0.1)
            backoff = min(10.0, backoff * 1.8)


# ───────────── JS8: TX (sender con heartbeat) ─────────────

import json
import socket
import threading
import time
from typing import Optional, Dict, Any


class JS8Sender:
    """
    Envío estable a JS8Call por TCP (2442) con drenaje continuo.
      - DIRIGIDO: solo API (TX.SEND_MESSAGE), no toca la caja (evita dedupe/mezclas).
      - FREE: @ALLCALL por API → fallback SET_TEXT + TX.SEND (con anti-dedupe mínimo).
      - Hilo _rx_pump que drena frames asíncronos del socket para que no se atasque tras el primer envío.
    """

    def __init__(self,
                 host: str = "127.0.0.1",
                 port: int = 2442,
                 protocol: str = "tcp",   # mantenemos TCP
                 udp_port: int = 2242,
                 heartbeat_secs: int = 30,
                 logger=None,
                 log=None):
        self.host = host
        self.port = port
        self.udp_port = udp_port
        self.protocol = protocol.lower()
        self.heartbeat_secs = heartbeat_secs
        self.log = log or logger or self._make_dummy_logger()
        self._sock: Optional[socket.socket] = None
        self._lock = threading.RLock()
        self._send_mutex = threading.RLock()
        self._rx_buffer = b""
        self._connected = False

        # drenaje/pump
        self._pump_thread: Optional[threading.Thread] = None
        self._pump_stop = threading.Event()
        self._last_seen = 0.0

        # tiempos
        self.send_retries = 3
        self.idle_wait = 8.0
        self.ui_sleep = 0.07
        self.clear_sleep = 0.05
        self.tx_cycle_wait = 15.0

        # anti-dedupe para FREE (cuando repites EXACTO el mismo texto)
        self._last_free_base: Optional[str] = None
        self._zws_choices = ["\u2060", "\u200A", "\u2062", "\u2009"]
        self._zws_idx = 0

    # logging dummy
    def _make_dummy_logger(self):
        class _L:
            def debug(self, *a, **k): pass
            def info(self, *a, **k): pass
            def warning(self, *a, **k): print("WARNING", *a)
            def error(self, *a, **k): print("ERROR", *a)
        return _L()

    # conexión
    def connect(self, timeout: float = 3.0):
        with self._lock:
            self.close()
            if self.protocol != "tcp":
                raise ValueError("Usa TCP aquí (protocol='tcp')")
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(timeout)
            s.connect((self.host, self.port))
            try:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            except Exception:
                pass
            s.setblocking(False)  # non-blocking
            self._sock = s
            self._connected = True
            self._last_seen = time.time()
            self._start_pump()
            self.log.debug("JS8Sender TCP conectado a %s:%d", self.host, self.port)

    def _start_pump(self):
        # hilo que lee y descarta continuamente, para que el socket no se tape
        self._pump_stop.clear()

        def _pump():
            while not self._pump_stop.is_set():
                try:
                    if not self._sock:
                        time.sleep(0.05)
                        continue
                    data = self._sock.recv(8192)
                    if data:
                        self._last_seen = time.time()
                        # partimos por líneas; descartamos (no necesitamos procesar nada aquí)
                        self._rx_buffer += data
                        while b"\n" in self._rx_buffer:
                            _line, self._rx_buffer = self._rx_buffer.split(b"\n", 1)
                    else:
                        # nada disponible ahora
                        time.sleep(0.02)
                except BlockingIOError:
                    time.sleep(0.02)
                except OSError:
                    time.sleep(0.1)
                except Exception:
                    time.sleep(0.05)

        self._pump_thread = threading.Thread(target=_pump, daemon=True)
        self._pump_thread.start()

    def close(self):
        # parar pump
        try:
            self._pump_stop.set()
            if self._pump_thread and self._pump_thread.is_alive():
                self._pump_thread.join(timeout=0.5)
        except Exception:
            pass
        self._pump_thread = None

        with self._lock:
            if self._sock:
                try:
                    self._sock.close()
                except Exception:
                    pass
            self._sock = None
            self._connected = False
            self._rx_buffer = b""

    # bajo nivel
    def _send_raw(self, obj: Dict[str, Any]):
        if not self._connected or not self._sock:
            raise RuntimeError("Socket no conectado")
        data = (json.dumps(obj) + "\n").encode("utf-8")
        # sendall puede lanzar BlockingIOError si el buffer está lleno; reintenta brevemente
        total = 0
        while total < len(data):
            try:
                sent = self._sock.send(data[total:])
                if sent == 0:
                    raise RuntimeError("socket send returned 0")
                total += sent
            except BlockingIOError:
                time.sleep(0.01)

    # API alto nivel
    def request(self, obj: Dict[str, Any], timeout: float = 0.0) -> Dict[str, Any]:
        """
        Enviamos y **no esperamos respuesta** (el hilo pump la drenará).
        Devolvemos {} siempre: evitamos bloquear y saturar.
        """
        try:
            with self._lock:
                self._send_raw(obj)
            return {}
        except Exception as e:
            # reconectar 1 vez
            self._reconnect_safely()
            try:
                with self._lock:
                    self._send_raw(obj)
                return {}
            except Exception:
                self.log.debug("request error: %s", e)
                return {}

    def _reconnect_safely(self):
        try:
            self.connect()
            time.sleep(0.1)
        except Exception:
            pass

    def send_js8(self, obj: Dict[str, Any], timeout: float = 0.0) -> Dict[str, Any]:
        return self.request(obj, timeout=timeout)

    def set_text(self, text: str) -> bool:
        self.request({"type": "TX.SET_TEXT", "value": text}, timeout=0.0)
        return True

    # utilidades
    def js8_is_alive(self, timeout: float = 0.8) -> bool:
        # Con pump activo, consideramos vivo si hubo tráfico en los últimos segundos o el send no falla.
        if not self._connected:
            return False
        if (time.time() - self._last_seen) < 10.0:
            return True
        try:
            self.request({"type": "STATION.GET_CALLSIGN"}, timeout=0.0)
            return True
        except Exception:
            return False

    def js8_wait_idle(self, max_wait: float = None) -> bool:
        # No preguntamos estado (para no saturar); solo una espera corta.
        time.sleep(0.15)
        return True

    def js8_wait_tx_cycle(self, max_wait: float = None) -> bool:
        # No consultamos PTT; pequeña espera para no encadenar órdenes.
        time.sleep(0.3)
        return True

    def _send_with_retry(self, obj: Dict[str, Any], retries: int = 3) -> bool:
        for i in range(retries):
            try:
                self.request(obj, timeout=0.0)
                return True
            except Exception:
                self._reconnect_safely()
            time.sleep(0.15 * (i + 1))
        return False

    # --------- ENVÍOS ---------

    def send_direct(self, to: str, text: str) -> bool:
        """
        DIRIGIDO: SOLO API (TX.SEND_MESSAGE), sin tocar la caja.
        """
        with self._send_mutex:
            if not self._connected:
                self.connect()
            if not self.js8_is_alive():
                return False

            self.js8_wait_idle()

            to_clean = (to or "").strip()
            if to_clean.startswith("@"):
                to_clean = to_clean[1:]
            to_clean = to_clean.upper()
            body = str(text or "").strip()

            for to_var in (to_clean, f"@{to_clean}"):
                #if self._send_with_retry({"type": "TX.SEND_MESSAGE", "value": body, "params": {"TO": to_var}}):
                if self._send_with_retry({"params": {}, "type": "TX.SEND_MESSAGE", "value": to_var + " " + body}):
                    self.js8_wait_tx_cycle()
                    return True
            return False

    def send_directed(self, to: str, text: str) -> bool:
        return self.send_direct(to, text)

    def send_free(self, text: str) -> bool:
        """
        FREE visible:
          1) @ALLCALL por API.
          2) Fallback: SET_TEXT + TX.SEND con anti-dedupe si repites el mismo texto.
        """
        with self._send_mutex:
            if not self._connected:
                self.connect()
            if not self.js8_is_alive():
                try:
                    self.connect()
                except Exception:
                    return False
                if not self.js8_is_alive():
                    return False

            self.js8_wait_idle()

            # 1) Intento ALLCALL por API
            for to in ("@ALLCALL", "ALLCALL"):
                if self._send_with_retry({"type": "TX.SEND_MESSAGE", "value": text, "params": {"TO": to}}):
                    self.js8_wait_tx_cycle()
                    return True

            # 2) Fallback UI
            try:
                base = text or ""
                payload = base
                if self._last_free_base is not None and base == self._last_free_base:
                    z = self._zws_choices[self._zws_idx % len(self._zws_choices)]
                    self._zws_idx += 1
                    payload = base + z

                try:
                    self.set_text("")
                except Exception:
                    pass
                time.sleep(self.clear_sleep)
                self.set_text(payload)
                time.sleep(self.ui_sleep)
                if self._send_with_retry({"type": "TX.SEND"}):
                    self.js8_wait_tx_cycle()
                    try:
                        self.set_text("")
                    except Exception:
                        pass
                    self._last_free_base = base
                    return True
            except Exception as e:
                self.log.warning("UI free send failed: %s", e)

            # 3) Reintento UI
            base = text or ""
            payload = base
            if self._last_free_base is not None and base == self._last_free_base:
                z = self._zws_choices[self._zws_idx % len(self._zws_choices)]
                self._zws_idx += 1
                payload = base + z
            try:
                self.set_text("")
            except Exception:
                pass
            time.sleep(self.clear_sleep)
            self.set_text(payload)
            time.sleep(self.ui_sleep)
            if self._send_with_retry({"type": "TX.SEND"}):
                self.js8_wait_tx_cycle()
                try:
                    self.set_text("")
                except Exception:
                    pass
                self._last_free_base = base
                return True

            return False

    # util varias
    def heartbeat(self) -> Optional[str]:
        if not self._connected:
            self.connect()
        try:
            self.request({"type": "STATION.GET_CALLSIGN"}, timeout=0.0)
            return None
        except Exception:
            return None

    def request_callsign(self) -> Optional[str]:
        try:
            self.request({"type": "STATION.GET_CALLSIGN"}, timeout=0.0)
        except Exception:
            pass
        return None

    def start_heartbeat(self):
        def _hb():
            while True:
                try:
                    ok = self.js8_is_alive()
                    if not ok:
                        self.log.warning("Heartbeat: JS8Call no responde.")
                except Exception as e:
                    self.log.warning("Heartbeat error: %s", e)
                time.sleep(self.heartbeat_secs)
        t = threading.Thread(target=_hb, daemon=True)
        t.start()


# ───────────── Mesh wrapper ─────────────

class Mesh:
    def __init__(self, serial_path: Optional[str], hostport: Optional[str], ack_timeout_sec: int, logger=None):
        self.log = logger or logging.getLogger("mesh")
        # ⬇️ guardar parámetros para poder recrear la interfaz
        self._serial_path = serial_path
        self._hostport = hostport

        if serial_path:
            if MSerial is None:
                raise RuntimeError("No hay SerialInterface. Instala/actualiza: pip install -U meshtastic")
            self.iface = MSerial(serial_path)
        elif hostport:
            host, port = hostport.split(":") if ":" in hostport else (hostport, "4403")
            port = int(port)
            self.iface = create_tcp_interface(host, port)
        else:
            if MSerial is None:
                raise RuntimeError("No hay SerialInterface y no diste --meshtastic-host.")
            self.iface = MSerial()

        # ⬇️ parche mínimo: capturar errores del heartbeat y recrear la interfaz
        self._orig_sendHeartbeat = getattr(self.iface, "sendHeartbeat", None)
        if self._orig_sendHeartbeat:
            def _safe_sendHeartbeat():
                try:
                    return self._orig_sendHeartbeat()
                except (ConnectionResetError, OSError) as e:
                    self.log.warning("Meshtastic heartbeat failed (%s). Recreating interface…", e)
                    self._recreate_iface()
                except Exception as e:
                    # Evitar que muera el hilo programado de la librería
                    self.log.warning("Meshtastic heartbeat exception: %s", e)
            try:
                self.iface.sendHeartbeat = _safe_sendHeartbeat
            except Exception:
                pass

        self.ack = AckTracker(timeout_sec=ack_timeout_sec)

        def _rx_callback(packet=None, interface=None, topic=None, **kwargs):
            try:
                dec = (packet or {}).get("decoded", {}) or {}
                txt = dec.get("text")
                if txt:
                    self.log.info("Mesh RX from %s → %r", packet.get("fromId"), txt)

                if dec.get("ack", False):
                    req_id = dec.get("requestId")
                    info = self.ack.confirm(req_id)
                    if info:
                        self.log.info("✅ ACK from %s for msg: %r", packet.get("fromId"), info['text'])
                    return

                port = dec.get("portnum")
                routing = dec.get("routing") or {}
                err = routing.get("errorReason")
                req_id = dec.get("requestId") or routing.get("requestId")
                if port == "ROUTING_APP" and err == "NONE" and req_id is not None:
                    info = self.ack.confirm(req_id)
                    self.log.info("✅ ROUTING OK from %s (requestId=%s)%s",
                                  packet.get("fromId"), req_id,
                                  f" for msg: {info['text']!r}" if info else "")
            except Exception as e:
                self.log.debug("onReceive parsing error: %s", e)

        pub.subscribe(_rx_callback, "meshtastic.receive")

    def _recreate_iface(self):
        """Cerrar y reabrir la interfaz Meshtastic y re-instalar el patch del heartbeat."""
        try:
            self.log.warning("Recreating Meshtastic interface…")
            if hasattr(self.iface, "close"):
                try:
                    self.iface.close()
                except Exception:
                    pass
            # reconstruir según cómo fue creada originalmente
            if self._serial_path:
                self.iface = MSerial(self._serial_path)
            else:
                host, port = self._hostport.split(":") if (self._hostport and ":" in self._hostport) else (self._hostport, "4403")
                self.iface = create_tcp_interface(host, int(port or 4403))

            # re-instalar el patch del heartbeat
            self._orig_sendHeartbeat = getattr(self.iface, "sendHeartbeat", None)
            if self._orig_sendHeartbeat:
                def _safe_sendHeartbeat():
                    try:
                        return self._orig_sendHeartbeat()
                    except (ConnectionResetError, OSError) as e:
                        self.log.warning("Meshtastic heartbeat failed (%s). Recreating interface…", e)
                        self._recreate_iface()
                    except Exception as e:
                        self.log.warning("Meshtastic heartbeat exception: %s", e)
                try:
                    self.iface.sendHeartbeat = _safe_sendHeartbeat
                except Exception:
                    pass

            self.log.info("Meshtastic interface recreated ✅")
        except Exception as e:
            self.log.error("Failed to recreate Meshtastic interface: %s", e)

    def close(self):
        try:
            if hasattr(self.iface, "close"):
                self.iface.close()
        except Exception as e:
            self.log.debug("error closing iface: %s", e)

    def resolve_dest_id(self, dest: Optional[str], shortname: Optional[str]) -> Optional[str]:
        if dest:
            return dest
        if shortname:
            sn = shortname.lower()
            try:
                for node_id, n in (self.iface.nodes or {}).items():
                    info = n.get("user", {})
                    if str(info.get("shortName", "")).lower() == sn:
                        return node_id
            except Exception:
                pass
        return None

    def resolve_channel_index(self, channel_index: Optional[int], channel_name: Optional[str]) -> Optional[int]:
        if channel_index is not None:
            return channel_index
        if channel_name:
            try:
                chs = self.iface.getChannelList()
                for i, c in enumerate(chs or []):
                    if not c:
                        continue
                    name = (c.get("name") or c.get("psk") or "").strip()
                    if name and name.lower() == channel_name.lower():
                        return i
            except Exception:
                pass
        return None

    def node_shortname(self, node_id: Optional[str]) -> Optional[str]:
        if not node_id:
            return None
        try:
            nodes = self.iface.nodes or {}
            n = nodes.get(node_id)
            if n and n.get("user"):
                return n["user"].get("shortName")
            lid = str(node_id).lower()
            for k, v in nodes.items():
                if str(k).lower() == lid:
                    u = v.get("user") or {}
                    return u.get("shortName")
        except Exception:
            pass
        return None

    def send_text(self, text: str, destination_id: Optional[str] = None,
                  channel_index: Optional[int] = None, want_ack: bool = False):
        self.log.info("→ Meshtastic: %r (dest=%s, ch=%s, ack=%s)", text, destination_id, channel_index, want_ack)
        kwargs: Dict[str, Any] = {}
        if destination_id:
            kwargs["destinationId"] = destination_id
        if channel_index is not None:
            kwargs["channelIndex"] = channel_index
        if want_ack and destination_id:
            kwargs["wantAck"] = True

        try:
            msg_id = self.iface.sendText(text, **kwargs)
        except TypeError:
            kwargs.pop("wantAck", None)
            msg_id = self.iface.sendText(text, **kwargs)
        except Exception as e:
            self.log.error("Failed sending to Meshtastic: %s", e)
            return

        if isinstance(msg_id, dict):
            request_id = msg_id.get("id") or msg_id.get("requestId") or msg_id.get("payloadId")
        else:
            request_id = msg_id

        if want_ack and destination_id and request_id is not None:
            try:
                self.ack.add(int(request_id), text)
            except Exception:
                pass


# ───────────── Extractor de texto JS8 ─────────────

def extract_js8_text(js8_evt: Dict[str, Any]) -> Optional[Tuple[str, str, str]]:
    def get_fields(c: Dict[str, Any]):
        frm = c.get("FROM") or c.get("from") or "UNKNOWN"
        to = c.get("TO") or c.get("to") or ""
        txt = c.get("TEXT") or c.get("text") or ""
        txt = normalize_text(txt)
        return frm, to, txt

    t = (js8_evt.get("type") or js8_evt.get("event") or "").upper()
    if t.startswith("RX"):
        frm, to, txt = get_fields(js8_evt)
        if txt:
            return frm, to, txt
    params = js8_evt.get("params") or js8_evt.get("value") or {}
    if isinstance(params, dict):
        t = (js8_evt.get("type") or "").upper()
        frm, to, txt = get_fields(params)
        if t.startswith("RX") and txt:
            return frm, to, txt
    return None


# ───────────── Matching NodeId/ShortName ─────────────

def _looks_like_suffix(token: str) -> bool:
    return bool(re.fullmatch(r"[A-Fa-f0-9]{3,}", token))


def matches_sender(token: str, from_id: str, mesh: Mesh) -> bool:
    if not token:
        return False
    tok = token.strip()
    if tok.startswith("!") or _looks_like_suffix(tok):
        fid = (from_id or "").strip()
        if not fid:
            return False
        if tok.startswith("!"):
            return fid.lower() == tok.lower()
        return fid.lower().endswith(tok.lower())
    short = mesh.node_shortname(from_id)
    if short is None:
        logging.getLogger("m2j").debug("No shortName yet for %s when matching %r", from_id, tok)
        return False
    return short.lower() == tok.lower()


# ───────────── @CALL al inicio (Meshtastic → JS8) ─────────────

AT_CALL_RE = re.compile(r"^@([A-Za-z0-9/]+)\s*(.*)$")


def split_at_call(msg: str):
    if not isinstance(msg, str):
        return None, None, msg
    m = AT_CALL_RE.match(msg.strip())
    if not m:
        return None, None, msg
    tocall_raw = m.group(1)
    tocall_upper = tocall_raw.upper()
    body = (m.group(2) or "").strip()
    return tocall_raw, tocall_upper, body


# ───────────── Meshtastic → JS8 (dos modos: @@ literal y @ dirigido) ─────────────

class MeshToJS8:
    """
    - '@@CALL MENSAJE' y --m2j-escape-at: envía literal '@CALL MENSAJE' (FREE, SET_TEXT+SEND).
    - '@CALL MENSAJE' (una @): envío dirigido exacto (TO=CALL, value=MENSAJE).
    - Resto: FREE o dirigido según --m2j-to.
    """

    def __init__(self, mesh: Mesh, js8_sender: JS8Sender, to: str, prefix: str,
                 maxlen: int, allow_self: bool, only_from: List[str],
                 j2m_prefix_to_ignore: str, logger=None, escape_at: bool = False):
        self.mesh = mesh
        self.js8 = js8_sender
        self.to = to or "@ALLCALL"
        self.prefix = prefix or ""
        self.maxlen = maxlen
        self.allow_self = allow_self
        self.only_from_raw = [x.strip() for x in (only_from or []) if x and x.strip()]
        self.j2m_prefix_to_ignore = (j2m_prefix_to_ignore or "").strip()
        self.escape_at = bool(escape_at)
        self.log = logger or logging.getLogger("m2j")

        # Anti-eco simple: recuerda últimos (from_id, txt) para evitar duplicados inmediatos.
        # Pon _recent_max = 0 si NO quieres este filtro.
        self._recent: List[Tuple[str, str]] = []
        self._recent_max = 20  # tamaño de ventana

        self.my_id = None
        try:
            info = getattr(self.mesh.iface, "myInfo", None) or {}
            self.my_id = (info.get("myNodeInfo") or {}).get("user", {}).get("id")
        except Exception:
            self.my_id = None

        # Solo una suscripción (evita doble envío)
        pub.subscribe(self.on_receive_text, "meshtastic.receive.text")
        self.log.info("Suscrito a meshtastic.receive.text ✅")

    def _passes_filter(self, from_id: Optional[str]) -> bool:
        if not self.only_from_raw:
            return True
        if not from_id:
            return False
        for tok in self.only_from_raw:
            if matches_sender(tok, from_id, self.mesh):
                return True
        self.log.debug("Filtered out %s by --m2j-only-from %r", from_id, self.only_from_raw)
        return False

    def _truncate(self, s: str) -> str:
        if self.maxlen and len(s) > self.maxlen:
            return s[: self.maxlen - 1] + "…"
        return s

    def _send_free_or_default_dest(self, rendered_text: str):
        dest = (self.to or "").strip()
        up = dest.upper()
        if up in {"@ALLCALL", "ALLCALL"}:
            ok = self.js8.send_free(rendered_text)
            self.log.info("→ JS8 (free) [%d c] ok=%s", len(rendered_text), ok)
        else:
            if dest.startswith("@"):
                dest = dest[1:]
            ok = self.js8.send_directed(dest.upper(), rendered_text)
            self.log.info("→ JS8 (direct to %s) [%d c] ok=%s", dest.upper(), len(rendered_text), ok)

    def _recent_push(self, from_id: str, txt: str) -> bool:
        """Devuelve True si (from_id, txt) ya fue procesado recientemente (dup)."""
        if self._recent_max <= 0:
            return False
        sig = (from_id or "", txt or "")
        if sig in self._recent:
            return True
        self._recent.append(sig)
        if len(self._recent) > self._recent_max:
            # elimina por el principio (FIFO)
            self._recent = self._recent[-self._recent_max:]
        return False

    def on_receive_text(self, packet=None, interface=None, topic=None, **kwargs):
        if not packet:
            return
        try:
            decoded = (packet or {}).get("decoded", {})
            txt = decoded.get("text") or decoded.get("payload")
            if not isinstance(txt, str):
                try:
                    txt = txt.decode("utf-8", errors="ignore")
                except Exception:
                    txt = None
            if not txt:
                return

            txt = normalize_text(txt)

            # Anti-eco: no reinyectar lo que vino de JS8 (prefijo J2M)
            if self.j2m_prefix_to_ignore and txt.startswith(self.j2m_prefix_to_ignore):
                self.log.debug("Ignoring message with J2M prefix (%r): %r", self.j2m_prefix_to_ignore, txt)
                return

            from_id = packet.get("fromId") or f"!{packet.get('from')}"
            if not self._passes_filter(from_id):
                return
            if not self.allow_self and self.my_id and from_id == self.my_id:
                return

            # Anti-dup inmediato por (origen, texto) — evita dobles envíos si la lib publica dos veces
            if self._recent_push(from_id, txt):
                self.log.debug("Duplicate (from_id,txt) ignored: %s | %r", from_id, txt)
                return

            stripped = txt.lstrip()

            # 1) Caso especial @@ → literal FREE (como el script que te funciona)
            if self.escape_at and stripped.startswith('@@'):
                txt_literal = re.sub(r'^(\s*)@@', r'\1@', txt, count=1)
                txt_literal = self._truncate(txt_literal)
                ok = self.js8.send_free(txt_literal)
                self.log.info("➡️  %s -> JS8 (FREE literal from @@) [%d c] ok=%s | text=%r",
                              from_id, len(txt_literal), ok, txt_literal)
                return

            # 2) Caso @CALL MENSAJE → dirigido EXACTO (como tu primer script)
            tocall_raw, tocall_upper, body = split_at_call(txt)
            if tocall_upper:
                body_out = (body or "").strip()
                if self.maxlen and len(body_out) > self.maxlen:
                    body_out = body_out[: self.maxlen - 1] + "…"
                ok = self.js8.send_directed(tocall_upper, body_out)
                if ok:
                    self.log.info("➡️  %s -> JS8 (direct to %s) body=[%d c] ok=True",
                                  from_id, tocall_upper, len(body_out))
                else:
                    self.log.warning("Fallo al enviar a JS8 (direct to %s) body=%r", tocall_upper, body_out)
                return

            # 3) No dirigido → FREE/dirigido según --m2j-to
            short = self.mesh.node_shortname(from_id)
            core = (f"[{short}] " if short else "") + f"{from_id}: {txt}"
            out = self._truncate(f"{self.prefix}{core}".strip())
            self._send_free_or_default_dest(out)

        except Exception as e:
            self.log.warning("on_receive_text error: %s", e)

    # (Mantenemos el método por compat, pero ya no lo usamos)
    def on_receive_any(self, packet=None, interface=None, topic=None, **kwargs):
        try:
            decoded = (packet or {}).get("decoded", {})
            if decoded.get("portnum") == "TEXT_MESSAGE_APP":
                self.on_receive_text(packet=packet, interface=interface, topic=topic, **kwargs)
        except Exception:
            pass


# ───────────── JS8 → Meshtastic ─────────────

def _find_at_anywhere(text: str) -> Optional[Tuple[str, str]]:
    m = AT_RE_LOOSE.search(text.strip())
    if not m:
        return None
    tag = m.group("tag")
    body = (m.group("body") or "").strip()
    return tag, body


class JS8ToMesh:
    def __init__(self, mesh: Mesh, prefix: str, strip_tag: bool,
                 only_tag: Optional[str], chan_routes: Dict[str, List[str]],
                 node_routes: Dict[str, List[str]],
                 default_dest_id: Optional[str], default_chan_idx: Optional[int],
                 want_ack: bool, logger=None):
        self.mesh = mesh
        self.prefix = prefix
        self.strip_tag = strip_tag
        self.only_tag = (only_tag or "").lower() if only_tag else None
        self.chan_routes = chan_routes
        self.node_routes = node_routes
        self.default_dest_id = default_dest_id
        self.default_chan_idx = default_chan_idx
        self.want_ack = want_ack
        self.log = logger or logging.getLogger("j2m")

    def handle_js8_event(self, evt: Dict[str, Any]):
        extracted = extract_js8_text(evt)
        if not extracted:
            return
        frm, _to, text = extracted
        if frm == "UNKNOWN":
            self.log.debug("Ignoring JS8 RX without FROM: %r", text)
            return

        self.log.info("JS8 RX from %s → %r", frm, text)

        text_for_tag = strip_leading_callsign(text)

        m = AT_RE_STRICT.match(text_for_tag)
        if m:
            tag = m.group("tag")
            body = (m.group("body") or "").strip()
        else:
            found = _find_at_anywhere(text_for_tag)
            if not found:
                self.log.debug("No @TAG found in JS8 text after strip: %r", text_for_tag)
                if self.only_tag:
                    return
                return
            tag, body = found

        tag_l = (tag or "").lower()

        if self.strip_tag:
            out_text = body
        else:
            out_text = f"@{tag}" + (f" {body}" if body else "")
        final_msg = f"{self.prefix} {frm}: {out_text}".strip()

        sent_any = False

        for dest in self.node_routes.get(tag_l, []):
            if not dest:
                continue
            if dest.startswith("!"):
                dest_id = dest
            else:
                dest_id = self.resolve_dest_id_compat(dest)
            if not dest_id:
                self.log.warning("No node found for route-node %r (tag=%s)", dest, tag)
                continue
            self.mesh.send_text(final_msg, destination_id=dest_id, channel_index=None, want_ack=self.want_ack)
            sent_any = True

        for ch in self.chan_routes.get(tag_l, []):
            if ch.isdigit():
                ch_idx = int(ch)
            else:
                ch_idx = self.mesh.resolve_channel_index(None, ch)
            if ch_idx is None:
                self.log.warning("Unknown channel for route-chan %r (tag=%s)", ch, tag)
                continue
            self.mesh.send_text(final_msg, destination_id=None, channel_index=ch_idx, want_ack=False)
            sent_any = True

        if not self.chan_routes and not self.node_routes:
            if self.only_tag and tag_l != self.only_tag:
                return
            self.mesh.send_text(final_msg, destination_id=self.default_dest_id,
                                channel_index=self.default_chan_idx, want_ack=self.want_ack)
            sent_any = True

        if not sent_any:
            self.log.info("No route matched for tag '@%s' (ignored).", tag)

    # Compat pequeño para resolver shortName en rutas
    def resolve_dest_id_compat(self, short_or_id: str) -> Optional[str]:
        if short_or_id.startswith("!"):
            return short_or_id
        try:
            sn = short_or_id.lower()
            for node_id, n in (self.mesh.iface.nodes or {}).items():
                info = n.get("user", {})
                if str(info.get("shortName", "")).lower() == sn:
                    return node_id
        except Exception:
            pass
        return None


# ───────────── Main ─────────────

def main():
    ap = argparse.ArgumentParser(description="Puente bidireccional JS8Call ⇄ Meshtastic")
    ap.add_argument("--enable-j2m", default="true", choices=["true", "false"], help="JS8→Meshtastic")
    ap.add_argument("--enable-m2j", default="true", choices=["true", "false"], help="Meshtastic→JS8")

    ap.add_argument("--js8-mode", choices=["udp", "tcp"], default="tcp")
    ap.add_argument("--js8-host", default="127.0.0.1")
    ap.add_argument("--js8-port", type=int, default=2442)

    ap.add_argument("--js8-send-host", default=None)
    ap.add_argument("--js8-send-port", type=int, default=None)
    ap.add_argument("--js8-heartbeat", type=int, default=30, help="Latido TCP al sender JS8 (segundos). 0=off")

    ap.add_argument("--meshtastic-serial", default=None)
    ap.add_argument("--meshtastic-host", default=None, help="IP[:PUERTO]")

    ap.add_argument("--route-chan", action="append", default=[], help="TAG=ChannelName|Index (repetible)")
    ap.add_argument("--route-node", action="append", default=[], help="TAG=ShortName|!Id (repetible)")

    ap.add_argument("--only-tag", default=None)
    ap.add_argument("--strip-tag", action="store_true")
    ap.add_argument("--dest-id", default=None)
    ap.add_argument("--dest-shortname", default=None)
    ap.add_argument("--channel-index", type=int, default=None)
    ap.add_argument("--channel-name", default=None)
    ap.add_argument("--prefix", default="[JS8]")  # Prefijo J2M

    ap.add_argument("--want-ack", action="store_true")
    ap.add_argument("--ack-timeout", type=int, default=30)

    ap.add_argument("--m2j-to", required=False, default="@ALLCALL",
                    help="@ALLCALL o indicativo (p.ej. 30QXT02). Si empieza por '@', se quitará.")
    ap.add_argument("--m2j-prefix", default="[mesh] ")
    ap.add_argument("--m2j-maxlen", type=int, default=200)
    ap.add_argument("--m2j-allow-self", action="store_true")
    ap.add_argument("--m2j-only-from", nargs="*", help="!NodeId, sufijo HEX (EF01), o ShortName (QXT6)")
    ap.add_argument("--m2j-escape-at", action="store_true",
                    help="@@CALL MENSAJE → enviar literal '@CALL MENSAJE' como FREE")

    ap.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])

    args = ap.parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    LOG = logging.getLogger("bridge")

    enable_j2m = (args.enable_j2m.lower() == "true")
    enable_m2j = (args.enable_m2j.lower() == "true")

    if not args.meshtastic_host and not args.meshtastic_serial:
        print("Debes indicar --meshtastic-host IP[:PUERTO] o --meshtastic-serial", file=sys.stderr)
        sys.exit(2)

    mesh = None
    js8_listener = None
    js8_sender = None
    alive_refs: List[Any] = []

    try:
        mesh = Mesh(serial_path=args.meshtastic_serial,
                    hostport=args.meshtastic_host,
                    ack_timeout_sec=args.ack_timeout,
                    logger=logging.getLogger("mesh"))

        chan_routes = parse_routes(args.route_chan)
        node_routes = parse_routes(args.route_node)

        default_dest_id = mesh.resolve_dest_id(args.dest_id, args.dest_shortname)
        default_chan_idx = mesh.resolve_channel_index(args.channel_index, args.channel_name)

        def ack_watchdog(mesh_obj: Mesh):
            while True:
                time.sleep(1.0)
                expired = mesh_obj.ack.sweep_timeouts()
                for rid, info in expired:
                    logging.getLogger("mesh").warning("⏱️  ACK timeout (>%ss) for requestId=%s, msg=%r",
                                                      mesh_obj.ack.timeout, rid, info['text'])

        threading.Thread(target=ack_watchdog, args=(mesh,), daemon=True).start()

        send_host = args.js8_send_host or args.js8_host
        send_port = args.js8_send_port or args.js8_port
        js8_sender = JS8Sender(host=send_host, port=send_port,
                               heartbeat_secs=int(args.js8_heartbeat),
                               logger=logging.getLogger("js8.sender"))
        if enable_m2j:
            js8_sender.connect()
            try:
                js8_sender.request_callsign()
            except Exception:
                pass

        if enable_m2j:
            m2j_handler = MeshToJS8(mesh=mesh,
                                    js8_sender=js8_sender,
                                    to=args.m2j_to,
                                    prefix=args.m2j_prefix,
                                    maxlen=args.m2j_maxlen,
                                    allow_self=args.m2j_allow_self,
                                    only_from=(args.m2j_only_from or []),
                                    j2m_prefix_to_ignore=args.prefix,  # ignora eco desde JS8
                                    logger=logging.getLogger("m2j"),
                                    escape_at=args.m2j_escape_at)  # @@ → @ literal FREE
            alive_refs.append(m2j_handler)

        if enable_j2m:
            j2m_handler = JS8ToMesh(mesh=mesh,
                                    prefix=args.prefix,
                                    strip_tag=args.strip_tag,
                                    only_tag=args.only_tag,
                                    chan_routes=chan_routes,
                                    node_routes=node_routes,
                                    default_dest_id=default_dest_id,
                                    default_chan_idx=default_chan_idx,
                                    want_ack=args.want_ack,
                                    logger=logging.getLogger("j2m"))
            js8_listener = JS8Listener(mode=args.js8_mode, host=args.js8_host, port=args.js8_port,
                                       logger=logging.getLogger("js8.listener"))
            js8_listener.start(j2m_handler.handle_js8_event)
            alive_refs.append(j2m_handler)

        LOG.info("Bridge running. J2M=%s, M2J=%s | Meshtastic=%s",
                 enable_j2m, enable_m2j,
                 args.meshtastic_host or args.meshtastic_serial or "auto-serial")

        while True:
            time.sleep(0.5)

    except KeyboardInterrupt:
        LOG.info("CTRL+C: shutting down…")
    finally:
        if js8_listener:
            logging.getLogger("js8.listener").info("Stopping JS8 listener…")
            js8_listener.stop()
        if js8_sender:
            logging.getLogger("js8.sender").info("Closing JS8 sender…")
            try:
                js8_sender.close()
            except Exception:
                pass
        if mesh:
            logging.getLogger("mesh").info("Closing Meshtastic interface…")
            try:
                mesh.close()
            except Exception:
                pass
        logging.getLogger("bridge").info("Exited cleanly.")


if __name__ == "__main__":
    main()
