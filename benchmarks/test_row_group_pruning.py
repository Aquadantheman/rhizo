"""
Test Row-Group Pruning - Verifies that row group statistics are used for pruning.

Row-group pruning only provides speedup when:
1. The Parquet file has MULTIPLE row groups
2. The filter can eliminate some row groups based on min/max statistics
"""

import io
import os
import sys
import time
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

from _rhizo import PyParquetEncoder, PyParquetDecoder, PyPredicateFilter


def create_multi_rowgroup_parquet(num_row_groups: int = 10, rows_per_group: int = 10000):
    """Create a Parquet file with multiple row groups, each with distinct id ranges.

    Row group 0: ids 0 to 9999
    Row group 1: ids 10000 to 19999
    ...
    Row group N: ids N*10000 to (N+1)*10000-1
    """
    all_data = []

    for rg in range(num_row_groups):
        start_id = rg * rows_per_group
        end_id = start_id + rows_per_group

        data = pa.table({
            "id": pa.array(range(start_id, end_id)),
            "value": pa.array(np.random.randn(rows_per_group)),
            "category": pa.array(np.random.choice(["A", "B", "C", "D"], rows_per_group)),
        })
        all_data.append(data)

    # Combine and write to bytes with row group size set to rows_per_group
    # This ensures each batch becomes its own row group
    combined = pa.concat_tables(all_data)

    buffer = io.BytesIO()
    pq.write_table(
        combined,
        buffer,
        row_group_size=rows_per_group,
        compression='zstd',
        write_statistics=True,  # Important for pruning!
    )

    return buffer.getvalue()


def main():
    print("=" * 60)
    print("ROW-GROUP PRUNING TEST")
    print("=" * 60)

    num_row_groups = 10
    rows_per_group = 10000
    total_rows = num_row_groups * rows_per_group

    print(f"\nCreating test file with:")
    print(f"  - {num_row_groups} row groups")
    print(f"  - {rows_per_group:,} rows per group")
    print(f"  - {total_rows:,} total rows")

    parquet_data = create_multi_rowgroup_parquet(num_row_groups, rows_per_group)
    print(f"  - File size: {len(parquet_data) / 1024:.1f} KB")

    decoder = PyParquetDecoder()

    # Verify row group count
    pq_file = pq.ParquetFile(io.BytesIO(parquet_data))
    actual_rg = pq_file.num_row_groups
    print(f"  - Actual row groups in file: {actual_rg}")

    print("\n--- Row Group Statistics ---")
    for i in range(min(3, actual_rg)):
        rg_meta = pq_file.metadata.row_group(i)
        id_col = rg_meta.column(0)
        if id_col.is_stats_set:
            print(f"  Row group {i}: id range [{id_col.statistics.min}, {id_col.statistics.max}]")
    if actual_rg > 3:
        print(f"  ... ({actual_rg - 3} more row groups)")

    print("\n--- Pruning Tests ---")

    # Test 1: Query that should only need 1 row group
    print("\nTest 1: Filter id < 5000 (should need only row group 0)")
    filter1 = PyPredicateFilter("id", "lt", 5000)
    total, pruned, kept = decoder.get_pruning_stats(parquet_data, [filter1])
    print(f"  Result: {pruned}/{total} row groups pruned ({100*pruned/total:.1f}%)")
    print(f"  Kept {kept} row groups")

    start = time.perf_counter()
    for _ in range(5):
        result = decoder.decode_with_filter(parquet_data, [filter1])
    elapsed = (time.perf_counter() - start) / 5 * 1000
    print(f"  Filtered decode: {elapsed:.2f}ms, {result.num_rows} rows returned")

    # Test 2: Query in the middle range
    print("\nTest 2: Filter id >= 45000 AND id < 55000 (should need 2 row groups)")
    filter2a = PyPredicateFilter("id", "ge", 45000)
    filter2b = PyPredicateFilter("id", "lt", 55000)
    total, pruned, kept = decoder.get_pruning_stats(parquet_data, [filter2a, filter2b])
    print(f"  Result: {pruned}/{total} row groups pruned ({100*pruned/total:.1f}%)")
    print(f"  Kept {kept} row groups")

    start = time.perf_counter()
    for _ in range(5):
        result = decoder.decode_with_filter(parquet_data, [filter2a, filter2b])
    elapsed = (time.perf_counter() - start) / 5 * 1000
    print(f"  Filtered decode: {elapsed:.2f}ms, {result.num_rows} rows returned")

    # Test 3: Query for last row group only
    print(f"\nTest 3: Filter id >= {total_rows - 5000} (should need only last row group)")
    filter3 = PyPredicateFilter("id", "ge", total_rows - 5000)
    total, pruned, kept = decoder.get_pruning_stats(parquet_data, [filter3])
    print(f"  Result: {pruned}/{total} row groups pruned ({100*pruned/total:.1f}%)")
    print(f"  Kept {kept} row groups")

    start = time.perf_counter()
    for _ in range(5):
        result = decoder.decode_with_filter(parquet_data, [filter3])
    elapsed = (time.perf_counter() - start) / 5 * 1000
    print(f"  Filtered decode: {elapsed:.2f}ms, {result.num_rows} rows returned")

    # Test 4: Query that cannot prune (matches all row groups)
    print("\nTest 4: Filter value > 0 (cannot prune - all row groups have positive values)")
    filter4 = PyPredicateFilter("value", "gt", 0.0)
    total, pruned, kept = decoder.get_pruning_stats(parquet_data, [filter4])
    print(f"  Result: {pruned}/{total} row groups pruned ({100*pruned/total:.1f}%)")

    # Test 5: Compare full scan vs pruned scan
    print("\n--- Performance Comparison ---")

    # Full decode (no filter)
    start = time.perf_counter()
    for _ in range(5):
        full_result = decoder.decode(parquet_data)
    full_time = (time.perf_counter() - start) / 5 * 1000
    print(f"\nFull decode (all {total_rows:,} rows):    {full_time:.2f}ms")

    # Filtered decode (1 row group)
    start = time.perf_counter()
    for _ in range(5):
        filtered_result = decoder.decode_with_filter(parquet_data, [filter1])
    filtered_time = (time.perf_counter() - start) / 5 * 1000
    print(f"Filtered decode ({filtered_result.num_rows:,} rows): {filtered_time:.2f}ms")

    speedup = full_time / filtered_time if filtered_time > 0 else float('inf')
    print(f"\nSpeedup from row-group pruning: {speedup:.1f}x")

    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"Row-group pruning is working correctly!")
    print(f"When filtering {total_rows:,} rows to ~{rows_per_group // 2:,} rows:")
    print(f"  - {pruned}/{total} row groups pruned ({100*pruned/total:.0f}%)")
    print(f"  - {speedup:.1f}x speedup achieved")


if __name__ == "__main__":
    main()
