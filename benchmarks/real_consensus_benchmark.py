"""
Real Consensus Benchmark: Measuring Rhizo Against Actual Coordination Systems

This benchmark compares Rhizo's coordination-free algebraic operations against
REAL systems that perform actual coordination work. No simulated delays.

Systems measured:
1. Rhizo algebraic (ADD, MAX) - local commit, no coordination
2. SQLite WAL (NORMAL sync) - local durability, no coordination
3. SQLite WAL (FULL sync) - local durability with fsync per commit
4. Localhost 2-Phase Commit (3 nodes) - real coordination over TCP sockets
5. Redis (optional) - network + persistence
6. etcd (optional) - Raft consensus

The 2PC benchmark is the key addition: it spawns 3 real processes that
coordinate over localhost TCP, measuring the actual protocol overhead of
consensus-style coordination separate from network latency.

Run: python benchmarks/real_consensus_benchmark.py
"""

import json
import multiprocessing
import os
import pickle
import socket
import struct
import sys
import sqlite3
import statistics
import tempfile
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import List, Optional, Callable

# Add rhizo to path
sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

from _rhizo import (
    PyNodeId,
    PyVectorClock,
    PyAlgebraicOperation,
    PyAlgebraicTransaction,
    PyLocalCommitProtocol,
    PyOpType,
    PyAlgebraicValue,
)


@dataclass
class BenchmarkResult:
    system: str
    operation: str
    iterations: int
    latencies_ms: List[float]

    @property
    def mean_ms(self) -> float:
        return statistics.mean(self.latencies_ms)

    @property
    def median_ms(self) -> float:
        return statistics.median(self.latencies_ms)

    @property
    def p99_ms(self) -> float:
        if len(self.latencies_ms) >= 100:
            return statistics.quantiles(self.latencies_ms, n=100)[98]
        return max(self.latencies_ms)

    @property
    def min_ms(self) -> float:
        return min(self.latencies_ms)

    @property
    def max_ms(self) -> float:
        return max(self.latencies_ms)

    def summary_dict(self) -> dict:
        return {
            "system": self.system,
            "operation": self.operation,
            "iterations": self.iterations,
            "mean_ms": round(self.mean_ms, 4),
            "median_ms": round(self.median_ms, 4),
            "p99_ms": round(self.p99_ms, 4),
            "min_ms": round(self.min_ms, 4),
            "max_ms": round(self.max_ms, 4),
        }


def benchmark_operation(
    name: str, operation: str, func: Callable,
    iterations: int = 1000, warmup: int = 100,
) -> BenchmarkResult:
    """Benchmark a single operation with warmup."""
    for _ in range(warmup):
        func()

    latencies = []
    for _ in range(iterations):
        start = time.perf_counter()
        func()
        latencies.append((time.perf_counter() - start) * 1000)

    return BenchmarkResult(
        system=name,
        operation=operation,
        iterations=iterations,
        latencies_ms=latencies,
    )


# =============================================================================
# Rhizo Algebraic Operations (Coordination-Free)
# =============================================================================

def benchmark_rhizo_algebraic(iterations: int = 10000) -> BenchmarkResult:
    """Benchmark Rhizo's coordination-free ADD operation."""
    node_id = PyNodeId("benchmark-node")
    clock = PyVectorClock()

    def do_algebraic_add():
        tx = PyAlgebraicTransaction()
        op = PyAlgebraicOperation("counter", PyOpType("ADD"), PyAlgebraicValue.integer(1))
        tx.add_operation(op)
        PyLocalCommitProtocol.commit_local(tx, node_id, clock)

    return benchmark_operation("Rhizo algebraic", "ADD counter", do_algebraic_add, iterations)


