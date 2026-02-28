"""
sensor_simulator.py  –  Simulates parking lot sensors.

Each sensor randomly picks a lot and sends UPDATE <lotId> +1/-1 lines
at a configurable rate (default: 10 updates/sec/lot).

Usage
-----
    python sensor_simulator.py [--host localhost] [--port 9001]
                               [--rate 10] [--lots A B C]
                               [--duration 30]
"""

import argparse
import random
import socket
import time
import threading
import logging

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)-7s %(message)s")


class SensorWorker(threading.Thread):
    """One thread per lot; sends updates at *rate* Hz."""

    def __init__(self, host: str, port: int, lot_id: str,
                 rate: float, duration: float):
        super().__init__(daemon=True, name=f"sensor-{lot_id}")
        self.host     = host
        self.port     = port
        self.lot_id   = lot_id
        self.interval = 1.0 / rate
        self.duration = duration
        self.sent     = 0

    def run(self):
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.connect((self.host, self.port))
        except OSError as e:
            log.error("sensor %s cannot connect: %s", self.lot_id, e)
            return

        deadline = time.time() + self.duration
        while time.time() < deadline:
            delta = random.choice([+1, -1])
            line  = f"UPDATE {self.lot_id} {delta:+d}\n"
            try:
                sock.sendall(line.encode())
            except OSError as e:
                log.warning("sensor %s send error: %s – reconnecting", self.lot_id, e)
                try:
                    sock.close()
                    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    sock.connect((self.host, self.port))
                except OSError:
                    break
            self.sent += 1
            time.sleep(self.interval)

        sock.close()
        log.info("sensor %s done  sent=%d", self.lot_id, self.sent)


def main():
    ap = argparse.ArgumentParser(description="Parking sensor simulator")
    ap.add_argument("--host",     default="localhost")
    ap.add_argument("--port",     type=int, default=9001)
    ap.add_argument("--rate",     type=float, default=10,
                    help="updates per second per lot")
    ap.add_argument("--lots",     nargs="+", default=["A", "B", "C"])
    ap.add_argument("--duration", type=float, default=60,
                    help="seconds to run")
    args = ap.parse_args()

    log.info("Starting %d sensor workers  rate=%.1f/s  duration=%.0fs",
             len(args.lots), args.rate, args.duration)

    workers = [
        SensorWorker(args.host, args.port, lot_id, args.rate, args.duration)
        for lot_id in args.lots
    ]
    for w in workers:
        w.start()
    for w in workers:
        w.join()

    total = sum(w.sent for w in workers)
    log.info("All sensors done  total_sent=%d", total)


if __name__ == "__main__":
    main()
