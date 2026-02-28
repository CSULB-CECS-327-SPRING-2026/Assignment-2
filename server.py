"""
server.py  –  CSULB Smart Parking Server

Listens on three ports:
  RPC_PORT    (default 9000)  –  RPC clients (length-prefixed JSON)
  SENSOR_PORT (default 9001)  –  Sensor update stream (text, UPDATE <lot> <delta>)
  PUBSUB_PORT (default 9002)  –  Pub/sub event subscribers (one per sub)

Server organisation
-------------------
Dispatcher/worker with a *bounded thread pool* (ThreadPoolExecutor).
Rationale:
  - A pure thread-per-connection model would allow unbounded thread creation,
    wasting memory and causing thrashing under load.
  - A bounded pool (default 16) caps resource usage while still allowing
    genuine parallelism on multi-core machines.
  - Connections that arrive when the pool is saturated are placed in the OS
    accept-backlog (128) and are served as threads become free.
  - Thread pool size is configurable via config.json.

RPC path
--------
  Caller → rpc_client.py (Stub) → TCP → server skeleton → method → return
                                                                    → Stub → Caller
"""

import json
import logging
import os
import socket
import struct
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from parking_state import ParkingState
from pubsub import PubSubBroker
from rpc_framing import recv_message, send_message, FramingError, TimeoutError as RpcTimeout

# ------------------------------------------------------------------
# Logging
# ------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(threadName)-18s %(message)s",
)
log = logging.getLogger(__name__)

# ------------------------------------------------------------------
# Config loader
# ------------------------------------------------------------------
DEFAULT_CONFIG = {
    "rpc_port": 9000,
    "sensor_port": 9001,
    "pubsub_port": 9002,
    "thread_pool_size": 16,
    "sensor_pool_size": 4,
    "backlog": 128,
    "reservation_ttl": 300,
    "lots": [
        {"id": "A", "capacity": 50},
        {"id": "B", "capacity": 30},
        {"id": "C", "capacity": 20},
    ],
}


def load_config(path: str = "config.json") -> dict:
    if os.path.exists(path):
        with open(path) as f:
            cfg = json.load(f)
        merged = {**DEFAULT_CONFIG, **cfg}
        return merged
    return DEFAULT_CONFIG.copy()


# ==================================================================
# Text-protocol + RPC skeleton per-connection handler
# ==================================================================

class ConnectionHandler:
    """Handles one RPC client connection (runs in a pool thread)."""

    def __init__(self, conn: socket.socket, addr, state: ParkingState,
                 broker: PubSubBroker):
        self.conn  = conn
        self.addr  = addr
        self.state = state
        self.broker = broker

    def run(self):
        log.info("client connected addr=%s", self.addr)
        try:
            while True:
                msg = recv_message(self.conn, timeout=60)
                reply = self._dispatch(msg)
                send_message(self.conn, reply)
        except (FramingError, RpcTimeout, OSError) as e:
            log.info("client disconnected addr=%s reason=%s", self.addr, e)
        finally:
            self.conn.close()

    # ------------------------------------------------------------------
    # Skeleton: route RPC method → implementation
    # ------------------------------------------------------------------
    def _dispatch(self, req: dict) -> dict:
        rpc_id = req.get("rpcId", 0)
        method = req.get("method", "")
        args   = req.get("args", [])

        try:
            result = self._invoke(method, args)
            return {"rpcId": rpc_id, "result": result, "error": None}
        except Exception as e:
            log.warning("rpc error method=%s: %s", method, e)
            return {"rpcId": rpc_id, "result": None, "error": str(e)}

    def _invoke(self, method: str, args: list):
        if method == "getLots":
            return self.state.get_lots()

        elif method == "getAvailability":
            lot_id = args[0]
            v = self.state.get_availability(lot_id)
            if v is None:
                raise ValueError(f"Unknown lot: {lot_id}")
            return v

        elif method == "reserve":
            lot_id, plate = args[0], args[1]
            ok, reason = self.state.reserve(lot_id, plate)
            if not ok and reason not in ("FULL", "EXISTS"):
                raise ValueError(reason)
            return ok

        elif method == "cancel":
            lot_id, plate = args[0], args[1]
            ok, reason = self.state.cancel(lot_id, plate)
            return ok

        # ------ Text protocol aliases (used by simple TCP clients) ------
        elif method == "LOTS":
            return self.state.get_lots()

        elif method == "AVAIL":
            lot_id = args[0]
            v = self.state.get_availability(lot_id)
            return v if v is not None else "NOT_FOUND"

        elif method == "RESERVE":
            ok, reason = self.state.reserve(args[0], args[1])
            return reason

        elif method == "CANCEL":
            ok, reason = self.state.cancel(args[0], args[1])
            return reason

        elif method == "PING":
            return "PONG"

        # ------ Pub/sub via RPC ------
        elif method == "subscribe":
            lot_id = args[0] if args else ""
            # open a NEW raw socket back to the client for event push
            # In this design the client is expected to connect on PUBSUB_PORT separately.
            # This method just returns a sub_id stub; real socket registered on pubsub port.
            raise ValueError("Use the pubsub port (9002) to subscribe")

        elif method == "unsubscribe":
            sub_id = args[0]
            return self.broker.unsubscribe(sub_id)

        else:
            raise ValueError(f"Unknown method: {method}")


# ==================================================================
# Sensor handler  (text lines on port 9001)
# ==================================================================