def benchmark_rhizo_max(iterations: int = 10000) -> BenchmarkResult:
    """Benchmark Rhizo's MAX operation (semilattice)."""
    node_id = PyNodeId("benchmark-node")
    clock = PyVectorClock()

    def do_algebraic_max():
        tx = PyAlgebraicTransaction()
        op = PyAlgebraicOperation("high_score", PyOpType("MAX"), PyAlgebraicValue.integer(100))
        tx.add_operation(op)
        PyLocalCommitProtocol.commit_local(tx, node_id, clock)

    return benchmark_operation("Rhizo algebraic", "MAX high_score", do_algebraic_max, iterations)


# =============================================================================
# SQLite Baselines (Local Durability)
# =============================================================================

def benchmark_sqlite_wal(iterations: int = 1000) -> BenchmarkResult:
    """SQLite WAL mode with NORMAL sync (batched fsync)."""
    db_path = tempfile.mktemp(suffix=".db")
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("CREATE TABLE counters (id TEXT PRIMARY KEY, value INTEGER)")
    conn.execute("INSERT INTO counters VALUES ('counter', 0)")

    def do_sqlite_incr():
        conn.execute("UPDATE counters SET value = value + 1 WHERE id = 'counter'")

    result = benchmark_operation("SQLite WAL (NORMAL)", "UPDATE counter", do_sqlite_incr, iterations)
    conn.close()
    os.unlink(db_path)
    return result


def benchmark_sqlite_full_sync(iterations: int = 1000) -> BenchmarkResult:
    """SQLite WAL mode with FULL sync (fsync per commit — strictest durability)."""
    db_path = tempfile.mktemp(suffix=".db")
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("CREATE TABLE counters (id TEXT PRIMARY KEY, value INTEGER)")
    conn.execute("INSERT INTO counters VALUES ('counter', 0)")

    def do_sqlite_incr():
        conn.execute("UPDATE counters SET value = value + 1 WHERE id = 'counter'")

    result = benchmark_operation("SQLite WAL (FULL sync)", "UPDATE counter", do_sqlite_incr, iterations)
    conn.close()
    os.unlink(db_path)
    return result


# =============================================================================
# Localhost Two-Phase Commit (Real Coordination Protocol)
# =============================================================================
#
# This is the key benchmark: it measures the actual overhead of coordinating
# between multiple processes, which is what consensus protocols do.
#
# Architecture:
#   - 1 coordinator process (runs in the benchmark thread)
#   - 2 participant processes (spawned via multiprocessing)
#   - Communication over localhost TCP sockets
#
# Protocol per transaction:
#   Phase 1 (Prepare): Coordinator sends PREPARE to all participants,
#                       waits for VOTE_COMMIT from each
#   Phase 2 (Commit):  Coordinator sends COMMIT to all participants,
#                       waits for ACK from each
#
# This measures real IPC overhead: socket creation, serialization,
# context switches, and protocol round-trips.

MSG_PREPARE = b"PREP"
MSG_VOTE_COMMIT = b"VOTE"
MSG_COMMIT = b"COMT"
MSG_ACK = b"ACKK"
MSG_SHUTDOWN = b"SHUT"


def _send_msg(sock: socket.socket, msg: bytes) -> None:
    """Send a length-prefixed message."""
    sock.sendall(struct.pack("!I", len(msg)) + msg)


def _recv_msg(sock: socket.socket) -> bytes:
    """Receive a length-prefixed message."""
    raw_len = _recv_exact(sock, 4)
    if not raw_len:
        return b""
    msg_len = struct.unpack("!I", raw_len)[0]
    return _recv_exact(sock, msg_len)


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    """Receive exactly n bytes."""
    data = b""
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:
            return b""
        data += chunk
    return data


