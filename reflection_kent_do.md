# Individual Reflection – Kent Do
**CECS 327 – Assignment 2 | Smart Parking System**

---

## What I Implemented

I built the pub/sub system (`pubsub.py`) and the subscriber client (`subscriber_client.py`). The broker assigns each subscriber a dedicated `Queue(maxsize=64)` and uses two background notifier threads to drain all queues concurrently, writing newline-delimited JSON event payloads over TCP. I also integrated the broker into `parking_state.py` so that any change to a lot's occupancy — whether from a RESERVE, CANCEL, or sensor UPDATE — automatically triggers a fan-out notification to all active subscribers. The subscriber client connects to port 9002, sends a SUBSCRIBE command, and prints incoming events to the console in real time.

## A Bug I Fixed

Early on, subscriber notifications were being sent while the `RLock` in `ParkingState` was still held. This caused a subtle deadlock: if a notifier thread tried to read lot state to enrich the event payload, it would block waiting for the same lock that was already held by the thread triggering the notification. I fixed this by moving the listener callback calls to after the `RLock` is released, passing a snapshot of the relevant lot data into the event rather than re-reading state inside the callback.

## One Design Change

My first implementation used a single notifier thread shared across all subscribers. Under a large subscriber count this created a bottleneck — one slow network write to subscriber A delayed delivery to subscriber B. I changed it to two notifier threads, each handling half the subscriber list, which halved worst-case fan-out latency. A further improvement would be one thread per subscriber, but two threads was a reasonable balance between resource use and throughput for our expected subscriber count.

---
*Word count: ~265*
