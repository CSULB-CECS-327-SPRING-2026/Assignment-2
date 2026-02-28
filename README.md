# CSULB Smart Parking System – Assignment 2

**CECS 327 – Distributed Systems**

---

## Architecture Overview

```
  RPC Clients ──────┐
                    │  port 9000
                    ▼
              ┌─────────────┐
              │ TCP Listener│ (accept loop, main thread)
              └──────┬──────┘
                     │ dispatch
                     ▼
              ┌─────────────┐
              │ Thread Pool │  16 workers (configurable)
              └──┬────┬────┬┘
                 ▼    ▼    ▼
            worker  worker  worker
                     │
              ┌──────▼──────┐
              │ ParkingState│ (RLock-protected shared state)
              └──────┬──────┘
                     │ listener hook
                     ▼
              ┌─────────────┐
              │ PubSubBroker│ ──► per-subscriber queues
              └─────────────┘         │
                                       ▼
  Sensor Simulators ──► port 9001   Subscribers ──► port 9002
```

### Thread Model Choice: Bounded Thread Pool

A **dispatcher/worker** pattern with `ThreadPoolExecutor(max_workers=16)` was
chosen over pure thread-per-connection because:

| Concern | Thread-per-connection | Bounded pool |
|---|---|---|
| Memory | Unbounded – 1 thread ≈ 8 MB stack | Capped |
| Scheduling | OS thrash at high concurrency | Predictable |
| Queue | No backlog; new connections blocked at OS | Pool queue |
| Complexity | Simpler | Slightly more setup |

Pool size = 16 matches a typical 8-core server with 2× over-subscription.
`backlog=128` allows the kernel to buffer arriving connections during bursts.

---

## Environment Setup

```bash
# 1. Create virtual environment
python -m venv .venv

# 2. Activate
#    macOS / Linux:
source .venv/bin/activate
#    Windows:
.venv\Scripts\activate

# 3. Install dependencies  (stdlib only – requirements.txt is intentionally empty)
pip install -r requirements.txt
```

---

## Running

### Start the server
```bash
python server.py
```

Reads `config.json` from the current directory (falls back to defaults).

### RPC smoke test
```bash
python rpc_client.py
```

### Sensor simulator (10 updates/sec, 60 seconds)
```bash
python sensor_simulator.py --rate 10 --duration 60 --lots A B C
```

### Pub/sub subscriber
```bash
# Subscribe to all lots
python subscriber_client.py

# Subscribe to lot A only
python subscriber_client.py --lot A
```

### Load test (sync RPC baseline)
```bash
python load_test.py --workers 1 4 8 16 --duration 30 --mode mixed
```

---

## Configuration (`config.json`)

| Key | Default | Description |
|-----|---------|-------------|
| `rpc_port` | 9000 | RPC client port |
| `sensor_port` | 9001 | Sensor update port |
| `pubsub_port` | 9002 | Pub/sub event port |
| `thread_pool_size` | 16 | Max concurrent RPC worker threads |
| `sensor_pool_size` | 4 | Sensor connection threads |
| `backlog` | 128 | TCP listen backlog |
| `reservation_ttl` | 300 | Reservation lifetime (seconds) |
| `lots` | A/B/C | List of `{id, capacity}` objects |

---

## RPC Framing & Marshalling Spec

### Wire Format
```
[ length : uint32 big-endian ] [ JSON payload : UTF-8 ]
```

- **Framing**: 4-byte big-endian length prefix prevents TCP stream fragmentation
  ("message sticking").
- **Endianness**: Big-endian (network byte order, `!I` in Python `struct`).
- **Encoding**: UTF-8 JSON.
- **Max frame size**: 4 MB (enforced client + server).

### Request Schema
```json
{
  "rpcId":  42,
  "method": "reserve",
  "args":   ["A", "7ABC123"]
}
```

### Reply Schema
```json
{
  "rpcId":  42,
  "result": true,
  "error":  null
}
```

### Parameter Passing
- **rpcId**: `uint32` monotonically increasing per client instance.
- **method**: UTF-8 string.
- **args**: JSON array (positional only; no kwargs).
- Integers are JSON numbers (no precision issues for occupancy counts).
- Timestamps are IEEE 754 doubles (Unix epoch seconds).