def _participant_process(port: int) -> None:
    """
    Participant in 2PC protocol. Runs as a separate OS process.

    Listens for PREPARE/COMMIT messages from coordinator,
    responds with VOTE_COMMIT/ACK. Maintains a local counter
    to simulate actual state mutation.
    """
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", port))
    server.listen(1)

    conn, _ = server.accept()
    conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    counter = 0
    while True:
        msg = _recv_msg(conn)
        if not msg or msg == MSG_SHUTDOWN:
            break
        if msg == MSG_PREPARE:
            # Simulate prepare: validate we can commit
            counter += 1  # Tentative state change
            _send_msg(conn, MSG_VOTE_COMMIT)
        elif msg == MSG_COMMIT:
            # Commit is final — state already updated
            _send_msg(conn, MSG_ACK)

    conn.close()
    server.close()


def _find_free_ports(n: int) -> List[int]:
    """Find n free TCP ports."""
    ports = []
    socks = []
    for _ in range(n):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0))
        ports.append(s.getsockname()[1])
        socks.append(s)
    for s in socks:
        s.close()
    return ports


def benchmark_localhost_2pc(iterations: int = 500) -> Optional[BenchmarkResult]:
    """
    Benchmark a real 2-Phase Commit protocol over localhost TCP.

    Spawns 2 participant processes, coordinator runs in the main process.
    Each iteration performs a full 2PC round (PREPARE + COMMIT phases).

    This measures the actual cost of coordination:
    - TCP socket round-trips (localhost)
    - Process context switches
    - Message serialization/deserialization
    - Protocol overhead (2 phases x 2 participants = 4 round-trips)
    """
    num_participants = 2
    ports = _find_free_ports(num_participants)

    # Start participant processes
    processes = []
    for port in ports:
        p = multiprocessing.Process(target=_participant_process, args=(port,))
        p.daemon = True
        p.start()
        processes.append(p)

    # Give participants time to bind
    time.sleep(0.3)

    # Connect coordinator to all participants
    conns = []
    try:
        for port in ports:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            s.connect(("127.0.0.1", port))
            conns.append(s)
    except ConnectionRefusedError:
        print("  2PC benchmark: Failed to connect to participants")
        for p in processes:
            p.terminate()
        return None

    def do_2pc_transaction():
        # Phase 1: PREPARE — send to all, collect votes
        for conn in conns:
            _send_msg(conn, MSG_PREPARE)
        for conn in conns:
            vote = _recv_msg(conn)
            assert vote == MSG_VOTE_COMMIT

        # Phase 2: COMMIT — send to all, collect acks
        for conn in conns:
            _send_msg(conn, MSG_COMMIT)
        for conn in conns:
            ack = _recv_msg(conn)
            assert ack == MSG_ACK

    # Warmup
    for _ in range(50):
        do_2pc_transaction()

    # Measure
    latencies = []
    for _ in range(iterations):
        start = time.perf_counter()
        do_2pc_transaction()
        latencies.append((time.perf_counter() - start) * 1000)

    # Shutdown participants
    for conn in conns:
        try:
            _send_msg(conn, MSG_SHUTDOWN)
        except OSError:
            pass
        conn.close()

    for p in processes:
        p.join(timeout=2)
        if p.is_alive():
            p.terminate()

    return BenchmarkResult(
        system="Localhost 2PC (3 nodes)",
        operation="2-phase commit over TCP",
        iterations=iterations,
        latencies_ms=latencies,
    )


# =============================================================================
# Redis (Optional — Network + Persistence)
# =============================================================================

def benchmark_redis_local(iterations: int = 1000) -> Optional[BenchmarkResult]:
    """Benchmark Redis single-node (no replication)."""
    try:
        import redis
        r = redis.Redis(host="localhost", port=6379, decode_responses=True)
        r.ping()
    except Exception as e:
        print(f"  Redis not available: {e}")
        return None

    def do_redis_incr():
        r.incr("counter")

    result = benchmark_operation("Redis (local)", "INCR counter", do_redis_incr, iterations)
    r.close()
    return result


