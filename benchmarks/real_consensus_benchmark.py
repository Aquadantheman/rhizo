"""
Real Consensus Benchmark: Measuring Rhizo Against Actual Coordination Systems

This benchmark compares Rhizo's coordination-free algebraic operations against
REAL systems that perform actual coordination work. No simulated delays.

Systems measured:
1. Rhizo algebraic (ADD, MAX) - local commit, no coordination
2. SQLite WAL (NORMAL sync) - local durability, no coordination
3. SQLite WAL (FULL sync) - local durability with fsync per commit
4. Localhost 2-Phase Commit (3 nodes) - real coordination over TCP sockets
5. Remote 2-Phase Commit - real coordination over network (cloud mode)
6. Redis (optional) - network + persistence
7. etcd (optional) - Raft consensus

Modes:
  Local (default):
    python benchmarks/real_consensus_benchmark.py

  Remote 2PC (against 2pc_participant_server.py instances):
    python benchmarks/real_consensus_benchmark.py --remote-2pc host1:9000,host2:9000

  Remote Redis:
    python benchmarks/real_consensus_benchmark.py --remote-redis host:6379

  Remote etcd:
    python benchmarks/real_consensus_benchmark.py --remote-etcd host:2379

  All remote:
    python benchmarks/real_consensus_benchmark.py \\
      --remote-2pc us.example.com:9000,eu.example.com:9000 \\
      --remote-redis us.example.com:6379 \\
      --remote-etcd us.example.com:2379
"""

import argparse
import json
import multiprocessing
import os
import socket
import struct
import sys
import sqlite3
import statistics
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Callable, Tuple

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


def parse_host_port(s: str, default_port: int) -> Tuple[str, int]:
    """Parse 'host:port' string, using default_port if port omitted."""
    if ":" in s:
        host, port_str = s.rsplit(":", 1)
        return host, int(port_str)
    return s, default_port


def measure_rtt(host: str, port: int, samples: int = 10) -> Optional[float]:
    """Measure TCP round-trip time to a host:port in milliseconds."""
    latencies = []
    for _ in range(samples):
        try:
            start = time.perf_counter()
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(5.0)
            s.connect((host, port))
            elapsed = (time.perf_counter() - start) * 1000
            s.close()
            latencies.append(elapsed)
        except (socket.timeout, ConnectionRefusedError, OSError):
            pass
    if latencies:
        return statistics.median(latencies)
    return None


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
# Two-Phase Commit Protocol (shared between localhost and remote)
# =============================================================================

MSG_PREPARE = b"PREP"
MSG_VOTE_COMMIT = b"VOTE"
MSG_COMMIT = b"COMT"
MSG_ACK = b"ACKK"
MSG_SHUTDOWN = b"SHUT"
MSG_PING = b"PING"
MSG_PONG = b"PONG"


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
            counter += 1
            _send_msg(conn, MSG_VOTE_COMMIT)
        elif msg == MSG_COMMIT:
            _send_msg(conn, MSG_ACK)

    conn.close()
    server.close()


def _do_2pc_round(conns: List[socket.socket]) -> None:
    """Execute one full 2PC round across all participant connections."""
    # Phase 1: PREPARE
    for conn in conns:
        _send_msg(conn, MSG_PREPARE)
    for conn in conns:
        vote = _recv_msg(conn)
        assert vote == MSG_VOTE_COMMIT

    # Phase 2: COMMIT
    for conn in conns:
        _send_msg(conn, MSG_COMMIT)
    for conn in conns:
        ack = _recv_msg(conn)
        assert ack == MSG_ACK


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


# =============================================================================
# Localhost Two-Phase Commit
# =============================================================================

def benchmark_localhost_2pc(iterations: int = 500) -> Optional[BenchmarkResult]:
    """
    Benchmark a real 2-Phase Commit protocol over localhost TCP.

    Spawns 2 participant processes, coordinator runs in the main process.
    Each iteration performs a full 2PC round (PREPARE + COMMIT phases).
    """
    num_participants = 2
    ports = _find_free_ports(num_participants)

    processes = []
    for port in ports:
        p = multiprocessing.Process(target=_participant_process, args=(port,))
        p.daemon = True
        p.start()
        processes.append(p)

    time.sleep(0.3)

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

    # Warmup
    for _ in range(50):
        _do_2pc_round(conns)

    # Measure
    latencies = []
    for _ in range(iterations):
        start = time.perf_counter()
        _do_2pc_round(conns)
        latencies.append((time.perf_counter() - start) * 1000)

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
# Remote Two-Phase Commit (Cloud Mode)
# =============================================================================

