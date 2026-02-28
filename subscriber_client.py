"""
subscriber_client.py  –  Connects to the pub/sub port and prints EVENT notifications.

Usage
-----
    python subscriber_client.py [--host localhost] [--port 9002]
                                [--lot A]          # omit to subscribe to ALL lots
                                [--duration 60]
"""

import argparse
import json
import socket
import time
import logging

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)-7s %(message)s")


def subscribe(host: str, port: int, lot_id: str, duration: float):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.connect((host, port))

    # Send subscription command
    cmd = f"SUBSCRIBE {lot_id}\n" if lot_id else "SUBSCRIBE ALL\n"
    sock.sendall(cmd.encode())

    # Read OK <subId>
    sock.settimeout(5)
    line = b""
    while b"\n" not in line:
        line += sock.recv(256)
    sock.settimeout(None)
    resp = line.split(b"\n")[0].decode().strip()
    if not resp.startswith("OK"):
        log.error("Subscription rejected: %s", resp)
        sock.close()
        return

    sub_id = resp.split()[1]
    log.info("Subscribed  sub_id=%s  lot=%s", sub_id, lot_id or "ALL")

    received = 0
    deadline = time.time() + duration
    buf = ""
    sock.settimeout(1.0)

    try:
        while time.time() < deadline:
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                continue
            if not chunk:
                log.info("Server closed connection")
                break
            buf += chunk.decode("utf-8", errors="replace")
            while "\n" in buf:
                line_str, buf = buf.split("\n", 1)
                line_str = line_str.strip()
                if not line_str:
                    continue
                try:
                    event = json.loads(line_str)
                    received += 1
                    log.info("EVENT  lotId=%-4s  free=%3d  ts=%.3f",
                             event.get("lotId"),
                             event.get("free"),
                             event.get("timestamp", 0))
                except json.JSONDecodeError:
                    log.warning("Non-JSON line: %s", line_str)
    except KeyboardInterrupt:
        pass
    finally:
        sock.close()

    log.info("Done  received=%d events", received)
    return received


def main():
    ap = argparse.ArgumentParser(description="Pub/sub subscriber client")
    ap.add_argument("--host",     default="localhost")
    ap.add_argument("--port",     type=int, default=9002)
    ap.add_argument("--lot",      default="",  help="lot to subscribe to (blank=ALL)")
    ap.add_argument("--duration", type=float, default=60)
    args = ap.parse_args()

    subscribe(args.host, args.port, args.lot, args.duration)


if __name__ == "__main__":
    main()
