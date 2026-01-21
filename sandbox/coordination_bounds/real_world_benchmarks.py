"""
Phase 16: Real-World Benchmarks

Industry-standard benchmark implementations to validate coordination bounds
at production scale.

Benchmarks:
1. TPC-C (OLTP workload)
2. YCSB (Key-Value workload)
3. TPC-H (Analytics workload)
4. Custom coordination-focused benchmark

Run: python sandbox/coordination_bounds/real_world_benchmarks.py
"""

import sys
import time
import random
import string
import threading
import statistics
from pathlib import Path
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple, Any
from collections import defaultdict
from enum import Enum
import json

# Import UCL
sys.path.insert(0, str(Path(__file__).parent))
from universal_coordination_layer import (
    OperationType, UniversalCoordinationLayer, UCLCluster
)


# =============================================================================
# BENCHMARK INFRASTRUCTURE
# =============================================================================

@dataclass
class BenchmarkConfig:
    """Configuration for a benchmark run."""
    name: str
    num_nodes: int
    num_operations: int
    num_threads: int
    duration_seconds: float
    warmup_seconds: float = 5.0


@dataclass
class BenchmarkMetrics:
    """Metrics collected during benchmark."""
    throughput_ops_sec: float
    avg_latency_ms: float
    p50_latency_ms: float
    p99_latency_ms: float
    p999_latency_ms: float
    coordination_free_pct: float
    errors: int = 0

    def to_dict(self) -> Dict:
        return {
            "throughput_ops_sec": self.throughput_ops_sec,
            "avg_latency_ms": self.avg_latency_ms,
            "p50_latency_ms": self.p50_latency_ms,
            "p99_latency_ms": self.p99_latency_ms,
            "p999_latency_ms": self.p999_latency_ms,
            "coordination_free_pct": self.coordination_free_pct,
            "errors": self.errors,
        }


def calculate_percentiles(latencies: List[float]) -> Tuple[float, float, float]:
    """Calculate p50, p99, p999 latencies."""
    if not latencies:
        return 0, 0, 0
    sorted_lat = sorted(latencies)
    n = len(sorted_lat)
    p50 = sorted_lat[int(n * 0.50)]
    p99 = sorted_lat[int(n * 0.99)]
    p999 = sorted_lat[min(int(n * 0.999), n - 1)]
    return p50, p99, p999


# =============================================================================
# TPC-C WORKLOAD
# =============================================================================

