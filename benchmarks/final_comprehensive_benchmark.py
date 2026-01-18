"""
Final Comprehensive Benchmark - Performance Summary of All Optimizations.

This benchmark summarizes the performance gains from:
1. Phase R.1: Projection pushdown (column selection)
2. Phase R.2: Predicate pushdown (row filtering)
3. Phase R.3: Row-group pruning (skip entire row groups)
4. Phase R.4: Parallel encoding (multi-chunk writes)

Run: python benchmarks/final_comprehensive_benchmark.py
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
import pyarrow as pa
import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

from _rhizo import PyChunkStore, PyCatalog, PyParquetEncoder, PyParquetDecoder, PyPredicateFilter
from rhizo import TableWriter, TableReader, Filter


def benchmark(func, warmup: int = 1, iterations: int = 5):
    """Run benchmark and return median time in ms."""
    for _ in range(warmup):
        func()
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
    }


def generate_test_data(num_rows: int) -> pa.Table:
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


def create_multi_rowgroup_parquet(table: pa.Table, rows_per_group: int = 10000) -> bytes:
    """Create a Parquet file with multiple row groups."""
    buffer = io.BytesIO()
    pq.write_table(
        table,
        buffer,
        row_group_size=rows_per_group,
        compression='zstd',
        write_statistics=True,
    )
    return buffer.getvalue()


def main():
    print("=" * 70)
    print("ARMILLARIA PERFORMANCE BENCHMARK - FINAL COMPREHENSIVE RESULTS")
    print("=" * 70)

    results = {
        "metadata": {
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "phases_tested": ["R.1 Projection", "R.2 Predicate", "R.3 Row-Group Pruning", "R.4 Parallel Encoding"],
        },
        "write": {},
        "read": {},
        "optimization_gains": {},
    }

    temp_dir = tempfile.mkdtemp(prefix="rhizo_final_bench_")
    chunks_path = os.path.join(temp_dir, "chunks")
    catalog_path = os.path.join(temp_dir, "catalog")

    try:
        store = PyChunkStore(chunks_path)
        catalog = PyCatalog(catalog_path)
        encoder = PyParquetEncoder("zstd")
        decoder = PyParquetDecoder()

        # ================================================================
        # PART 1: WRITE BENCHMARKS
        # ================================================================
        print("\n" + "=" * 50)
        print("PART 1: WRITE PERFORMANCE")
        print("=" * 50)

        # Small write (100k rows, single chunk)
        print("\n--- Small Write (100k rows, single chunk) ---")
        small_table = generate_test_data(100_000)
        batch = small_table.combine_chunks().to_batches()[0]

        r = benchmark(lambda: encoder.encode(batch))
        results["write"]["100k_single_chunk"] = r
        print(f"  Native Encoder: {r['median_ms']:.2f}ms")

        # Large write (1M rows, parallel encoding)
        print("\n--- Large Write (1M rows, 10 chunks, parallel) ---")
        large_table = generate_test_data(1_000_000)
        batches_10 = [large_table.slice(i, 100_000).combine_chunks().to_batches()[0]
                      for i in range(0, 1_000_000, 100_000)]

        r_seq = benchmark(lambda: [encoder.encode(b) for b in batches_10])
        results["write"]["1M_sequential"] = r_seq
        print(f"  Sequential:     {r_seq['median_ms']:.2f}ms")

        r_par = benchmark(lambda: encoder.encode_batch(batches_10))
        results["write"]["1M_parallel"] = r_par
        print(f"  Parallel:       {r_par['median_ms']:.2f}ms")

        speedup = r_seq['median_ms'] / r_par['median_ms']
        results["optimization_gains"]["parallel_encoding"] = round(speedup, 2)
        print(f"  Speedup:        {speedup:.2f}x")

        # ================================================================
        # PART 2: READ BENCHMARKS
        # ================================================================
        print("\n" + "=" * 50)
        print("PART 2: READ PERFORMANCE")
        print("=" * 50)

        # Create multi-row-group file for read tests
        test_data = generate_test_data(100_000)
        parquet_data = create_multi_rowgroup_parquet(test_data, rows_per_group=10_000)

        # Baseline full decode
        print("\n--- Baseline: Full Decode (100k rows, 10 row groups) ---")
        r_full = benchmark(lambda: decoder.decode(parquet_data))
        results["read"]["full_decode"] = r_full
        print(f"  Full decode:    {r_full['median_ms']:.2f}ms")

        # Phase R.1: Projection pushdown
        print("\n--- Phase R.1: Projection Pushdown ---")
        r_proj_2 = benchmark(lambda: decoder.decode_columns_by_name(parquet_data, ["id", "value"]))
        results["read"]["projection_2_cols"] = r_proj_2
        print(f"  2 columns:      {r_proj_2['median_ms']:.2f}ms (vs full: {r_full['median_ms']:.2f}ms)")
        speedup = r_full['median_ms'] / r_proj_2['median_ms']
        results["optimization_gains"]["projection_2cols"] = round(speedup, 2)
        print(f"  Speedup:        {speedup:.2f}x")

        r_proj_1 = benchmark(lambda: decoder.decode_columns_by_name(parquet_data, ["id"]))
        results["read"]["projection_1_col"] = r_proj_1
        print(f"  1 column:       {r_proj_1['median_ms']:.2f}ms (vs full: {r_full['median_ms']:.2f}ms)")
        speedup = r_full['median_ms'] / r_proj_1['median_ms']
        results["optimization_gains"]["projection_1col"] = round(speedup, 2)
        print(f"  Speedup:        {speedup:.2f}x")

        # Phase R.2 + R.3: Predicate pushdown with row-group pruning
        print("\n--- Phase R.2 + R.3: Predicate Pushdown + Row-Group Pruning ---")

        # Filter that can prune row groups (id < 5000 - first half of first row group)
        filter_5k = PyPredicateFilter("id", "lt", 5000)
        total, pruned, kept = decoder.get_pruning_stats(parquet_data, [filter_5k])
        print(f"  Filter: id < 5000")
        print(f"  Row groups: {pruned}/{total} pruned ({100*pruned/total:.0f}%), {kept} kept")

        r_filter = benchmark(lambda: decoder.decode_with_filter(parquet_data, [filter_5k]))
        results["read"]["filter_5pct"] = r_filter
        print(f"  Filtered decode: {r_filter['median_ms']:.2f}ms (vs full: {r_full['median_ms']:.2f}ms)")
        speedup = r_full['median_ms'] / r_filter['median_ms']
        results["optimization_gains"]["filter_with_pruning"] = round(speedup, 2)
        print(f"  Speedup:        {speedup:.2f}x")

        # Combined: projection + filter
        print("\n--- Combined: Projection + Filter ---")
        r_combined = benchmark(lambda: decoder.decode_with_filter(
            parquet_data,
            [filter_5k],
            column_indices=[0, 1]  # id, value
        ))
        results["read"]["combined_proj_filter"] = r_combined
        print(f"  2 cols + filter: {r_combined['median_ms']:.2f}ms (vs full: {r_full['median_ms']:.2f}ms)")
        speedup = r_full['median_ms'] / r_combined['median_ms']
        results["optimization_gains"]["combined_proj_filter"] = round(speedup, 2)
        print(f"  Speedup:        {speedup:.2f}x")

        # ================================================================
        # PART 3: HIGH-LEVEL COMPARISON
        # ================================================================
        print("\n" + "=" * 50)
        print("PART 3: TABLEWRITER/TABLEREADER PERFORMANCE")
        print("=" * 50)

        writer = TableWriter(store, catalog, chunk_size_rows=10_000)
        writer.write("benchmark_table", test_data)
        reader = TableReader(store, catalog)

        # TableReader benchmarks
        print("\n--- TableReader Performance ---")
        r_reader_full = benchmark(lambda: reader.read_arrow("benchmark_table"))
        results["read"]["table_reader_full"] = r_reader_full
        print(f"  Full read:      {r_reader_full['median_ms']:.2f}ms")

        r_reader_proj = benchmark(lambda: reader.read_arrow("benchmark_table", columns=["id", "value"]))
        results["read"]["table_reader_proj"] = r_reader_proj
        print(f"  2 columns:      {r_reader_proj['median_ms']:.2f}ms")

        r_reader_filter = benchmark(lambda: reader.read_arrow("benchmark_table", filters=[Filter("id").lt(5000)]))
        results["read"]["table_reader_filter"] = r_reader_filter
        print(f"  Filtered:       {r_reader_filter['median_ms']:.2f}ms")

        # ================================================================
        # SUMMARY
        # ================================================================
        print("\n" + "=" * 70)
        print("OPTIMIZATION SUMMARY")
        print("=" * 70)

        print("\nWrite Optimizations:")
        print(f"  Phase R.4 Parallel Encoding: {results['optimization_gains'].get('parallel_encoding', 'N/A')}x speedup")
        print(f"    (1M rows: {results['write']['1M_sequential']['median_ms']:.0f}ms -> {results['write']['1M_parallel']['median_ms']:.0f}ms)")

        print("\nRead Optimizations:")
        print(f"  Phase R.1 Projection (2 cols): {results['optimization_gains'].get('projection_2cols', 'N/A')}x speedup")
        print(f"  Phase R.1 Projection (1 col):  {results['optimization_gains'].get('projection_1col', 'N/A')}x speedup")
        print(f"  Phase R.2+R.3 Filter+Prune:    {results['optimization_gains'].get('filter_with_pruning', 'N/A')}x speedup")
        print(f"  Combined (proj+filter):        {results['optimization_gains'].get('combined_proj_filter', 'N/A')}x speedup")

        print("\n" + "-" * 70)
        print("MATHEMATICAL MODEL VALIDATION")
        print("-" * 70)
        print("""
Theoretical Model:
  Projection: Speedup = n/k where n=total columns, k=requested columns
  Filter:     Speedup = (G/g) x (1/s) where G=row groups, g=kept groups, s=selectivity
  Combined:   Speedup = (G/g) x (n/k) x (1/s)

Actual Results:
  10 columns, 2 requested:    Expected ~5x, Actual {:.1f}x
  10 row groups, 1 kept:      Expected ~10x, Actual {:.1f}x
  Combined (proj + filter):   Expected ~50x, Actual {:.1f}x

Note: Real-world results are lower due to fixed overhead (metadata parsing, etc.)
""".format(
            results['optimization_gains'].get('projection_2cols', 0),
            results['optimization_gains'].get('filter_with_pruning', 0),
            results['optimization_gains'].get('combined_proj_filter', 0),
        ))

        # Save results
        output_path = Path(__file__).parent / "FINAL_BENCHMARK_RESULTS.json"
        with open(output_path, "w") as f:
            json.dump(results, f, indent=2)
        print(f"Results saved to: {output_path}")

    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