def benchmark_remote_2pc(
    endpoints: List[Tuple[str, int]], iterations: int = 200,
) -> Optional[BenchmarkResult]:
    """
    Benchmark 2PC against remote participant servers.

    Connects to 2pc_participant_server.py instances running on remote hosts.
    Measures real network + protocol overhead for geo-distributed coordination.

    Args:
        endpoints: List of (host, port) tuples for participant servers
        iterations: Number of 2PC rounds to measure
    """
    endpoint_strs = [f"{h}:{p}" for h, p in endpoints]
    print(f"  Connecting to remote participants: {', '.join(endpoint_strs)}")

    # Measure RTT to each endpoint
    rtts = {}
    for host, port in endpoints:
        rtt = measure_rtt(host, port)
        if rtt is not None:
            rtts[f"{host}:{port}"] = round(rtt, 2)
            print(f"    RTT to {host}:{port}: {rtt:.2f} ms")
        else:
            print(f"    RTT to {host}:{port}: FAILED")
            return None

    # Connect to all participants
    conns = []
    try:
        for host, port in endpoints:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            s.settimeout(10.0)
            s.connect((host, port))
            conns.append(s)
    except (ConnectionRefusedError, socket.timeout, OSError) as e:
        print(f"  Failed to connect: {e}")
        for c in conns:
            c.close()
        return None

    # Verify connectivity with PING
    for i, conn in enumerate(conns):
        try:
            _send_msg(conn, MSG_PING)
            pong = _recv_msg(conn)
            if pong != MSG_PONG:
                print(f"  Participant {endpoint_strs[i]} did not respond to PING")
                for c in conns:
                    c.close()
                return None
        except (socket.timeout, OSError) as e:
            print(f"  PING failed to {endpoint_strs[i]}: {e}")
            for c in conns:
                c.close()
            return None

    print(f"  All {len(conns)} participants connected and responding")

    # Warmup
    for _ in range(20):
        _do_2pc_round(conns)

    # Measure
    latencies = []
    for _ in range(iterations):
        start = time.perf_counter()
        _do_2pc_round(conns)
        latencies.append((time.perf_counter() - start) * 1000)

    # Shutdown
    for conn in conns:
        try:
            _send_msg(conn, MSG_SHUTDOWN)
        except OSError:
            pass
        conn.close()

    region_str = " + ".join(endpoint_strs)
    system_name = f"Remote 2PC ({len(endpoints)+1} nodes: coordinator + {region_str})"

    result = BenchmarkResult(
        system=system_name,
        operation="2-phase commit over network",
        iterations=iterations,
        latencies_ms=latencies,
    )
    # Attach RTT info for JSON output
    result._rtts = rtts  # type: ignore[attr-defined]
    return result


# =============================================================================
# Redis (Optional — Network + Persistence)
# =============================================================================

def benchmark_redis(
    host: str = "localhost", port: int = 6379, iterations: int = 1000,
) -> Optional[BenchmarkResult]:
    """Benchmark Redis single-node."""
    try:
        import redis
        r = redis.Redis(host=host, port=port, decode_responses=True)
        r.ping()
    except Exception as e:
        print(f"  Redis not available at {host}:{port}: {e}")
        return None

    location = "local" if host in ("localhost", "127.0.0.1") else host

    def do_redis_incr():
        r.incr("rhizo_benchmark_counter")

    result = benchmark_operation(
        f"Redis ({location})", "INCR counter", do_redis_incr, iterations,
    )
    r.delete("rhizo_benchmark_counter")
    r.close()
    return result


def benchmark_redis_with_sync(
    host: str = "localhost", port: int = 6379, iterations: int = 100,
) -> Optional[BenchmarkResult]:
    """Benchmark Redis with synchronous replication (WAIT command)."""
    try:
        import redis
        r = redis.Redis(host=host, port=port, decode_responses=True)
        r.ping()
        info = r.info("replication")
        replicas = info.get("connected_slaves", 0)
        if replicas == 0:
            print(f"  Redis at {host}:{port} has no replicas (single node mode)")
            return None
        print(f"  Redis at {host}:{port} has {replicas} replica(s)")
    except Exception as e:
        print(f"  Redis not available at {host}:{port}: {e}")
        return None

    location = "local" if host in ("localhost", "127.0.0.1") else host

    def do_redis_incr_sync():
        r.incr("rhizo_benchmark_counter")
        r.execute_command("WAIT", replicas, 0)

    result = benchmark_operation(
        f"Redis replicated ({location})", "INCR + WAIT", do_redis_incr_sync, iterations,
    )
    r.delete("rhizo_benchmark_counter")
    r.close()
    return result


# =============================================================================
# etcd (Optional — Raft Consensus)
# =============================================================================