class SensorHandler:
    """Parses UPDATE <lotId> <delta> lines from sensor connections."""

    def __init__(self, conn: socket.socket, addr, state: ParkingState):
        self.conn  = conn
        self.addr  = addr
        self.state = state

    def run(self):
        log.info("sensor connected addr=%s", self.addr)
        buf = ""
        try:
            while True:
                chunk = self.conn.recv(1024)
                if not chunk:
                    break
                buf += chunk.decode("utf-8", errors="replace")
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    self._handle_line(line.strip())
        except OSError as e:
            log.info("sensor disconnected addr=%s: %s", self.addr, e)
        finally:
            self.conn.close()

    def _handle_line(self, line: str):
        if not line:
            return
        parts = line.split()
        if len(parts) == 3 and parts[0] == "UPDATE":
            _, lot_id, delta_str = parts
            try:
                delta = int(delta_str)
            except ValueError:
                log.warning("sensor bad delta: %s", line)
                return
            ok, reason = self.state.update_occupancy(lot_id, delta)
            if not ok:
                log.warning("sensor update failed lot=%s: %s", lot_id, reason)
        else:
            log.warning("sensor unknown line: %r", line)


# ==================================================================
# Pub/sub subscriber handler  (port 9002)
# One persistent connection per subscriber; server pushes EVENT lines.
# ==================================================================

class PubSubHandler:
    """
    Client connects → sends  SUBSCRIBE <lotId>  (or SUBSCRIBE ALL)
                    → server replies with sub_id
                    → server pushes EVENT JSON lines until disconnect.
    """

    def __init__(self, conn: socket.socket, addr, broker: PubSubBroker):
        self.conn   = conn
        self.addr   = addr
        self.broker = broker
        self.sub_id: Optional[str] = None

    def run(self):
        log.info("pubsub client connected addr=%s", self.addr)
        try:
            # Read subscription command
            self.conn.settimeout(10)
            raw = b""
            while b"\n" not in raw:
                chunk = self.conn.recv(256)
                if not chunk:
                    return
                raw += chunk
            self.conn.settimeout(None)

            line = raw.split(b"\n")[0].decode().strip()
            parts = line.split()
            if not parts or parts[0] != "SUBSCRIBE":
                self.conn.sendall(b"ERROR bad command\n")
                return

            lot_id = parts[1] if len(parts) > 1 and parts[1] != "ALL" else ""
            self.sub_id = self.broker.subscribe(self.conn, lot_id)
            self.conn.sendall(f"OK {self.sub_id}\n".encode())
            log.info("pubsub subscribed sub=%s lot=%s addr=%s",
                     self.sub_id, lot_id or "ALL", self.addr)

            # Hold the connection open; the broker writes events to self.conn
            # from its notifier threads.  We just block here waiting for EOF
            # (client disconnect) so we can clean up.
            while True:
                data = self.conn.recv(64)
                if not data:
                    break

        except OSError as e:
            log.info("pubsub client disconnected addr=%s: %s", self.addr, e)
        finally:
            if self.sub_id:
                self.broker.unsubscribe(self.sub_id)
            try:
                self.conn.close()
            except OSError:
                pass


# ==================================================================
# Server bootstrap
# ==================================================================

def make_server_socket(port: int, backlog: int) -> socket.socket:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("", port))
    s.listen(backlog)
    return s


def accept_loop(srv_sock: socket.socket, pool: ThreadPoolExecutor,
                handler_factory):
    """Dispatcher: accept connections and submit to the thread pool."""
    while True:
        try:
            conn, addr = srv_sock.accept()
        except OSError:
            break
        pool.submit(handler_factory(conn, addr).run)


def main():
    cfg     = load_config()
    state   = ParkingState(cfg["lots"], cfg["reservation_ttl"])
    broker  = PubSubBroker(num_notifiers=2)
    state.add_listener(broker.on_lot_change)

    backlog = cfg["backlog"]

    # RPC server
    rpc_sock = make_server_socket(cfg["rpc_port"], backlog)
    rpc_pool = ThreadPoolExecutor(max_workers=cfg["thread_pool_size"],
                                  thread_name_prefix="rpc-worker")

    # Sensor server
    sensor_sock = make_server_socket(cfg["sensor_port"], backlog)
    sensor_pool = ThreadPoolExecutor(max_workers=cfg["sensor_pool_size"],
                                     thread_name_prefix="sensor-worker")

    # Pub/sub server
    pubsub_sock = make_server_socket(cfg["pubsub_port"], backlog)
    pubsub_pool = ThreadPoolExecutor(max_workers=cfg["thread_pool_size"],
                                     thread_name_prefix="pubsub-worker")

    log.info("=== Parking Server starting ===")
    log.info("RPC     port=%d  pool=%d", cfg["rpc_port"],    cfg["thread_pool_size"])
    log.info("Sensor  port=%d  pool=%d", cfg["sensor_port"], cfg["sensor_pool_size"])
    log.info("PubSub  port=%d",          cfg["pubsub_port"])
    log.info("Lots: %s", [f"{l['id']}(cap={l['capacity']})" for l in cfg["lots"]])

    for target, sock, pool, factory in [
        ("rpc",    rpc_sock,    rpc_pool,
         lambda c, a: ConnectionHandler(c, a, state, broker)),
        ("sensor", sensor_sock, sensor_pool,
         lambda c, a: SensorHandler(c, a, state)),
        ("pubsub", pubsub_sock, pubsub_pool,
         lambda c, a: PubSubHandler(c, a, broker)),
    ]:
        t = threading.Thread(target=accept_loop,
                             args=(sock, pool, factory),
                             daemon=True, name=f"accept-{target}")
        t.start()

    log.info("Server ready. Press Ctrl-C to stop.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("Shutting down.")


if __name__ == "__main__":
    main()
