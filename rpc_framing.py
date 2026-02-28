"""
rpc_framing.py  –  Length-prefixed JSON framing for the RPC layer.

Wire format
-----------
Each message is prefixed by a 4-byte big-endian unsigned integer that carries
the byte-length of the following UTF-8 JSON payload:

  [ length : uint32 BE ] [ JSON payload : length bytes ]

Endianness: big-endian (network byte order) as per IETF convention.
Types used in JSON:
  rpcId  : uint32  (monotonically increasing per client)
  method : str
  args   : list (positional arguments only)
  result : any JSON-serialisable value, or null
  error  : str | null

Choosing JSON over TLV/msgpack keeps the wire human-readable during debugging
while still being framed (avoiding TCP stream fragmentation / "sticking").
"""

import json
import struct
import socket
from typing import Any, Optional

_HDR = struct.Struct("!I")   # network-order uint32
HDR_SIZE = _HDR.size         # 4 bytes


class FramingError(Exception):
    pass


class TimeoutError(Exception):
    pass


# ------------------------------------------------------------------
# Low-level send / recv
# ------------------------------------------------------------------

def send_message(sock: socket.socket, payload: dict) -> None:
    """Serialise *payload* as JSON, prefix with 4-byte length, send."""
    raw = json.dumps(payload).encode("utf-8")
    header = _HDR.pack(len(raw))
    sock.sendall(header + raw)


def recv_message(sock: socket.socket, timeout: Optional[float] = None) -> dict:
    """
    Block until one complete framed message arrives.
    Raises TimeoutError if *timeout* seconds elapse before the message arrives.
    Raises FramingError on malformed data or connection close.
    """
    if timeout is not None:
        sock.settimeout(timeout)
    try:
        header = _recv_exact(sock, HDR_SIZE)
        length, = _HDR.unpack(header)
        if length > 4 * 1024 * 1024:          # sanity cap: 4 MB
            raise FramingError(f"Frame too large: {length} bytes")
        raw = _recv_exact(sock, length)
        return json.loads(raw.decode("utf-8"))
    except socket.timeout:
        raise TimeoutError("RPC timed out")
    except (ConnectionResetError, BrokenPipeError, EOFError) as e:
        raise FramingError(f"Connection lost: {e}")
    finally:
        if timeout is not None:
            sock.settimeout(None)


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise FramingError("Connection closed mid-frame")
        buf += chunk
    return bytes(buf)