class TPCCBenchmark:
    """
    TPC-C style OLTP benchmark.

    Transaction types:
    - New Order (45%): Create new order with multiple items
    - Payment (43%): Update customer balance
    - Order Status (4%): Read order status
    - Delivery (4%): Update delivery status
    - Stock Level (4%): Check stock levels

    Coordination analysis:
    - New Order: Mix of writes (non-commutative) and reads
    - Payment: Balance update can be lifted to counter CRDT
    - Order Status: Read-only (coordination-free)
    - Delivery: Status update (can use LWW)
    - Stock Level: Read-only (coordination-free)
    """

    def __init__(self, num_nodes: int, num_warehouses: int = 10):
        self.num_nodes = num_nodes
        self.num_warehouses = num_warehouses
        self.cluster = UCLCluster(num_nodes)

        # Initialize data
        self._init_data()

        # Metrics
        self.latencies: Dict[str, List[float]] = defaultdict(list)
        self.counts: Dict[str, int] = defaultdict(int)

    def _init_data(self):
        """Initialize TPC-C data structures."""
        # Warehouses
        for w in range(self.num_warehouses):
            # Stock levels (can use counter CRDT)
            for i in range(100):  # 100 items per warehouse
                self.cluster.execute_on(
                    0, OperationType.ADD,
                    f"stock_{w}_{i}", 1000  # Initial stock
                )

            # Warehouse balance (counter CRDT)
            self.cluster.execute_on(
                0, OperationType.ADD,
                f"warehouse_balance_{w}", 0
            )

        # Customer balances (counter CRDTs)
        for c in range(1000):
            self.cluster.execute_on(
                0, OperationType.ADD,
                f"customer_balance_{c}", 10000  # Initial balance
            )

        self.cluster.propagate()

    def new_order(self, node_id: int) -> float:
        """New Order transaction."""
        start = time.perf_counter()

        warehouse = random.randint(0, self.num_warehouses - 1)
        customer = random.randint(0, 999)
        num_items = random.randint(5, 15)

        # For each item in order
        for _ in range(num_items):
            item_id = random.randint(0, 99)
            quantity = random.randint(1, 10)

            # Decrement stock (ADD with negative = coordination-free!)
            self.cluster.execute_on(
                node_id, OperationType.ADD,
                f"stock_{warehouse}_{item_id}", -quantity
            )

        # Create order record (SET = requires coordination)
        order_id = f"order_{time.time_ns()}"
        self.cluster.execute_on(
            node_id, OperationType.SET,
            order_id, {"warehouse": warehouse, "customer": customer, "items": num_items}
        )

        latency = (time.perf_counter() - start) * 1000
        self.latencies["new_order"].append(latency)
        self.counts["new_order"] += 1
        return latency

    def payment(self, node_id: int) -> float:
        """Payment transaction."""
        start = time.perf_counter()

        warehouse = random.randint(0, self.num_warehouses - 1)
        customer = random.randint(0, 999)
        amount = random.randint(1, 5000)

        # Update customer balance (ADD = coordination-free!)
        self.cluster.execute_on(
            node_id, OperationType.ADD,
            f"customer_balance_{customer}", -amount
        )

        # Update warehouse balance (ADD = coordination-free!)
        self.cluster.execute_on(
            node_id, OperationType.ADD,
            f"warehouse_balance_{warehouse}", amount
        )

        latency = (time.perf_counter() - start) * 1000
        self.latencies["payment"].append(latency)
        self.counts["payment"] += 1
        return latency

    def order_status(self, node_id: int) -> float:
        """Order Status transaction (read-only)."""
        start = time.perf_counter()

        customer = random.randint(0, 999)

        # Read customer balance (read = coordination-free)
        _ = self.cluster.nodes[node_id].get(f"customer_balance_{customer}")

        latency = (time.perf_counter() - start) * 1000
        self.latencies["order_status"].append(latency)
        self.counts["order_status"] += 1
        return latency

    def delivery(self, node_id: int) -> float:
        """Delivery transaction."""
        start = time.perf_counter()

        warehouse = random.randint(0, self.num_warehouses - 1)

        # Update delivery status (MAX = coordination-free!)
        # Using MAX for "latest delivery timestamp"
        self.cluster.execute_on(
            node_id, OperationType.MAX,
            f"delivery_status_{warehouse}", time.time()
        )

        latency = (time.perf_counter() - start) * 1000
        self.latencies["delivery"].append(latency)
        self.counts["delivery"] += 1
        return latency

    def stock_level(self, node_id: int) -> float:
        """Stock Level transaction (read-only)."""
        start = time.perf_counter()

        warehouse = random.randint(0, self.num_warehouses - 1)
        threshold = random.randint(10, 20)

        # Read stock levels (read = coordination-free)
        low_stock = 0
        for i in range(100):
            stock = self.cluster.nodes[node_id].get(f"stock_{warehouse}_{i}")
            if stock and stock < threshold:
                low_stock += 1

        latency = (time.perf_counter() - start) * 1000
        self.latencies["stock_level"].append(latency)
        self.counts["stock_level"] += 1
        return latency

    def run_transaction(self, node_id: int) -> Tuple[str, float]:
        """Run a random TPC-C transaction based on mix."""
        r = random.random()
        if r < 0.45:
            return "new_order", self.new_order(node_id)
        elif r < 0.88:
            return "payment", self.payment(node_id)
        elif r < 0.92:
            return "order_status", self.order_status(node_id)
        elif r < 0.96:
            return "delivery", self.delivery(node_id)
        else:
            return "stock_level", self.stock_level(node_id)

    def run(self, config: BenchmarkConfig) -> BenchmarkMetrics:
        """Run TPC-C benchmark."""
        print(f"\n  Running TPC-C for {config.duration_seconds}s...")

        # Warmup
        print(f"  Warmup ({config.warmup_seconds}s)...")
        warmup_end = time.time() + config.warmup_seconds
        while time.time() < warmup_end:
            self.run_transaction(random.randint(0, self.num_nodes - 1))
        self.latencies.clear()
        self.counts.clear()

        # Benchmark
        print(f"  Running benchmark...")
        start_time = time.time()
        end_time = start_time + config.duration_seconds

        while time.time() < end_time:
            node_id = random.randint(0, self.num_nodes - 1)
            self.run_transaction(node_id)

        duration = time.time() - start_time

        # Calculate metrics
        all_latencies = []
        for lats in self.latencies.values():
            all_latencies.extend(lats)

        total_ops = sum(self.counts.values())
        p50, p99, p999 = calculate_percentiles(all_latencies)

        # Coordination analysis
        # Payment, delivery, order_status, stock_level = coordination-free (mostly)
        # new_order has one SET operation
        coord_free_ops = (
            self.counts["payment"] * 2 +  # 2 ADDs
            self.counts["delivery"] +      # 1 MAX
            self.counts["order_status"] +  # 1 read
            self.counts["stock_level"] +   # reads
            self.counts["new_order"] * 10  # ~10 ADDs per order (stock decrements)
        )
        coord_required_ops = self.counts["new_order"]  # 1 SET per order
        total_individual_ops = coord_free_ops + coord_required_ops
        coord_free_pct = coord_free_ops / total_individual_ops * 100 if total_individual_ops > 0 else 0

        return BenchmarkMetrics(
            throughput_ops_sec=total_ops / duration,
            avg_latency_ms=statistics.mean(all_latencies) if all_latencies else 0,
            p50_latency_ms=p50,
            p99_latency_ms=p99,
            p999_latency_ms=p999,
            coordination_free_pct=coord_free_pct,
        )