def benchmark_etcd(
    host: str = "localhost", port: int = 2379, iterations: int = 100,
) -> Optional[BenchmarkResult]:
    """Benchmark etcd (Raft consensus)."""
    try:
        import etcd3
        client = etcd3.client(host=host, port=port)
        client.status()
    except Exception as e:
        print(f"  etcd not available at {host}:{port}: {e}")
        return None

    location = "local" if host in ("localhost", "127.0.0.1") else host
    counter = [0]

    def do_etcd_put():
        counter[0] += 1
        client.put("/rhizo_benchmark_counter", str(counter[0]))

    result = benchmark_operation(
        f"etcd Raft ({location})", "PUT counter", do_etcd_put, iterations,
    )
    client.delete("/rhizo_benchmark_counter")
    client.close()
    return result


# =============================================================================
# Output Helpers
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


# =============================================================================
# Main
# =============================================================================

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Benchmark Rhizo against real coordination systems",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  # Localhost only (default)
  python benchmarks/real_consensus_benchmark.py

  # Remote 2PC against participant servers
  python benchmarks/real_consensus_benchmark.py \\
    --remote-2pc us-east.example.com:9000,eu-west.example.com:9000

  # Remote Redis
  python benchmarks/real_consensus_benchmark.py --remote-redis redis.example.com:6379

  # Remote etcd
  python benchmarks/real_consensus_benchmark.py --remote-etcd etcd.example.com:2379

  # All remote systems
  python benchmarks/real_consensus_benchmark.py \\
    --remote-2pc host1:9000,host2:9000 \\
    --remote-redis host1:6379 \\
    --remote-etcd host1:2379
