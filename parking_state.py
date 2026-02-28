"""
parking_state.py  –  Shared, thread-safe parking lot state.

All public methods acquire the internal lock before mutating state,
so callers do not need their own synchronisation.
"""

import threading
import time
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

RESERVATION_TTL = 300          # seconds (5 minutes, configurable via config)


@dataclass
class Lot:
    lot_id: str
    capacity: int
    occupied: int = 0
    # plate -> expiry_epoch
    reservations: Dict[str, float] = field(default_factory=dict)

    @property
    def free(self) -> int:
        return max(0, self.capacity - self.occupied - len(self.reservations))

    def to_dict(self) -> dict:
        return {
            "id": self.lot_id,
            "capacity": self.capacity,
            "occupied": self.occupied,
            "free": self.free,
        }


class ParkingState:
    """
    Central shared state.  A single reentrant lock protects every mutation.
    A background thread periodically expires stale reservations.
    """

    def __init__(self, lots_config: List[dict], reservation_ttl: int = RESERVATION_TTL):
        self._lock = threading.RLock()
        self._ttl = reservation_ttl
        self._lots: Dict[str, Lot] = {}
        for cfg in lots_config:
            lot = Lot(lot_id=cfg["id"], capacity=cfg["capacity"])
            self._lots[lot.lot_id] = lot
        # change listeners: called with (lot_id, free_count) on every state change
        self._listeners: List = []
        # start expiry thread
        t = threading.Thread(target=self._expiry_loop, daemon=True, name="expiry")
        t.start()

    # ------------------------------------------------------------------
    # Listener registration (used by pub/sub notifier)
    # ------------------------------------------------------------------
    def add_listener(self, fn):
        with self._lock:
            self._listeners.append(fn)

    def remove_listener(self, fn):
        with self._lock:
            self._listeners.discard(fn) if hasattr(self._listeners, 'discard') else None
            try:
                self._listeners.remove(fn)
            except ValueError:
                pass

    def _notify(self, lot_id: str, free: int):
        """Called with lock held; notify outside lock to avoid deadlock."""
        listeners = list(self._listeners)
        # release lock while notifying
        self._lock.release()
        try:
            ts = time.time()
            for fn in listeners:
                try:
                    fn(lot_id, free, ts)
                except Exception as e:
                    log.warning("listener error: %s", e)
        finally:
            self._lock.acquire()

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------
    def get_lots(self) -> List[dict]:
        with self._lock:
            return [lot.to_dict() for lot in self._lots.values()]

    def get_availability(self, lot_id: str) -> Optional[int]:
        with self._lock:
            lot = self._lots.get(lot_id)
            return lot.free if lot else None

    # ------------------------------------------------------------------
    # Mutations
    # ------------------------------------------------------------------
    def reserve(self, lot_id: str, plate: str) -> Tuple[bool, str]:
        """
        Returns (success, reason).
        reason in {OK, FULL, EXISTS, NOT_FOUND}
        """
        with self._lock:
            lot = self._lots.get(lot_id)
            if lot is None:
                return False, "NOT_FOUND"
            self._purge_expired(lot)
            if plate in lot.reservations:
                return False, "EXISTS"
            if lot.free <= 0:
                return False, "FULL"
            lot.reservations[plate] = time.time() + self._ttl
            log.info("RESERVE lot=%s plate=%s free=%d", lot_id, plate, lot.free)
            free = lot.free
            self._notify(lot_id, free)
            return True, "OK"

    def cancel(self, lot_id: str, plate: str) -> Tuple[bool, str]:
        with self._lock:
            lot = self._lots.get(lot_id)
            if lot is None:
                return False, "NOT_FOUND"
            if plate not in lot.reservations:
                return False, "NOT_FOUND"
            del lot.reservations[plate]
            log.info("CANCEL lot=%s plate=%s free=%d", lot_id, plate, lot.free)
            free = lot.free
            self._notify(lot_id, free)
            return True, "OK"

    def update_occupancy(self, lot_id: str, delta: int) -> Tuple[bool, str]:
        """Apply a sensor delta (+1 / -1)."""
        with self._lock:
            lot = self._lots.get(lot_id)
            if lot is None:
                return False, "NOT_FOUND"
            new_occ = max(0, min(lot.capacity, lot.occupied + delta))
            lot.occupied = new_occ
            log.info("UPDATE lot=%s delta=%+d occupied=%d free=%d",
                     lot_id, delta, lot.occupied, lot.free)
            free = lot.free
            self._notify(lot_id, free)
            return True, "OK"

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _purge_expired(self, lot: Lot):
        """Must be called with lock held."""
        now = time.time()
        expired = [p for p, exp in lot.reservations.items() if now > exp]
        for p in expired:
            del lot.reservations[p]
            log.info("EXPIRE reservation lot=%s plate=%s", lot.lot_id, p)

    def _expiry_loop(self):
        while True:
            time.sleep(30)
            with self._lock:
                for lot in self._lots.values():
                    self._purge_expired(lot)