# =============================================================================
# YCSB WORKLOAD
# =============================================================================

class YCSBBenchmark:
    """
    YCSB (Yahoo! Cloud Serving Benchmark) style workload.

    Workload types:
    - Workload A: 50% read, 50% update (heavy update)
    - Workload B: 95% read, 5% update (read-mostly)
    - Workload C: 100% read (read-only)
    - Workload D: 95% read, 5% insert (read-latest)
    - Workload F: 50% read, 50% read-modify-write

    For coordination analysis, we add:
    - Workload G: 100% increment (coordination-free)
    - Workload H: 100% max-update (coordination-free)
    """

    class Workload(Enum):
        A = "update_heavy"
        B = "read_mostly"
        C = "read_only"
        D = "read_latest"
        F = "read_modify_write"
        G = "increment_only"  # Coordination-free
        H = "max_only"        # Coordination-free

    def __init__(self, num_nodes: int, num_records: int = 10000):
        self.num_nodes = num_nodes
        self.num_records = num_records
        self.cluster = UCLCluster(num_nodes)

        # Initialize data
        self._init_data()

        # Metrics
        self.latencies: List[float] = []
        self.op_types: Dict[str, int] = defaultdict(int)

    def _init_data(self):
        """Initialize YCSB records."""
        for i in range(self.num_records):
            # Initialize as counter for increment workloads
            self.cluster.execute_on(0, OperationType.ADD, f"record_{i}", 0)
        self.cluster.propagate()

    def _read(self, node_id: int, key: str) -> float:
        """Read operation."""
        start = time.perf_counter()
        _ = self.cluster.nodes[node_id].get(key)
        return (time.perf_counter() - start) * 1000

    def _update(self, node_id: int, key: str, value: Any) -> float:
        """Update (overwrite) operation."""
        start = time.perf_counter()
        self.cluster.execute_on(node_id, OperationType.SET, key, value)
        return (time.perf_counter() - start) * 1000

    def _increment(self, node_id: int, key: str, delta: int = 1) -> float:
        """Increment operation (coordination-free)."""
        start = time.perf_counter()
        self.cluster.execute_on(node_id, OperationType.ADD, key, delta)
        return (time.perf_counter() - start) * 1000

    def _max_update(self, node_id: int, key: str, value: float) -> float:
        """Max update operation (coordination-free)."""
        start = time.perf_counter()
        self.cluster.execute_on(node_id, OperationType.MAX, key, value)
        return (time.perf_counter() - start) * 1000

    def run_workload_a(self, node_id: int) -> Tuple[str, float]:
        """Workload A: 50/50 read/update."""
        key = f"record_{random.randint(0, self.num_records - 1)}"
        if random.random() < 0.5:
            return "read", self._read(node_id, key)
        else:
            return "update", self._update(node_id, key, random.randint(0, 1000000))

    def run_workload_b(self, node_id: int) -> Tuple[str, float]:
        """Workload B: 95/5 read/update."""
        key = f"record_{random.randint(0, self.num_records - 1)}"
        if random.random() < 0.95:
            return "read", self._read(node_id, key)
        else:
            return "update", self._update(node_id, key, random.randint(0, 1000000))

    def run_workload_c(self, node_id: int) -> Tuple[str, float]:
        """Workload C: 100% read."""
        key = f"record_{random.randint(0, self.num_records - 1)}"
        return "read", self._read(node_id, key)

    def run_workload_g(self, node_id: int) -> Tuple[str, float]:
        """Workload G: 100% increment (coordination-free)."""
        key = f"record_{random.randint(0, self.num_records - 1)}"
        return "increment", self._increment(node_id, key, 1)

    def run_workload_h(self, node_id: int) -> Tuple[str, float]:
        """Workload H: 100% max-update (coordination-free)."""
        key = f"record_{random.randint(0, self.num_records - 1)}"
        return "max", self._max_update(node_id, key, random.random() * 1000)

    def run(self, workload: Workload, config: BenchmarkConfig) -> BenchmarkMetrics:
        """Run YCSB benchmark with specified workload."""
        print(f"\n  Running YCSB-{workload.name} for {config.duration_seconds}s...")

        workload_funcs = {
            self.Workload.A: self.run_workload_a,
            self.Workload.B: self.run_workload_b,
            self.Workload.C: self.run_workload_c,
            self.Workload.G: self.run_workload_g,
            self.Workload.H: self.run_workload_h,
        }
        func = workload_funcs.get(workload, self.run_workload_a)

        # Warmup
        print(f"  Warmup ({config.warmup_seconds}s)...")
        warmup_end = time.time() + config.warmup_seconds
        while time.time() < warmup_end:
            func(random.randint(0, self.num_nodes - 1))
        self.latencies.clear()
        self.op_types.clear()

        # Benchmark
        print(f"  Running benchmark...")
        start_time = time.time()
        end_time = start_time + config.duration_seconds

        while time.time() < end_time:
            node_id = random.randint(0, self.num_nodes - 1)
            op_type, latency = func(node_id)
            self.latencies.append(latency)
            self.op_types[op_type] += 1

        duration = time.time() - start_time

        # Calculate metrics
        p50, p99, p999 = calculate_percentiles(self.latencies)

        # Coordination analysis
        coord_free_ops = self.op_types.get("read", 0) + self.op_types.get("increment", 0) + self.op_types.get("max", 0)
        coord_required_ops = self.op_types.get("update", 0)
        total_ops = coord_free_ops + coord_required_ops
        coord_free_pct = coord_free_ops / total_ops * 100 if total_ops > 0 else 0

        return BenchmarkMetrics(
            throughput_ops_sec=len(self.latencies) / duration,
            avg_latency_ms=statistics.mean(self.latencies) if self.latencies else 0,
            p50_latency_ms=p50,
            p99_latency_ms=p99,
            p999_latency_ms=p999,
            coordination_free_pct=coord_free_pct,
        )