""",
    )
    parser.add_argument(
        "--remote-2pc",
        help="Comma-separated host:port list for remote 2PC participant servers",
    )
    parser.add_argument(
        "--remote-redis",
        help="host:port for remote Redis instance",
    )
    parser.add_argument(
        "--remote-etcd",
        help="host:port for remote etcd instance",
    )
    parser.add_argument(
        "--iterations", type=int, default=None,
        help="Override default iteration count for all benchmarks",
    )
    parser.add_argument(
        "--skip-local", action="store_true",
        help="Skip localhost benchmarks (SQLite, localhost 2PC)",
    )
    parser.add_argument(
        "--output", type=str, default=None,
        help="Output JSON file path (default: benchmarks/REAL_CONSENSUS_BENCHMARK_RESULTS.json)",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    has_remote = args.remote_2pc or args.remote_redis or args.remote_etcd
    mode = "CLOUD" if has_remote else "LOCAL"

    print("=" * 70)
    print(f"REAL CONSENSUS BENCHMARK ({mode} MODE)")
    print("All systems measured. No simulated delays.")
    print("=" * 70)
    print()

    results = []
    speedups = {}

    # --- Rhizo (always runs) ---
    print("1. RHIZO ALGEBRAIC OPERATIONS (Coordination-Free)")
    print("-" * 50)
    iters_rhizo = args.iterations or 10000
    rhizo_add = benchmark_rhizo_algebraic(iterations=iters_rhizo)
    print_result(rhizo_add)
    results.append(rhizo_add)

    rhizo_max = benchmark_rhizo_max(iterations=iters_rhizo)
    print_result(rhizo_max)
    results.append(rhizo_max)
    print()

    # --- SQLite (local only) ---
    if not args.skip_local:
        print("2. SQLITE (Local Durability Baselines)")
        print("-" * 50)

        iters_sqlite = args.iterations or 1000
        sqlite_normal = benchmark_sqlite_wal(iterations=iters_sqlite)
        print_result(sqlite_normal)
        results.append(sqlite_normal)
        print_comparison(rhizo_add, sqlite_normal)
        speedups["SQLite WAL (NORMAL)"] = sqlite_normal.mean_ms / rhizo_add.mean_ms
        print()

        sqlite_full = benchmark_sqlite_full_sync(iterations=iters_sqlite)
        print_result(sqlite_full)
        results.append(sqlite_full)
        print_comparison(rhizo_add, sqlite_full)
        speedups["SQLite WAL (FULL sync)"] = sqlite_full.mean_ms / rhizo_add.mean_ms
        print()

    # --- Localhost 2PC (local only) ---
    tpc_result = None
    if not args.skip_local:
        print("3. LOCALHOST 2-PHASE COMMIT (Real Coordination Protocol)")
        print("-" * 50)
        print("  Spawning 2 participant processes + coordinator...")
        iters_2pc = args.iterations or 500
        tpc_result = benchmark_localhost_2pc(iterations=iters_2pc)
        if tpc_result:
            print_result(tpc_result)
            results.append(tpc_result)
            print_comparison(rhizo_add, tpc_result)
            speedups["Localhost 2PC (3 nodes)"] = tpc_result.mean_ms / rhizo_add.mean_ms
        else:
            print("  2PC benchmark failed")
        print()

    # --- Remote 2PC (cloud mode) ---
    if args.remote_2pc:
        print("4. REMOTE 2-PHASE COMMIT (Geo-Distributed Coordination)")
        print("-" * 50)
        endpoints = [
            parse_host_port(ep.strip(), 9000) for ep in args.remote_2pc.split(",")
        ]
        iters_remote = args.iterations or 200
        remote_2pc = benchmark_remote_2pc(endpoints, iterations=iters_remote)
        if remote_2pc:
            print_result(remote_2pc)
            results.append(remote_2pc)
            print_comparison(rhizo_add, remote_2pc)
            speedups[remote_2pc.system] = remote_2pc.mean_ms / rhizo_add.mean_ms
        else:
            print("  Remote 2PC benchmark failed")
        print()

    # --- Redis ---
    step = 5 if args.remote_2pc else 4
    redis_host, redis_port = "localhost", 6379
    if args.remote_redis:
        redis_host, redis_port = parse_host_port(args.remote_redis, 6379)

    print(f"{step}. REDIS (Optional — {'Remote' if args.remote_redis else 'Local'})")
    print("-" * 50)
    iters_redis = args.iterations or 1000
    redis_result = benchmark_redis(redis_host, redis_port, iterations=iters_redis)
    if redis_result:
        print_result(redis_result)
        results.append(redis_result)
        print_comparison(rhizo_add, redis_result)
        speedups[redis_result.system] = redis_result.mean_ms / rhizo_add.mean_ms

    redis_sync = benchmark_redis_with_sync(redis_host, redis_port, iterations=args.iterations or 100)
    if redis_sync:
        print_result(redis_sync)
        results.append(redis_sync)
        print_comparison(rhizo_add, redis_sync)
        speedups[redis_sync.system] = redis_sync.mean_ms / rhizo_add.mean_ms
    print()

    # --- etcd ---
    step += 1
    etcd_host, etcd_port = "localhost", 2379
    if args.remote_etcd:
        etcd_host, etcd_port = parse_host_port(args.remote_etcd, 2379)

    print(f"{step}. ETCD (Optional — {'Remote' if args.remote_etcd else 'Local'} Raft Consensus)")
    print("-" * 50)
    etcd_result = benchmark_etcd(etcd_host, etcd_port, iterations=args.iterations or 100)
    if etcd_result:
        print_result(etcd_result)
        results.append(etcd_result)
        print_comparison(rhizo_add, etcd_result)
        speedups[etcd_result.system] = etcd_result.mean_ms / rhizo_add.mean_ms
    print()

    # --- Summary ---
    print("=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print()
    print("ALL MEASURED SPEEDUPS (no simulated delays):")
    print("-" * 50)
    for system, speedup in sorted(speedups.items(), key=lambda x: x[1]):
        print(f"  vs {system}: {speedup:.1f}x faster")

    print()
    print("WHAT THIS TELLS US:")
    print(f"  Rhizo algebraic commit:       {rhizo_add.mean_ms:.4f} ms (measured)")
    if not args.skip_local:
        print(f"  Local durability (SQLite):     {sqlite_normal.mean_ms:.4f} ms (measured)")
        print(f"  Local durability + fsync:      {sqlite_full.mean_ms:.4f} ms (measured)")
    if tpc_result:
        print(f"  Localhost coordination (2PC):  {tpc_result.mean_ms:.4f} ms (measured)")

    if not args.skip_local and tpc_result:
        print()
        print("The coordination overhead on LOCALHOST is real and measurable.")
        print("Cross-region adds 50-150ms of network latency on top of this.")
        print()
        print(f"PROJECTED cross-region speedups (adding typical RTT to measured 2PC):")
        print(f"  vs 50ms RTT + 2PC overhead:  {(50 + tpc_result.mean_ms) / rhizo_add.mean_ms:,.0f}x")
        print(f"  vs 100ms RTT + 2PC overhead: {(100 + tpc_result.mean_ms) / rhizo_add.mean_ms:,.0f}x")
        print(f"  vs 150ms RTT + 2PC overhead: {(150 + tpc_result.mean_ms) / rhizo_add.mean_ms:,.0f}x")

    # --- Save results ---
    output = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "mode": mode.lower(),
        "methodology": (
            "All measurements taken on real systems. Rhizo values are "
            "coordination-free local commits. SQLite values are local disk "
            "durability. 2PC values are real TCP coordination. "
            "Remote benchmarks measure actual network + protocol overhead."
        ),
        "results": [r.summary_dict() for r in results],
        "measured_speedups": {k: round(v, 1) for k, v in speedups.items()},
    }

    # Include RTT data for remote benchmarks
    for r in results:
        if hasattr(r, "_rtts"):
            output["endpoint_rtts_ms"] = r._rtts

    output_path = args.output or str(
        Path(__file__).parent / "REAL_CONSENSUS_BENCHMARK_RESULTS.json"
    )
    with open(output_path, "w") as f:
        json.dump(output, f, indent=2)
    print()
    print(f"Results saved to {output_path}")


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