def benchmark_redis_with_sync(iterations: int = 1000) -> Optional[BenchmarkResult]:
    """Benchmark Redis with synchronous replication (WAIT command)."""
    try:
        import redis
        r = redis.Redis(host="localhost", port=6379, decode_responses=True)
        r.ping()
        info = r.info("replication")
        replicas = info.get("connected_slaves", 0)
        if replicas == 0:
            print(f"  Redis has no replicas (single node mode)")
            return None
    except Exception as e:
        print(f"  Redis not available: {e}")
        return None

    def do_redis_incr_sync():
        r.incr("counter")
        r.wait(1, 0)

    result = benchmark_operation("Redis (replicated)", "INCR + WAIT", do_redis_incr_sync, iterations)
    r.close()
    return result


# =============================================================================
# etcd (Optional — Raft Consensus)
# =============================================================================

def benchmark_etcd(iterations: int = 1000) -> Optional[BenchmarkResult]:
    """Benchmark etcd (Raft consensus)."""
    try:
        import etcd3
        client = etcd3.client(host="localhost", port=2379)
        client.status()
    except Exception as e:
        print(f"  etcd not available: {e}")
        return None

    counter = [0]

    def do_etcd_put():
        counter[0] += 1
        client.put("/counter", str(counter[0]))

    result = benchmark_operation("etcd (Raft)", "PUT counter", do_etcd_put, iterations)
    client.close()
    return result


# =============================================================================
# Main Benchmark Runner
# =============================================================================

def print_result(result: BenchmarkResult) -> None:
    print(f"  {result.system}: {result.operation}")
    print(f"    Mean:   {result.mean_ms:.4f} ms")
    print(f"    Median: {result.median_ms:.4f} ms")
    print(f"    p99:    {result.p99_ms:.4f} ms")
    print(f"    Range:  {result.min_ms:.4f} - {result.max_ms:.4f} ms")


def print_comparison(rhizo: BenchmarkResult, other: BenchmarkResult) -> None:
    speedup = other.mean_ms / rhizo.mean_ms
    print(f"\n  SPEEDUP vs {other.system}:")
    print(f"    {speedup:.1f}x faster (mean)")
    print(f"    Rhizo: {rhizo.mean_ms:.4f} ms vs {other.system}: {other.mean_ms:.4f} ms")


