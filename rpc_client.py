"""
rpc_client.py  –  Client-side RPC stubs.

RPC path (from README):
  Caller → Client Stub → TCP → Server Skeleton → Method → Return → Client Stub → Caller

Each stub method:
  1. Assigns a monotonically increasing rpcId.
  2. Serialises method name + args into a length-prefixed JSON frame.
  3. Blocks for the reply (honours per-call timeout → raises TimeoutError).
  4. Unwraps result or raises an error.

Usage
-----
    from rpc_client import ParkingClient
    with ParkingClient("localhost", 9000, timeout=5.0) as client:
        lots = client.get_lots()
        avail = client.get_availability("A")
        ok = client.reserve("A", "7ABC123")
"""

import itertools
import socket
import threading

from rpc_framing import send_message, recv_message, TimeoutError, FramingError

_id_counter = itertools.count(1)
_id_lock    = threading.Lock()


def _next_id() -> int:
    with _id_lock:
        return next(_id_counter)


class RpcError(Exception):
    pass


class ParkingClient:
    """Thread-safe RPC client (one TCP connection per instance)."""

    def __init__(self, host: str = "localhost", port: int = 9000,
                 timeout: float = 5.0):
        self._host    = host
        self._port    = port
        self._timeout = timeout
        self._sock: socket.socket = None
        self._lock    = threading.Lock()   # one RPC in-flight at a time per connection
        self._connect()

    def _connect(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.connect((self._host, self._port))
        self._sock = s

    def _call(self, method: str, *args):
        req = {"rpcId": _next_id(), "method": method, "args": list(args)}
        with self._lock:
            send_message(self._sock, req)
            reply = recv_message(self._sock, timeout=self._timeout)
        if reply.get("error"):
            raise RpcError(reply["error"])
        return reply["result"]

    # ------------------------------------------------------------------
    # Stubs (mirror server skeleton)
    # ------------------------------------------------------------------

    def get_lots(self) -> list:
        """Returns list of lot dicts {id, capacity, occupied, free}."""
        return self._call("getLots")

    def get_availability(self, lot_id: str) -> int:
        """Returns number of free spaces in *lot_id*."""
        return self._call("getAvailability", lot_id)

    def reserve(self, lot_id: str, plate: str) -> bool:
        """
        Reserve a space. Returns True on success.
        Returns False (not exception) when FULL or duplicate plate.
        """
        return self._call("reserve", lot_id, plate)

    def cancel(self, lot_id: str, plate: str) -> bool:
        """Cancel a reservation. Returns True if found and removed."""
        return self._call("cancel", lot_id, plate)

    def ping(self) -> str:
        return self._call("PING")

    def close(self):
        try:
            self._sock.close()
        except OSError:
            pass

    # Context-manager support
    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


# ------------------------------------------------------------------
# Text-protocol client (thin wrapper for simple testing)
# ------------------------------------------------------------------

class TextClient:
    """Wraps the newline-delimited text protocol (LOTS / AVAIL / RESERVE …)."""

    def __init__(self, host="localhost", port=9000, timeout=5.0):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.connect((host, port))
        self._sock.settimeout(timeout)
        self._buf = ""

    def _send(self, line: str) -> str:
        # Convert text command to RPC frame
        parts = line.split()
        method = parts[0]
        args = parts[1:]
        req = {"rpcId": _next_id(), "method": method, "args": args}
        from rpc_framing import send_message, recv_message
        send_message(self._sock, req)
        reply = recv_message(self._sock, timeout=5)
        return reply.get("result", reply.get("error", ""))

    def lots(self):
        return self._send("LOTS")

    def avail(self, lot_id):
        return self._send(f"AVAIL {lot_id}")

    def reserve(self, lot_id, plate):
        return self._send(f"RESERVE {lot_id} {plate}")

    def cancel(self, lot_id, plate):
        return self._send(f"CANCEL {lot_id} {plate}")

    def ping(self):
        return self._send("PING")

    def close(self):
        self._sock.close()

    def __enter__(self): return self
    def __exit__(self, *_): self.close()


# ------------------------------------------------------------------
# Quick smoke-test
# ------------------------------------------------------------------
if __name__ == "__main__":
    import json
    with ParkingClient() as c:
        print("PING  →", c.ping())
        print("LOTS  →", json.dumps(c.get_lots(), indent=2))
        print("AVAIL A →", c.get_availability("A"))
        print("RESERVE A TEST123 →", c.reserve("A", "TEST123"))
        print("AVAIL A →", c.get_availability("A"))
        print("CANCEL A TEST123 →", c.cancel("A", "TEST123"))
        print("AVAIL A →", c.get_availability("A"))