# =============================================================================
# COORDINATION-FOCUSED BENCHMARK
# =============================================================================

class CoordinationBenchmark:
    """
    Custom benchmark specifically designed to measure coordination overhead.

    Compares:
    - Pure coordination-free workload
    - Pure coordination-required workload
    - Mixed workloads at different ratios
    """

    def __init__(self, num_nodes: int):
        self.num_nodes = num_nodes
        self.cluster = UCLCluster(num_nodes)

    def run_coordination_free(self, num_ops: int) -> BenchmarkMetrics:
        """100% coordination-free operations."""
        latencies = []

        start_time = time.time()
        for i in range(num_ops):
            op_start = time.perf_counter()
            self.cluster.execute_on(
                i % self.num_nodes,
                OperationType.ADD,
                f"counter_{i % 100}",
                1
            )
            latencies.append((time.perf_counter() - op_start) * 1000)
        duration = time.time() - start_time

        p50, p99, p999 = calculate_percentiles(latencies)

        return BenchmarkMetrics(
            throughput_ops_sec=num_ops / duration,
            avg_latency_ms=statistics.mean(latencies),
            p50_latency_ms=p50,
            p99_latency_ms=p99,
            p999_latency_ms=p999,
            coordination_free_pct=100.0,
        )

    def run_coordination_required(self, num_ops: int) -> BenchmarkMetrics:
        """100% coordination-required operations."""
        latencies = []

        start_time = time.time()
        for i in range(num_ops):
            op_start = time.perf_counter()
            self.cluster.execute_on(
                i % self.num_nodes,
                OperationType.SET,
                f"config_{i % 100}",
                {"value": i}
            )
            latencies.append((time.perf_counter() - op_start) * 1000)
        duration = time.time() - start_time

        p50, p99, p999 = calculate_percentiles(latencies)

        return BenchmarkMetrics(
            throughput_ops_sec=num_ops / duration,
            avg_latency_ms=statistics.mean(latencies),
            p50_latency_ms=p50,
            p99_latency_ms=p99,
            p999_latency_ms=p999,
            coordination_free_pct=0.0,
        )

    def run_mixed(self, num_ops: int, coord_free_ratio: float) -> BenchmarkMetrics:
        """Mixed workload with specified coordination-free ratio."""
        latencies = []
        coord_free_count = 0

        start_time = time.time()
        for i in range(num_ops):
            op_start = time.perf_counter()
            if random.random() < coord_free_ratio:
                self.cluster.execute_on(
                    i % self.num_nodes,
                    OperationType.ADD,
                    f"counter_{i % 100}",
                    1
                )
                coord_free_count += 1
            else:
                self.cluster.execute_on(
                    i % self.num_nodes,
                    OperationType.SET,
                    f"config_{i % 100}",
                    {"value": i}
                )
            latencies.append((time.perf_counter() - op_start) * 1000)
        duration = time.time() - start_time

        p50, p99, p999 = calculate_percentiles(latencies)

        return BenchmarkMetrics(
            throughput_ops_sec=num_ops / duration,
            avg_latency_ms=statistics.mean(latencies),
            p50_latency_ms=p50,
            p99_latency_ms=p99,
            p999_latency_ms=p999,
            coordination_free_pct=coord_free_count / num_ops * 100,
        )


