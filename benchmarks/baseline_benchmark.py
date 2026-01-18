"""
Baseline Benchmark Suite for Armillaria Performance Optimization.

This establishes baseline metrics before implementing optimizations:
1. QueryEngine bypass for direct reads
2. Catalog LRU cache
3. Parallel Parquet encoding
4. Row-group pruning

Run: python benchmarks/baseline_benchmark.py
"""

import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

# Add parent to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

from _rhizo import PyChunkStore, PyCatalog, PyParquetEncoder, PyParquetDecoder
from rhizo import TableWriter, TableReader, QueryEngine, Filter


def generate_test_data(num_rows: int = 100_000) -> pa.Table:
    """Generate test data with various column types."""
    np.random.seed(42)
    return pa.table({
        "id": pa.array(range(num_rows)),
        "value": pa.array(np.random.randn(num_rows)),
        "category": pa.array(np.random.choice(["A", "B", "C", "D"], num_rows)),
        "timestamp": pa.array(np.random.randint(0, 1000000, num_rows)),
        "flag": pa.array(np.random.choice([True, False], num_rows)),
        "amount": pa.array(np.random.uniform(0, 10000, num_rows)),
        "count": pa.array(np.random.randint(0, 1000, num_rows)),
        "name": pa.array([f"item_{i % 1000}" for i in range(num_rows)]),
        "score": pa.array(np.random.uniform(0, 100, num_rows)),
        "status": pa.array(np.random.choice(["active", "inactive", "pending"], num_rows)),
    })


def benchmark(func, warmup: int = 1, iterations: int = 5):
    """Run benchmark with warmup and return median time in ms."""
    # Warmup
    for _ in range(warmup):
        func()

    # Timed runs
    times = []
    for _ in range(iterations):
        start = time.perf_counter()
        func()
        elapsed = (time.perf_counter() - start) * 1000
        times.append(elapsed)

    return {
        "median_ms": round(np.median(times), 2),
        "min_ms": round(min(times), 2),
        "max_ms": round(max(times), 2),
        "std_ms": round(np.std(times), 2),
    }


