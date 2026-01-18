"""
Competitive Benchmark: Armillaria vs Delta Lake vs Raw Parquet

Compares performance across key operations:
1. Write (single version)
2. Read (full table)
3. Time Travel (read old version)
4. Filtered Read (predicate pushdown)
5. Projection (column subset)

Run: python benchmarks/competitive_benchmark.py
"""

import io
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

# Armillaria imports
from armillaria import PyChunkStore, PyCatalog
from armillaria_query import TableWriter, TableReader, Filter

# Delta Lake import
try:
    from deltalake import DeltaTable, write_deltalake
    DELTA_AVAILABLE = True
except ImportError:
    DELTA_AVAILABLE = False
    print("Warning: deltalake not installed, skipping Delta Lake benchmarks")


def generate_test_data(num_rows: int) -> pd.DataFrame:
    """Generate test DataFrame."""
    np.random.seed(42)
    return pd.DataFrame({
        "id": range(num_rows),
        "value": np.random.randn(num_rows),
        "category": np.random.choice(["A", "B", "C", "D"], num_rows),
        "timestamp": np.random.randint(0, 1_000_000, num_rows),
        "amount": np.random.uniform(0, 10000, num_rows),
        "count": np.random.randint(0, 1000, num_rows),
        "name": [f"item_{i % 1000}" for i in range(num_rows)],
        "score": np.random.uniform(0, 100, num_rows),
        "status": np.random.choice(["active", "inactive", "pending"], num_rows),
        "flag": np.random.choice([True, False], num_rows),
    })


def benchmark(func, warmup: int = 1, iterations: int = 5):
    """Run benchmark and return median time in ms."""
    for _ in range(warmup):
        try:
            func()
        except Exception:
            pass

    times = []
    for _ in range(iterations):
        start = time.perf_counter()
        func()
        elapsed = (time.perf_counter() - start) * 1000
        times.append(elapsed)

    return round(np.median(times), 2)


def run_armillaria_benchmarks(df: pd.DataFrame, temp_dir: str) -> dict:
    """Run Armillaria benchmarks."""
    results = {}

    chunks_path = os.path.join(temp_dir, "armillaria_chunks")
    catalog_path = os.path.join(temp_dir, "armillaria_catalog")

    store = PyChunkStore(chunks_path)
    catalog = PyCatalog(catalog_path)
    writer = TableWriter(store, catalog)
    reader = TableReader(store, catalog)

    # Write benchmark
    results["write"] = benchmark(lambda: writer.write("bench_table", df))

    # Create multiple versions for time travel test
    for i in range(3):
        writer.write("bench_table", df)

    # Read benchmark (latest version)
    results["read"] = benchmark(lambda: reader.read_arrow("bench_table"))

    # Time travel benchmark (version 2)
    results["time_travel"] = benchmark(lambda: reader.read_arrow("bench_table", version=2))

    # Filtered read (5% selectivity)
    num_rows = len(df)
    threshold = int(num_rows * 0.05)
    results["filtered_read"] = benchmark(
        lambda: reader.read_arrow("bench_table", filters=[Filter("id").lt(threshold)])
    )

    # Projection (2 columns)
    results["projection"] = benchmark(
        lambda: reader.read_arrow("bench_table", columns=["id", "value"])
    )

    # Combined (filter + projection)
    results["combined"] = benchmark(
        lambda: reader.read_arrow("bench_table", columns=["id", "value"], filters=[Filter("id").lt(threshold)])
    )

    return results


def run_delta_benchmarks(df: pd.DataFrame, temp_dir: str) -> dict:
    """Run Delta Lake benchmarks."""
    if not DELTA_AVAILABLE:
        return {"error": "Delta Lake not available"}

    results = {}
    delta_path = os.path.join(temp_dir, "delta_table")

    # Write benchmark
    def delta_write():
        write_deltalake(delta_path, df, mode="overwrite")
    results["write"] = benchmark(delta_write)

    # Create multiple versions for time travel
    for i in range(3):
        write_deltalake(delta_path, df, mode="overwrite")

    # Read benchmark (latest version)
    def delta_read():
        dt = DeltaTable(delta_path)
        return dt.to_pandas()
    results["read"] = benchmark(delta_read)

    # Time travel benchmark (version 1)
    def delta_time_travel():
        dt = DeltaTable(delta_path, version=1)
        return dt.to_pandas()
    results["time_travel"] = benchmark(delta_time_travel)

    # Filtered read
    num_rows = len(df)
    threshold = int(num_rows * 0.05)
    def delta_filtered():
        dt = DeltaTable(delta_path)
        return dt.to_pandas(filters=[("id", "<", threshold)])
    results["filtered_read"] = benchmark(delta_filtered)

    # Projection
    def delta_projection():
        dt = DeltaTable(delta_path)
        return dt.to_pandas(columns=["id", "value"])
    results["projection"] = benchmark(delta_projection)

    # Combined
    def delta_combined():
        dt = DeltaTable(delta_path)
        return dt.to_pandas(columns=["id", "value"], filters=[("id", "<", threshold)])
    results["combined"] = benchmark(delta_combined)

    return results


