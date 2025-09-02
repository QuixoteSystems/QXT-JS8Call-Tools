#!/usr/bin/env python3
import argparse
import datetime as dt
import json
import socket
import time
from typing import Tuple

def parse_hhmm(s: str) -> Tuple[int, int]:
    h, m = s.split(":")
    return int(h), int(m)

def in_day_window(now: dt.datetime, start: str, end: str) -> bool:
    sh, sm = parse_hhmm(start)
    eh, em = parse_hhmm(end)
    start_t = now.replace(hour=sh, minute=sm, second=0, microsecond=0)
    end_t   = now.replace(hour=eh, minute=em, second=0, microsecond=0)
    if start_t <= end_t:
        return start_t <= now <= end_t
    else:
        # ventana que cruza medianoche (p. ej. 20:00–06:00)
        return now >= start_t or now <= end_t

def parse_freq_to_hz(s: str) -> int:
    s = str(s).strip().lower()
    if s.endswith("mhz"):
        return int(round(float(s[:-3]) * 1_000_000))
    if s.endswith("khz"):
        return int(round(float(s[:-3]) * 1_000))
    if s.endswith("hz"):
        return int(round(float(s[:-2])))
    # sin sufijo: si es >=1000 asumimos Hz; si no, MHz
    val = float(s)
    return int(round(val if val >= 1000 else val * 1_000_000))

def js8call_set_freq(freq_hz: int, host: str, port: int, timeout=2.5) -> None:
    """
    Cambia la frecuencia en JS8Call vía su API TCP (Help → API).
    Ajusta 'type' si tu build usa otro identificador.
    """
    payload = {"type": "RIG.SET_FREQ", "value": freq_hz}
    msg = (json.dumps(payload) + "\n").encode("utf-8")
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.sendall(msg)
        # Si tu JS8Call devuelve algo por el socket, podrías leerlo aquí:
        # _ = sock.recv(4096)

def main():
    ap = argparse.ArgumentParser(
        description="Cambia la frecuencia en JS8Call según horario (día/noche) usando solo la API de JS8Call."
    )
    ap.add_argument("--day-start", default="08:00", help="Inicio de día (HH:MM) local. Ej: 08:00")
    ap.add_argument("--day-end",   default="20:00", help="Fin de día (HH:MM) local. Ej: 20:00")
    ap.add_argument("--day-freq",  required=True,   help="Frecuencia diurna (Hz/kHz/MHz). Ej: 14.078 o 14078000")
    ap.add_argument("--night-freq",required=True,   help="Frecuencia nocturna (Hz/kHz/MHz). Ej: 7.078 o 7078000")
    ap.add_argument("--host",      default="127.0.0.1", help="Host API JS8Call")
    ap.add_argument("--port",      type=int, default=2442, help="Puerto API JS8Call")
    ap.add_argument("--watch",     action="store_true", help="Bucle: vigila y cambia al cruzar umbral")
    ap.add_argument("--interval",  type=int, default=60, help="Segundos entre comprobaciones en --watch")
    args = ap.parse_args()

    day_freq_hz   = parse_freq_to_hz(args.day_freq)
    night_freq_hz = parse_freq_to_hz(args.night_freq)

    def target_freq(now: dt.datetime) -> int:
        return day_freq_hz if in_day_window(now, args.day_start, args.day_end) else night_freq_hz

    last_applied = None

    if not args.watch:
        now = dt.datetime.now()
        freq = target_freq(now)
        js8call_set_freq(freq, args.host, args.port)
        print(f"[JS8Call] Frecuencia puesta a {freq} Hz")
        return

    try:
        while True:
            now = dt.datetime.now()
            freq = target_freq(now)
            if freq != last_applied:
                js8call_set_freq(freq, args.host, args.port)
                print(f"[JS8Call] Frecuencia puesta a {freq} Hz")
                last_applied = freq
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("Saliendo...")

if __name__ == "__main__":
    main()
