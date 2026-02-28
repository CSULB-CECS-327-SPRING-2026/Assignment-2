"""
load_test.py  –  Sync RPC baseline load test.

Runs 30-second windows with 1/4/8/16 parallel client workers.
Measures throughput (req/s) and latency (median, 95th percentile).

Usage
-----
    python load_test.py [--host localhost] [--port 9000]
                        [--workers 1 4 8 16] [--duration 30]
                        [--mode avail|reserve|mixed]
"""

import argparse
import csv
import os
import random
import statistics
import string
import threading
import time
import logging

from rpc_client import ParkingClient, RpcError
from rpc_framing import TimeoutError

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.WARNING,
                    format="%(asctime)s %(levelname)-7s %(message)s")


def rand_plate() -> str:
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=7))


def worker_fn(host: str, port: int, mode: str,
              duration: float, results: list, stop_event: threading.Event):
    """One worker: keeps sending requests until stop_event is set."""
    latencies = []
    errors    = 0
    lots      = ["A", "B", "C"]

    try:
        client = ParkingClient(host, port, timeout=5.0)
    except OSError as e:
        log.error("cannot connect: %s", e)
        results.append({"latencies": [], "errors": 1})
        return

    try:
        while not stop_event.is_set():
            lot = random.choice(lots)
            t0  = time.perf_counter()
            try:
                if mode == "avail":
                    client.get_availability(lot)
                elif mode == "reserve":
                    client.reserve(lot, rand_plate())
                else:  # mixed
                    if random.random() < 0.7:
                        client.get_availability(lot)
                    else:
                        client.reserve(lot, rand_plate())
                latencies.append(time.perf_counter() - t0)
            except (RpcError, TimeoutError, OSError):
                errors += 1
    finally:
        client.close()

    results.append({"latencies": latencies, "errors": errors})


def run_scenario(host, port, num_workers, duration, mode):
    results = []
    stop    = threading.Event()
    threads = []

    t_start = time.time()
    for _ in range(num_workers):
        t = threading.Thread(target=worker_fn,
                             args=(host, port, mode, duration, results, stop))
        t.start()
        threads.append(t)

    time.sleep(duration)
    stop.set()
    for t in threads:
        t.join(timeout=10)
    elapsed = time.time() - t_start

    all_lat = []
    total_err = 0
    for r in results:
        all_lat.extend(r["latencies"])
        total_err += r["errors"]

    n      = len(all_lat)
    tput   = n / elapsed
    median = statistics.median(all_lat) * 1000 if all_lat else 0
    p95    = sorted(all_lat)[int(0.95 * n)] * 1000 if all_lat else 0

    return {
        "workers":   num_workers,
        "mode":      mode,
        "requests":  n,
        "errors":    total_err,
        "throughput_rps": round(tput, 2),
        "latency_median_ms": round(median, 2),
        "latency_p95_ms":    round(p95, 2),
    }


def main():
    ap = argparse.ArgumentParser(description="Parking RPC load tester")
    ap.add_argument("--host",     default="localhost")
    ap.add_argument("--port",     type=int, default=9000)
    ap.add_argument("--workers",  type=int, nargs="+", default=[1, 4, 8, 16])
    ap.add_argument("--duration", type=float, default=30)
    ap.add_argument("--mode",     choices=["avail", "reserve", "mixed"],
                    default="mixed")
    ap.add_argument("--csv",      default="results.csv")
    args = ap.parse_args()

    print(f"\n{'Workers':>8}  {'Mode':>8}  {'Req':>7}  {'Err':>5}  "
          f"{'RPS':>8}  {'Med(ms)':>9}  {'P95(ms)':>9}")
    print("-" * 70)

    rows = []
    for w in args.workers:
        row = run_scenario(args.host, args.port, w, args.duration, args.mode)
        rows.append(row)
        print(f"{row['workers']:>8}  {row['mode']:>8}  {row['requests']:>7}  "
              f"{row['errors']:>5}  {row['throughput_rps']:>8}  "
              f"{row['latency_median_ms']:>9}  {row['latency_p95_ms']:>9}")

    # Save CSV
    with open(args.csv, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
    print(f"\nResults saved to {args.csv}")


if __name__ == "__main__":
    main()