def run_parquet_benchmarks(df: pd.DataFrame, temp_dir: str) -> dict:
    """Run raw Parquet benchmarks (no versioning)."""
    results = {}
    parquet_path = os.path.join(temp_dir, "raw.parquet")

    table = pa.Table.from_pandas(df)

    # Write benchmark
    def parquet_write():
        pq.write_table(table, parquet_path, compression="zstd")
    results["write"] = benchmark(parquet_write)

    # Read benchmark
    def parquet_read():
        return pq.read_table(parquet_path)
    results["read"] = benchmark(parquet_read)

    # Time travel - N/A for raw Parquet
    results["time_travel"] = "N/A"

    # Filtered read (post-hoc filtering)
    num_rows = len(df)
    threshold = int(num_rows * 0.05)
    def parquet_filtered():
        t = pq.read_table(parquet_path)
        return t.filter(pa.compute.less(t["id"], threshold))
    results["filtered_read"] = benchmark(parquet_filtered)

    # Projection
    def parquet_projection():
        return pq.read_table(parquet_path, columns=["id", "value"])
    results["projection"] = benchmark(parquet_projection)

    # Combined
    def parquet_combined():
        t = pq.read_table(parquet_path, columns=["id", "value"])
        return t.filter(pa.compute.less(t["id"], threshold))
    results["combined"] = benchmark(parquet_combined)

    return results


def format_result(value):
    """Format result for display."""
    if isinstance(value, str):
        return value
    return f"{value:.2f}ms"


def main():
    print("=" * 75)
    print("COMPETITIVE BENCHMARK: Armillaria vs Delta Lake vs Raw Parquet")
    print("=" * 75)

    num_rows = 100_000
    print(f"\nTest data: {num_rows:,} rows, 10 columns")

    print("\nGenerating test data...")
    df = generate_test_data(num_rows)
    print(f"  DataFrame size: {df.memory_usage(deep=True).sum() / 1024 / 1024:.1f} MB")

    temp_dir = tempfile.mkdtemp(prefix="competitive_bench_")

    try:
        # Run benchmarks
        print("\n" + "-" * 75)
        print("Running Armillaria benchmarks...")
        armillaria_results = run_armillaria_benchmarks(df, temp_dir)

        print("Running Delta Lake benchmarks...")
        delta_results = run_delta_benchmarks(df, temp_dir)

        print("Running Raw Parquet benchmarks...")
        parquet_results = run_parquet_benchmarks(df, temp_dir)

        # Results table
        print("\n" + "=" * 75)
        print("RESULTS (lower is better)")
        print("=" * 75)

        operations = [
            ("write", "Write (single version)"),
            ("read", "Read (full table)"),
            ("time_travel", "Time Travel (old version)"),
            ("filtered_read", "Filtered Read (5% selectivity)"),
            ("projection", "Projection (2 columns)"),
            ("combined", "Combined (filter + projection)"),
        ]

        print(f"\n{'Operation':<35} {'Armillaria':>12} {'Delta Lake':>12} {'Raw Parquet':>12}")
        print("-" * 75)

        for key, name in operations:
            arm = format_result(armillaria_results.get(key, "N/A"))
            delta = format_result(delta_results.get(key, "N/A"))
            parq = format_result(parquet_results.get(key, "N/A"))
            print(f"{name:<35} {arm:>12} {delta:>12} {parq:>12}")

        # Winner analysis
        print("\n" + "=" * 75)
        print("ANALYSIS")
        print("=" * 75)

        print("\nWhere Armillaria excels:")
        for key, name in operations:
            arm = armillaria_results.get(key)
            delta = delta_results.get(key)
            if isinstance(arm, (int, float)) and isinstance(delta, (int, float)):
                if arm < delta:
                    speedup = delta / arm
                    print(f"  - {name}: {speedup:.1f}x faster than Delta Lake")

        print("\nWhere Delta Lake excels:")
        for key, name in operations:
            arm = armillaria_results.get(key)
            delta = delta_results.get(key)
            if isinstance(arm, (int, float)) and isinstance(delta, (int, float)):
                if delta < arm:
                    speedup = arm / delta
                    print(f"  - {name}: {speedup:.1f}x faster than Armillaria")

        # Key differentiators
        print("\n" + "-" * 75)
        print("KEY DIFFERENTIATORS")
        print("-" * 75)
        print("""
Armillaria Advantages:
  - Content-addressable storage (automatic deduplication)
  - Merkle tree integrity verification
  - Native Rust performance with Python bindings
  - Simpler architecture (no Spark dependency)
  - Optimized time travel (O(1) version lookup)

Delta Lake Advantages:
  - Mature ecosystem and wide adoption
  - ACID transactions across multiple tables
  - Schema evolution and enforcement
  - Integration with Spark, Databricks
  - Larger community and tooling
""")

        # Save results
        all_results = {
            "metadata": {
                "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                "num_rows": num_rows,
                "num_columns": 10,
            },
            "armillaria": armillaria_results,
            "delta_lake": delta_results,
            "raw_parquet": parquet_results,
        }

        output_path = Path(__file__).parent / "COMPETITIVE_BENCHMARK_RESULTS.json"
        with open(output_path, "w") as f:
            json.dump(all_results, f, indent=2, default=str)
        print(f"\nResults saved to: {output_path}")

    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