def run_benchmarks():
    """Run all baseline benchmarks."""
    results = {
        "metadata": {
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "num_rows": 100_000,
            "num_columns": 10,
            "iterations": 5,
        },
        "write": {},
        "read": {},
        "time_travel": {},
        "projection": {},
        "filter": {},
    }

    # Setup temp directory
    temp_dir = tempfile.mkdtemp(prefix="rhizo_bench_")
    chunks_path = os.path.join(temp_dir, "chunks")
    catalog_path = os.path.join(temp_dir, "catalog")

    try:
        store = PyChunkStore(chunks_path)
        catalog = PyCatalog(catalog_path)

        # Generate test data
        print("Generating test data (100k rows, 10 columns)...")
        table = generate_test_data()
        batch = table.to_batches()[0]

        # =================================================================
        # WRITE BENCHMARKS
        # =================================================================
        print("\n--- WRITE BENCHMARKS ---")

        # 1. Native Parquet Encoder (raw encoding, no storage)
        encoder = PyParquetEncoder("zstd")
        print("  Native Parquet Encoder...", end=" ", flush=True)
        results["write"]["native_encoder"] = benchmark(lambda: encoder.encode(batch))
        print(f"{results['write']['native_encoder']['median_ms']:.2f}ms")

        # 2. TableWriter (encoder + storage + catalog)
        writer = TableWriter(store, catalog, chunk_size_rows=100_000)
        print("  TableWriter (full pipeline)...", end=" ", flush=True)
        results["write"]["table_writer"] = benchmark(
            lambda: writer.write("bench_table", table)
        )
        print(f"{results['write']['table_writer']['median_ms']:.2f}ms")

        # 3. QueryEngine write
        engine = QueryEngine(store, catalog)
        print("  QueryEngine write...", end=" ", flush=True)
        results["write"]["query_engine"] = benchmark(
            lambda: engine.write_table("bench_qe", table.to_pandas())
        )
        print(f"{results['write']['query_engine']['median_ms']:.2f}ms")

        # =================================================================
        # READ BENCHMARKS
        # =================================================================
        print("\n--- READ BENCHMARKS ---")

        # Setup: ensure we have data to read
        writer.write("read_bench", table)
        engine.write_table("read_bench_qe", table.to_pandas())

        # Get chunk data for native decoder test
        meta = catalog.get_version("read_bench")
        chunk_data = store.get(meta.chunk_hashes[0])

        # 1. Native Parquet Decoder
        decoder = PyParquetDecoder()
        print("  Native Parquet Decoder...", end=" ", flush=True)
        results["read"]["native_decoder"] = benchmark(lambda: decoder.decode(chunk_data))
        print(f"{results['read']['native_decoder']['median_ms']:.2f}ms")

        # 2. TableReader (decoder + catalog lookup)
        reader = TableReader(store, catalog)
        print("  TableReader...", end=" ", flush=True)
        results["read"]["table_reader"] = benchmark(
            lambda: reader.read_arrow("read_bench")
        )
        print(f"{results['read']['table_reader']['median_ms']:.2f}ms")

        # 3. QueryEngine SELECT *
        print("  QueryEngine SELECT *...", end=" ", flush=True)
        results["read"]["query_engine"] = benchmark(
            lambda: engine.query("SELECT * FROM read_bench_qe")
        )
        print(f"{results['read']['query_engine']['median_ms']:.2f}ms")

        # 4. QueryEngine to pandas
        print("  QueryEngine to pandas...", end=" ", flush=True)
        results["read"]["query_engine_pandas"] = benchmark(
            lambda: engine.query("SELECT * FROM read_bench_qe").to_pandas()
        )
        print(f"{results['read']['query_engine_pandas']['median_ms']:.2f}ms")

        # =================================================================
        # TIME TRAVEL BENCHMARKS
        # =================================================================
        print("\n--- TIME TRAVEL BENCHMARKS ---")

        # Create multiple versions
        for i in range(5):
            writer.write("versioned_table", table)

        # 1. Direct catalog lookup (version 2)
        print("  Catalog lookup (version 2)...", end=" ", flush=True)
        results["time_travel"]["catalog_lookup"] = benchmark(
            lambda: catalog.get_version("versioned_table", 2)
        )
        print(f"{results['time_travel']['catalog_lookup']['median_ms']:.2f}ms")

        # 2. TableReader with version
        print("  TableReader (version 2)...", end=" ", flush=True)
        results["time_travel"]["table_reader"] = benchmark(
            lambda: reader.read_arrow("versioned_table", version=2)
        )
        print(f"{results['time_travel']['table_reader']['median_ms']:.2f}ms")

        # 3. QueryEngine with version (if supported)
        print("  QueryEngine (version 2)...", end=" ", flush=True)
        # Register versioned table
        engine.write_table("ver_qe", table.to_pandas())
        for i in range(4):
            engine.write_table("ver_qe", table.to_pandas())
        results["time_travel"]["query_engine"] = benchmark(
            lambda: engine.query("SELECT * FROM ver_qe", versions={"ver_qe": 2})
        )
        print(f"{results['time_travel']['query_engine']['median_ms']:.2f}ms")

        # =================================================================
        # PROJECTION PUSHDOWN BENCHMARKS
        # =================================================================
        print("\n--- PROJECTION PUSHDOWN BENCHMARKS ---")

        # 1. Full table (10 columns)
        print("  Full table (10 cols)...", end=" ", flush=True)
        results["projection"]["full_10_cols"] = benchmark(
            lambda: reader.read_arrow("read_bench")
        )
        print(f"{results['projection']['full_10_cols']['median_ms']:.2f}ms")

        # 2. Two columns
        print("  2 columns...", end=" ", flush=True)
        results["projection"]["cols_2"] = benchmark(
            lambda: reader.read_arrow("read_bench", columns=["id", "value"])
        )
        print(f"{results['projection']['cols_2']['median_ms']:.2f}ms")

        # 3. One column
        print("  1 column...", end=" ", flush=True)
        results["projection"]["cols_1"] = benchmark(
            lambda: reader.read_arrow("read_bench", columns=["id"])
        )
        print(f"{results['projection']['cols_1']['median_ms']:.2f}ms")

        # =================================================================
        # FILTER PUSHDOWN BENCHMARKS
        # =================================================================
        print("\n--- FILTER PUSHDOWN BENCHMARKS ---")

        # 1. No filter (baseline)
        print("  No filter...", end=" ", flush=True)
        results["filter"]["no_filter"] = benchmark(
            lambda: reader.read_arrow("read_bench")
        )
        print(f"{results['filter']['no_filter']['median_ms']:.2f}ms")

        # 2. 10% selectivity (id < 10000)
        print("  10% selectivity (id < 10000)...", end=" ", flush=True)
        results["filter"]["selectivity_10pct"] = benchmark(
            lambda: reader.read_arrow("read_bench", filters=[Filter("id").lt(10000)])
        )
        print(f"{results['filter']['selectivity_10pct']['median_ms']:.2f}ms")

        # 3. 1% selectivity (id < 1000)
        print("  1% selectivity (id < 1000)...", end=" ", flush=True)
        results["filter"]["selectivity_1pct"] = benchmark(
            lambda: reader.read_arrow("read_bench", filters=[Filter("id").lt(1000)])
        )
        print(f"{results['filter']['selectivity_1pct']['median_ms']:.2f}ms")

        # 4. Combined filter + projection
        print("  1% filter + 2 cols...", end=" ", flush=True)
        results["filter"]["combined_1pct_2cols"] = benchmark(
            lambda: reader.read_arrow(
                "read_bench",
                columns=["id", "value"],
                filters=[Filter("id").lt(1000)]
            )
        )
        print(f"{results['filter']['combined_1pct_2cols']['median_ms']:.2f}ms")

        # =================================================================
        # SUMMARY
        # =================================================================
        print("\n" + "=" * 60)
        print("BASELINE BENCHMARK SUMMARY")
        print("=" * 60)

        print(f"\nWrite Performance:")
        print(f"  Native Encoder:   {results['write']['native_encoder']['median_ms']:>8.2f}ms")
        print(f"  TableWriter:      {results['write']['table_writer']['median_ms']:>8.2f}ms")
        print(f"  QueryEngine:      {results['write']['query_engine']['median_ms']:>8.2f}ms")

        print(f"\nRead Performance:")
        print(f"  Native Decoder:   {results['read']['native_decoder']['median_ms']:>8.2f}ms")
        print(f"  TableReader:      {results['read']['table_reader']['median_ms']:>8.2f}ms")
        print(f"  QueryEngine:      {results['read']['query_engine']['median_ms']:>8.2f}ms")

        print(f"\nTime Travel (version 2):")
        print(f"  Catalog lookup:   {results['time_travel']['catalog_lookup']['median_ms']:>8.2f}ms")
        print(f"  TableReader:      {results['time_travel']['table_reader']['median_ms']:>8.2f}ms")
        print(f"  QueryEngine:      {results['time_travel']['query_engine']['median_ms']:>8.2f}ms")

        print(f"\nProjection Pushdown:")
        print(f"  10 columns:       {results['projection']['full_10_cols']['median_ms']:>8.2f}ms")
        print(f"  2 columns:        {results['projection']['cols_2']['median_ms']:>8.2f}ms")
        print(f"  1 column:         {results['projection']['cols_1']['median_ms']:>8.2f}ms")

        print(f"\nFilter Pushdown:")
        print(f"  No filter:        {results['filter']['no_filter']['median_ms']:>8.2f}ms")
        print(f"  10% selectivity:  {results['filter']['selectivity_10pct']['median_ms']:>8.2f}ms")
        print(f"  1% selectivity:   {results['filter']['selectivity_1pct']['median_ms']:>8.2f}ms")
        print(f"  1% + 2 cols:      {results['filter']['combined_1pct_2cols']['median_ms']:>8.2f}ms")

        # Save results
        output_path = Path(__file__).parent / "BASELINE_RESULTS.json"
        with open(output_path, "w") as f:
            json.dump(results, f, indent=2)
        print(f"\nResults saved to: {output_path}")

        return results

    finally:
        # Cleanup
        shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    run_benchmarks()
