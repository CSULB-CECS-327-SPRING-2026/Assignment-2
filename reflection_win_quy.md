# Individual Reflection – Win Quy
**CECS 327 – Assignment 2 | Smart Parking System**

---

## What I Implemented

I was responsible for the asynchronous sensor channel (`sensor_simulator.py`) and the load testing framework (`load_test.py`). The sensor simulator spawns one thread per lot and fires UPDATE commands at a configurable rate (default 10 Hz) to port 9001, simulating real-world occupancy changes. The load test script spins up 1, 4, 8, or 16 worker threads, each hammering the RPC server with a mixed workload of RESERVE, CANCEL, and AVAIL calls for a fixed 30-second window. Each call is timed with `time.perf_counter` and results — throughput, median latency, and P95 latency — are written to a CSV file for analysis.

## A Bug I Fixed

The load test initially reported inflated throughput numbers because failed requests (e.g., reserving an already-full lot) were still counted as successful completions. This skewed both the req/s figure and the latency distribution. I fixed it by separating successful responses from error responses in the result collector, and only counting calls that received a valid non-error reply toward the throughput metric. This gave much more honest numbers that aligned with what we expected from Chapter 4's discussion of async throughput under contention.

## One Design Change

The sensor simulator originally reconnected immediately on disconnect with no delay, which caused a tight retry loop that flooded the server's accept queue when the server was restarting. I added an exponential back-off starting at 1 second and capping at 16 seconds. This is a standard reliability pattern in distributed systems — it gives the server time to recover and prevents a reconnection storm from making the outage worse than it needs to be.

---
*Word count: ~270*
