"""
Test Parallel Parquet Encoding - Benchmark multi-chunk writes with Rayon parallelization.

Parallel encoding provides speedup when:
1. Writing multiple chunks (large datasets split into chunks)
2. Using the native Rust encoder with Rayon
"""

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

import numpy as np
import pyarrow as pa

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

from _rhizo import PyChunkStore, PyCatalog, PyParquetEncoder
from rhizo import TableWriter


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


def benchmark(func, iterations: int = 5):
    """Run benchmark and return median time in ms."""
    times = []
    for _ in range(iterations):
        start = time.perf_counter()
        func()
        elapsed = (time.perf_counter() - start) * 1000
        times.append(elapsed)
    return np.median(times)


def main():
    print("=" * 60)
    print("PARALLEL PARQUET ENCODING BENCHMARK")
    print("=" * 60)

    # Test with 1M rows and small chunk size to force multiple chunks
    num_rows = 1_000_000
    chunk_size_rows = 100_000  # 10 chunks

    print(f"\nTest setup:")
    print(f"  - {num_rows:,} total rows")
    print(f"  - {chunk_size_rows:,} rows per chunk")
    print(f"  - Expected chunks: {num_rows // chunk_size_rows}")

    print("\nGenerating test data...")
    table = generate_test_data(num_rows)
    print(f"  - Data generated")

    # Setup temp directories
    temp_dir = tempfile.mkdtemp(prefix="parallel_enc_bench_")
    chunks_path1 = os.path.join(temp_dir, "chunks1")
    catalog_path1 = os.path.join(temp_dir, "catalog1")
    chunks_path2 = os.path.join(temp_dir, "chunks2")
    catalog_path2 = os.path.join(temp_dir, "catalog2")

    try:
        # Test 1: Sequential encoding (single batch at a time)
        print("\n--- Test 1: Sequential Encoding ---")
        store1 = PyChunkStore(chunks_path1)
        catalog1 = PyCatalog(catalog_path1)

        # Manually chunk and encode sequentially
        encoder = PyParquetEncoder("zstd")
        batches = []
        for i in range(0, num_rows, chunk_size_rows):
            chunk = table.slice(i, min(chunk_size_rows, num_rows - i))
            batch = chunk.combine_chunks().to_batches()[0]
            batches.append(batch)

        print(f"  Encoding {len(batches)} batches sequentially...")
        start = time.perf_counter()
        for _ in range(3):
            encoded = [bytes(encoder.encode(b)) for b in batches]
        sequential_time = (time.perf_counter() - start) / 3 * 1000
        print(f"  Sequential encoding: {sequential_time:.2f}ms")

        # Test 2: Parallel encoding using encode_batch
        print("\n--- Test 2: Parallel Encoding (Rayon) ---")
        print(f"  Encoding {len(batches)} batches in parallel...")
        start = time.perf_counter()
        for _ in range(3):
            encoded = [bytes(b) for b in encoder.encode_batch(batches)]
        parallel_time = (time.perf_counter() - start) / 3 * 1000
        print(f"  Parallel encoding: {parallel_time:.2f}ms")

        speedup = sequential_time / parallel_time if parallel_time > 0 else 0
        print(f"\n  Speedup: {speedup:.2f}x")

        # Test 3: Full TableWriter comparison
        print("\n--- Test 3: TableWriter Full Pipeline ---")
        store2 = PyChunkStore(chunks_path2)
        catalog2 = PyCatalog(catalog_path2)

        # With parallel encoding (new code)
        writer = TableWriter(store2, catalog2, chunk_size_rows=chunk_size_rows)
        print(f"  Writing with parallel encoding...")
        start = time.perf_counter()
        writer.write("parallel_table", table)
        parallel_write_time = (time.perf_counter() - start) * 1000
        print(f"  Parallel TableWriter: {parallel_write_time:.2f}ms")

        # Verify chunks were written
        version = catalog2.get_version("parallel_table")
        print(f"  Wrote version {version.version} with {len(version.chunk_hashes)} chunks")

        print("\n" + "=" * 60)
        print("SUMMARY")
        print("=" * 60)
        print(f"\nEncoding {num_rows:,} rows ({num_rows // chunk_size_rows} chunks):")
        print(f"  Sequential: {sequential_time:.2f}ms")
        print(f"  Parallel:   {parallel_time:.2f}ms")
        print(f"  Speedup:    {speedup:.2f}x")
        print(f"\nFull TableWriter pipeline: {parallel_write_time:.2f}ms")

    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
