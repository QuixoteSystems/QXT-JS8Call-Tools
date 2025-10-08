#!/usr/bin/env python3
# QXT JS8 Tools - SNR Beacon by Quixote Systems
# 31/08/25 v.1.0

import socket
import json
import time
import argparse
import logging, sys
from datetime import datetime

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)])

log = logging.getLogger("js8-snr")

def send_js8(text, host="127.0.0.1", port=2442, transport="TCP", timeout=10):
    """Sends a text to JS8Call using TX.SEND_MESSAGE. Return True/False."""
    pkt = {
        "type": "TX.SEND_MESSAGE",
        "value": text,
        "params": {"_ID": str(int(time.time() * 1000))}
    }
    data = (json.dumps(pkt) + "\n").encode("utf-8")

    if transport.upper() == "UDP":
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(timeout)
        try:
            s.sendto(data, (host, port))
            return True
        except Exception:
            return False
        finally:
            s.close()
    else:  # TCP by default
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        try:
            s.connect((host, port))
            s.sendall(data)
            return True
        except Exception:
            return False
        finally:
            try:
                s.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
            s.close()

def main():
    ap = argparse.ArgumentParser(description="Sends SNR? periodically to a JS8Call Group")
    ap.add_argument("--group", required=True, help="Group Name (without @), p.ej. QXTNET")
    ap.add_argument("--host", default="127.0.0.1", help="JS8Call Host (same PC def: 127.0.0.1)")
    ap.add_argument("--port", type=int, default=2442, help="JS8Call API Port (TCP def: 2442)")
    ap.add_argument("--transport", choices=["TCP", "UDP"], default="TCP", help="API Protocol (def: TCP)")
    ap.add_argument("--minutes", type=int, default=30, help="Interval in minutes (def: 30)")
    args = ap.parse_args()

    group_tag = args.group if args.group.startswith("@") else "@" + args.group.upper()
    interval = args.minutes * 60
    log.info("Starting QXT SNR Beacon...")
    log.info(f"Sending '{group_tag} SNR?' now and each {args.minutes} min "
          f"via {args.transport} - {args.host}:{args.port}")

    def tx_once():
        msg = f"{group_tag} SNR?"
        ok = send_js8(msg, host=args.host, port=args.port, transport=args.transport)
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
      
        if ok:
            log.info(f"TX -> {msg}")
        else:
            log.error("ERROR. Impossible to send.")
        return ok

    try:
        # 1) Initial send
        tx_once()

        # 2) Stable programming: each exact interval, without derive
        next_fire = time.monotonic() + interval
        while True:
            sleep_s = max(0.0, next_fire - time.monotonic())
            time.sleep(sleep_s)
            tx_once()
            next_fire += interval

    except KeyboardInterrupt:
        log.error("\nStop by the user. 73!")

if __name__ == "__main__":
    main()