### Timeout Policy
- Each RPC call enforces a per-call socket timeout (default **5 seconds**).
- On timeout a `TimeoutError` is raised to the caller.
- Clients are responsible for retry logic.

---

## RPC Method Reference

| Method | Args | Returns | Description |
|--------|------|---------|-------------|
| `getLots` | — | `List[Lot]` | All lots with occupancy |
| `getAvailability` | `lotId:str` | `int` | Free spaces |
| `reserve` | `lotId:str, plate:str` | `bool` | True=OK, False=FULL/EXISTS |
| `cancel` | `lotId:str, plate:str` | `bool` | True=cancelled |
| `PING` | — | `"PONG"` | Health check |

Text aliases (same socket, same framing): `LOTS`, `AVAIL <lotId>`,
`RESERVE <lotId> <plate>`, `CANCEL <lotId> <plate>`.

---

## Pub/Sub Design

### Architecture
- **Delivery mechanism**: Dedicated notifier threads + per-subscriber outbound queues.
- **Event subscription**: Clients connect to `PUBSUB_PORT (9002)` and send:
  ```
  SUBSCRIBE <lotId>   ← specific lot
  SUBSCRIBE ALL       ← all lots
  ```
  Server replies `OK <subId>`, then pushes newline-delimited JSON events:
  ```json
  {"type":"EVENT","lotId":"A","free":12,"timestamp":1712345678.9}
  ```

### Event Trigger
Events are published whenever `free` changes due to:
- `RESERVE` or `CANCEL` RPC calls.
- `UPDATE` sensor messages.

### Non-Blocking Guarantee
The notifier path is **fully decoupled** from the RPC request/response path:
- `ParkingState` calls listeners **after releasing the core lock**.
- Each subscriber has an independent `queue.Queue`.
- Two dedicated notifier threads drain queues and write to sockets.
- Slow subscribers cannot delay RPC processing.

### Back-Pressure Policy
```
Bounded per-subscriber queue (MAX_QUEUE_SIZE = 64 events).
  → Queue full: DROP OLDEST event, increment drop counter.
  → drop counter ≥ MAX_OVERFLOW_DROPS (10): DISCONNECT subscriber.
```
Rationale: dropping oldest events preserves freshness; disconnecting
chronic offenders frees server resources. New subscriptions are always
accepted (not rejected at saturation) because queue memory is bounded.

---

## Async Channel Design

```
Sensors ──UPDATE──► SensorHandler ──state.update_occupancy()──► ParkingState
                                                                      │
                                                              listener hook (async)
                                                                      │
                                                              PubSubBroker.on_lot_change()
                                                                      │
                                                    per-subscriber Queue (non-blocking enqueue)
                                                                      │
                                                           notifier thread
                                                                      │
                                                            subscriber sockets
```

Sensor updates are handled by a **separate 4-thread pool** on a dedicated port,
so sensor ingestion never contends with RPC worker threads.

---

## Reliability Notes

- **Sensor disconnects**: `SensorHandler` catches `OSError` gracefully; the
  sensor simulator auto-reconnects.
- **Idempotency**: `RESERVE` returns `EXISTS` for duplicate plates; `CANCEL`
  is safe to call multiple times (returns `NOT_FOUND` after first).
- **Reservation expiry**: Background thread purges expired reservations every
  30 seconds; `reserve()` also purges before checking capacity.
- **Overbooking prevention**: `reserve()` holds the `RLock` for the entire
  check-and-set operation.

---

## File Structure

```
parking/
├── server.py            Main server (RPC + sensor + pubsub listeners)
├── parking_state.py     Thread-safe shared state
├── pubsub.py            Pub/sub broker with back-pressure
├── rpc_framing.py       Length-prefixed JSON framing utilities
├── rpc_client.py        Client stubs + quick smoke test
├── sensor_simulator.py  Sensor update streamer
├── subscriber_client.py Pub/sub event receiver
├── load_test.py         Throughput + latency benchmarking
├── config.json          Server configuration
├── requirements.txt     (stdlib only)
└── README.md
```
