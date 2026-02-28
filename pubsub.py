"""
pubsub.py  –  Publish/Subscribe notifier.

Design choice: dedicated notifier thread(s) + per-subscriber outbound queues.
  - Each subscriber gets a bounded deque (MAX_QUEUE_SIZE events).
  - Back-pressure policy: drop OLDEST event when queue is full (never block
    the worker that produced the event).
  - A single notifier thread drains the queues and writes to subscriber sockets.
  - Slow or dead subscribers are disconnected when their queue overflows
    MAX_OVERFLOW_DROPS consecutive times.
"""

import json
import logging
import queue
import socket
import threading
import time
import uuid
from typing import Dict, Set

log = logging.getLogger(__name__)

MAX_QUEUE_SIZE   = 64    # per-subscriber event buffer
MAX_OVERFLOW_DROPS = 10  # disconnect subscriber after this many consecutive drops


class Subscriber:
    def __init__(self, sub_id: str, conn: socket.socket, lot_ids: Set[str]):
        self.sub_id   = sub_id
        self.conn     = conn
        self.lot_ids  = lot_ids          # subscribed lot ids (empty = all)
        self.q: queue.Queue = queue.Queue(maxsize=MAX_QUEUE_SIZE)
        self.drops    = 0
        self.alive    = True

    def enqueue(self, event: dict):
        """Non-blocking enqueue; drops oldest on overflow."""
        try:
            self.q.put_nowait(event)
            self.drops = 0
        except queue.Full:
            try:
                self.q.get_nowait()   # drop oldest
            except queue.Empty:
                pass
            self.q.put_nowait(event)
            self.drops += 1
            log.warning("pubsub back-pressure sub=%s drops=%d", self.sub_id, self.drops)
            if self.drops >= MAX_OVERFLOW_DROPS:
                log.warning("pubsub disconnecting slow sub=%s", self.sub_id)
                self.alive = False


class PubSubBroker:
    """
    Broker that:
      1. Receives lot-change notifications from ParkingState listeners.
      2. Fans out to matching subscribers via per-subscriber queues.
      3. A pool of notifier threads drains the queues.
    """

    def __init__(self, num_notifiers: int = 2):
        self._lock = threading.Lock()
        self._subs: Dict[str, Subscriber] = {}
        # start notifier threads
        for i in range(num_notifiers):
            t = threading.Thread(target=self._notifier_loop,
                                 daemon=True, name=f"notifier-{i}")
            t.start()

    # ------------------------------------------------------------------
    # Called by ParkingState listener hook
    # ------------------------------------------------------------------
    def on_lot_change(self, lot_id: str, free: int, ts: float):
        event = {"type": "EVENT", "lotId": lot_id, "free": free, "timestamp": ts}
        with self._lock:
            dead = []
            for sub in self._subs.values():
                if not sub.alive:
                    dead.append(sub.sub_id)
                    continue
                if not sub.lot_ids or lot_id in sub.lot_ids:
                    sub.enqueue(event)
            for sid in dead:
                self._remove(sid)

    # ------------------------------------------------------------------
    # Subscription management (called from RPC handlers)
    # ------------------------------------------------------------------
    def subscribe(self, conn: socket.socket, lot_id: str) -> str:
        sub_id = str(uuid.uuid4())[:8]
        sub = Subscriber(sub_id, conn, {lot_id} if lot_id else set())
        with self._lock:
            self._subs[sub_id] = sub
        log.info("pubsub subscribe sub=%s lot=%s", sub_id, lot_id)
        return sub_id

    def unsubscribe(self, sub_id: str) -> bool:
        with self._lock:
            return self._remove(sub_id)

    def _remove(self, sub_id: str) -> bool:
        sub = self._subs.pop(sub_id, None)
        if sub:
            sub.alive = False
            try:
                sub.conn.close()
            except Exception:
                pass
            return True
        return False

    # ------------------------------------------------------------------
    # Notifier thread
    # ------------------------------------------------------------------
    def _notifier_loop(self):
        while True:
            with self._lock:
                subs = list(self._subs.values())
            if not subs:
                time.sleep(0.05)
                continue
            for sub in subs:
                if not sub.alive:
                    self.unsubscribe(sub.sub_id)
                    continue
                # drain up to 8 events per subscriber per pass
                for _ in range(8):
                    try:
                        event = sub.q.get_nowait()
                    except queue.Empty:
                        break
                    msg = (json.dumps(event) + "\n").encode()
                    try:
                        sub.conn.sendall(msg)
                    except Exception as e:
                        log.info("pubsub send error sub=%s: %s", sub.sub_id, e)
                        sub.alive = False
                        break
            time.sleep(0.01)