def main():
    print("=" * 70)
    print("REAL CONSENSUS BENCHMARK")
    print("All systems measured on the same machine. No simulated delays.")
    print("=" * 70)
    print()

    results = []
    speedups = {}

    # --- Rhizo ---
    print("1. RHIZO ALGEBRAIC OPERATIONS (Coordination-Free)")
    print("-" * 50)
    rhizo_add = benchmark_rhizo_algebraic(iterations=10000)
    print_result(rhizo_add)
    results.append(rhizo_add)

    rhizo_max = benchmark_rhizo_max(iterations=10000)
    print_result(rhizo_max)
    results.append(rhizo_max)
    print()

    # --- SQLite ---
    print("2. SQLITE (Local Durability Baselines)")
    print("-" * 50)

    sqlite_normal = benchmark_sqlite_wal(iterations=1000)
    print_result(sqlite_normal)
    results.append(sqlite_normal)
    print_comparison(rhizo_add, sqlite_normal)
    speedups["SQLite WAL (NORMAL)"] = sqlite_normal.mean_ms / rhizo_add.mean_ms
    print()

    sqlite_full = benchmark_sqlite_full_sync(iterations=1000)
    print_result(sqlite_full)
    results.append(sqlite_full)
    print_comparison(rhizo_add, sqlite_full)
    speedups["SQLite WAL (FULL sync)"] = sqlite_full.mean_ms / rhizo_add.mean_ms
    print()

    # --- Localhost 2PC ---
    print("3. LOCALHOST 2-PHASE COMMIT (Real Coordination Protocol)")
    print("-" * 50)
    print("  Spawning 2 participant processes + coordinator...")
    tpc_result = benchmark_localhost_2pc(iterations=500)
    if tpc_result:
        print_result(tpc_result)
        results.append(tpc_result)
        print_comparison(rhizo_add, tpc_result)
        speedups["Localhost 2PC (3 nodes)"] = tpc_result.mean_ms / rhizo_add.mean_ms
    else:
        print("  2PC benchmark failed")
    print()

    # --- Redis (optional) ---
    print("4. REDIS (Optional — Network + Persistence)")
    print("-" * 50)
    redis_local = benchmark_redis_local(iterations=1000)
    if redis_local:
        print_result(redis_local)
        results.append(redis_local)
        print_comparison(rhizo_add, redis_local)
        speedups["Redis (local)"] = redis_local.mean_ms / rhizo_add.mean_ms

    redis_sync = benchmark_redis_with_sync(iterations=100)
    if redis_sync:
        print_result(redis_sync)
        results.append(redis_sync)
        print_comparison(rhizo_add, redis_sync)
        speedups["Redis (replicated)"] = redis_sync.mean_ms / rhizo_add.mean_ms
    print()

    # --- etcd (optional) ---
    print("5. ETCD (Optional — Raft Consensus)")
    print("-" * 50)
    etcd_result = benchmark_etcd(iterations=100)
    if etcd_result:
        print_result(etcd_result)
        results.append(etcd_result)
        print_comparison(rhizo_add, etcd_result)
        speedups["etcd (Raft)"] = etcd_result.mean_ms / rhizo_add.mean_ms
    print()

    # --- Summary ---
    print("=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print()
    print("ALL MEASURED SPEEDUPS (same machine, no simulated delays):")
    print("-" * 50)
    for system, speedup in sorted(speedups.items(), key=lambda x: x[1]):
        print(f"  vs {system}: {speedup:.1f}x faster")

    print()
    print("WHAT THIS TELLS US:")
    print(f"  Rhizo algebraic commit:       {rhizo_add.mean_ms:.4f} ms (measured)")
    print(f"  Local durability (SQLite):     {sqlite_normal.mean_ms:.4f} ms (measured)")
    print(f"  Local durability + fsync:      {sqlite_full.mean_ms:.4f} ms (measured)")
    if tpc_result:
        print(f"  Localhost coordination (2PC):  {tpc_result.mean_ms:.4f} ms (measured)")
    print()
    print("The coordination overhead on LOCALHOST is real and measurable.")
    print("Cross-region adds 50-150ms of network latency on top of this.")
    if tpc_result:
        print()
        low = 50 / rhizo_add.mean_ms
        mid = 100 / rhizo_add.mean_ms
        high = 150 / rhizo_add.mean_ms
        print(f"PROJECTED cross-region speedups (adding typical RTT to measured 2PC):")
        print(f"  vs 50ms RTT + 2PC overhead:  {(50 + tpc_result.mean_ms) / rhizo_add.mean_ms:,.0f}x")
        print(f"  vs 100ms RTT + 2PC overhead: {(100 + tpc_result.mean_ms) / rhizo_add.mean_ms:,.0f}x")
        print(f"  vs 150ms RTT + 2PC overhead: {(150 + tpc_result.mean_ms) / rhizo_add.mean_ms:,.0f}x")

    # --- Save results ---
    output = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "methodology": (
            "All measurements taken on the same machine. Rhizo values are "
            "coordination-free local commits. SQLite values are local disk "
            "durability. 2PC values are real TCP coordination between 3 OS "
            "processes on localhost. Cross-region projections add typical "
            "network RTT to measured protocol overhead."
        ),
        "results": [r.summary_dict() for r in results],
        "measured_speedups": {k: round(v, 1) for k, v in speedups.items()},
    }

    output_path = Path(__file__).parent / "REAL_CONSENSUS_BENCHMARK_RESULTS.json"
    with open(output_path, "w") as f:
        json.dump(output, f, indent=2)
    print()
    print(f"Results saved to {output_path}")


if __name__ == "__main__":
    # Required for multiprocessing on Windows
    multiprocessing.freeze_support()
    main()