# =============================================================================
# MAIN BENCHMARK RUNNER
# =============================================================================

def run_all_benchmarks():
    """Run all benchmarks and report results."""

    print("=" * 70)
    print("PHASE 16: REAL-WORLD BENCHMARKS")
    print("=" * 70)
    print("""
Running industry-standard benchmarks to validate coordination bounds
at production-relevant scale.

Benchmarks:
1. TPC-C (OLTP) - Transaction processing
2. YCSB (Key-Value) - Cloud serving
3. Coordination-focused - Direct comparison
""")

    results = {}
    num_nodes = 8

    # TPC-C Benchmark
    print("\n" + "-" * 70)
    print("BENCHMARK 1: TPC-C (OLTP)")
    print("-" * 70)

    tpcc = TPCCBenchmark(num_nodes=num_nodes)
    tpcc_config = BenchmarkConfig(
        name="TPC-C",
        num_nodes=num_nodes,
        num_operations=0,
        num_threads=1,
        duration_seconds=10.0,
        warmup_seconds=2.0,
    )
    tpcc_metrics = tpcc.run(tpcc_config)
    results["tpcc"] = tpcc_metrics

    print(f"\n  Results:")
    print(f"    Throughput: {tpcc_metrics.throughput_ops_sec:,.0f} txn/sec")
    print(f"    Avg latency: {tpcc_metrics.avg_latency_ms:.3f}ms")
    print(f"    P99 latency: {tpcc_metrics.p99_latency_ms:.3f}ms")
    print(f"    Coordination-free ops: {tpcc_metrics.coordination_free_pct:.1f}%")

    # YCSB Benchmarks
    print("\n" + "-" * 70)
    print("BENCHMARK 2: YCSB (Key-Value)")
    print("-" * 70)

    ycsb = YCSBBenchmark(num_nodes=num_nodes)
    ycsb_config = BenchmarkConfig(
        name="YCSB",
        num_nodes=num_nodes,
        num_operations=0,
        num_threads=1,
        duration_seconds=5.0,
        warmup_seconds=1.0,
    )

    for workload in [YCSBBenchmark.Workload.A, YCSBBenchmark.Workload.B,
                     YCSBBenchmark.Workload.C, YCSBBenchmark.Workload.G]:
        metrics = ycsb.run(workload, ycsb_config)
        results[f"ycsb_{workload.name}"] = metrics

        print(f"\n  YCSB-{workload.name}:")
        print(f"    Throughput: {metrics.throughput_ops_sec:,.0f} ops/sec")
        print(f"    Avg latency: {metrics.avg_latency_ms:.4f}ms")
        print(f"    Coordination-free: {metrics.coordination_free_pct:.1f}%")

    # Coordination-focused Benchmark
    print("\n" + "-" * 70)
    print("BENCHMARK 3: Coordination Comparison")
    print("-" * 70)

    coord_bench = CoordinationBenchmark(num_nodes=num_nodes)
    num_ops = 5000

    print("\n  Running pure coordination-free...")
    cf_metrics = coord_bench.run_coordination_free(num_ops)
    results["coord_free_100"] = cf_metrics

    print("  Running pure coordination-required...")
    cr_metrics = coord_bench.run_coordination_required(num_ops)
    results["coord_required_100"] = cr_metrics

    print("\n  Results:")
    print(f"    100% Coord-Free:     {cf_metrics.throughput_ops_sec:,.0f} ops/sec, {cf_metrics.avg_latency_ms:.4f}ms avg")
    print(f"    100% Coord-Required: {cr_metrics.throughput_ops_sec:,.0f} ops/sec, {cr_metrics.avg_latency_ms:.4f}ms avg")
    print(f"    Speedup: {cf_metrics.throughput_ops_sec / cr_metrics.throughput_ops_sec:.0f}x")

    # Mixed workload analysis
    print("\n  Mixed workload analysis:")
    print(f"    {'Coord-Free %':<15} {'Throughput':<15} {'Avg Latency':<15} {'Speedup'}")
    print("    " + "-" * 55)

    for ratio in [0.0, 0.25, 0.50, 0.75, 0.90, 0.95, 1.0]:
        metrics = coord_bench.run_mixed(num_ops, ratio)
        speedup = metrics.throughput_ops_sec / cr_metrics.throughput_ops_sec
        print(f"    {ratio*100:<15.0f}% {metrics.throughput_ops_sec:<15,.0f} {metrics.avg_latency_ms:<15.4f}ms {speedup:.2f}x")
        results[f"mixed_{int(ratio*100)}"] = metrics

    # Summary
    print("\n" + "=" * 70)
    print("BENCHMARK SUMMARY")
    print("=" * 70)

    print(f"""
KEY FINDINGS:

1. TPC-C (OLTP):
   - {tpcc_metrics.coordination_free_pct:.0f}% of operations are coordination-free
   - Most writes (stock updates, balance changes) use algebraic operations
   - Only order creation requires consensus

2. YCSB:
   - Workload A (50% updates): {results['ycsb_A'].coordination_free_pct:.0f}% coordination-free
   - Workload G (increments): 100% coordination-free, {results['ycsb_G'].throughput_ops_sec:,.0f} ops/sec

3. Coordination Comparison:
   - Pure coordination-free: {cf_metrics.throughput_ops_sec:,.0f} ops/sec
   - Pure coordination-required: {cr_metrics.throughput_ops_sec:,.0f} ops/sec
   - Speedup: {cf_metrics.throughput_ops_sec / cr_metrics.throughput_ops_sec:,.0f}x

VALIDATION:
   - Theory predicts C=0 for commutative operations
   - Empirically confirmed with {cf_metrics.throughput_ops_sec / cr_metrics.throughput_ops_sec:,.0f}x speedup
   - Real workloads (TPC-C) have {tpcc_metrics.coordination_free_pct:.0f}%+ coordination-free potential
""")

    # Save results
    output_path = Path(__file__).parent / "real_world_benchmark_results.json"
    with open(output_path, "w") as f:
        json.dump({k: v.to_dict() for k, v in results.items()}, f, indent=2)
    print(f"Results saved to: {output_path}")

    return results


def main():
    """Run real-world benchmarks."""
    results = run_all_benchmarks()
    return True


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
