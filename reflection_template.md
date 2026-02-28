# Individual Reflection – [Your Name]

**CECS 327 – Assignment 2 – Smart Parking System**

---

## What I Implemented

<!-- Fill in your own contribution below (≤ 300 words total) -->

I was responsible for [describe your portion – e.g., the PubSubBroker and
back-pressure policy / the RPC framing layer / the load-test harness / etc.].

Key decisions I made:
- [Decision 1]
- [Decision 2]

---

## A Bug I Fixed

During development I encountered a race condition in `ParkingState.reserve()`
where two threads could simultaneously read `lot.free > 0` and both proceed to
insert a reservation, overbooking the lot.  I fixed this by ensuring the entire
check-and-set sequence executes under the `RLock` without releasing it between
the capacity check and the dictionary write.

<!-- Replace / extend with your actual bug -->

---

## One Design Change

Initially the pub/sub notifier called subscriber socket `.sendall()` while
holding `self._lock`, which caused the notifier to block if a subscriber's TCP
buffer was full, stalling all other subscribers.  I refactored to use per-
subscriber `queue.Queue` objects so the publisher thread only does a non-
blocking enqueue, and separate notifier threads handle the actual socket writes.

<!-- Replace / extend with your actual design change -->
