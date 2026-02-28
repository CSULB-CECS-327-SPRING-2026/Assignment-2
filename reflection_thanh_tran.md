# Individual Reflection – Thanh Tran
**CECS 327 – Assignment 2 | Smart Parking System**

---

## What I Implemented

I was responsible for the core server architecture and the RPC framing layer. This included writing `server.py` with its three concurrent TCP listeners on ports 9000, 9001, and 9002, and configuring the `ThreadPoolExecutor` with 16 workers for the main RPC port and 4 workers for the sensor channel. I also designed and implemented `rpc_framing.py`, which defines the length-prefixed binary protocol — a 4-byte big-endian unsigned integer header followed by a UTF-8 JSON payload. This framing ensures the receiver always knows exactly how many bytes to read before parsing, which prevents partial-read bugs common in naive line-based protocols.

## A Bug I Fixed

During early testing, concurrent RESERVE calls from multiple threads occasionally caused overbooking — two clients would both read `free > 0` and both succeed, pushing occupied above capacity. The root cause was a race condition between the availability check and the counter increment. I fixed this by wrapping the entire check-and-modify block inside the `RLock` in `parking_state.py`, ensuring the read and write happen atomically. After the fix, no overbooking was observed even under 16-worker load tests.

## One Design Change

My original design used one thread per connection (the simplest approach). After reading Chapter 3 on resource management, I switched to a bounded `ThreadPoolExecutor`. Thread-per-connection risks spawning hundreds of threads under load, each consuming ~8 MB of stack space and adding scheduler overhead. The bounded pool caps memory usage and keeps context-switch cost predictable, which was clearly reflected in the load test results — throughput scaled well up to 8 workers before contention on the shared lock became the bottleneck.

---
*Word count: ~260*
