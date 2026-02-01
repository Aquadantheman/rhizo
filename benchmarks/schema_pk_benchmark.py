"""
Schema Evolution & Primary Key Benchmarks.

Measures overhead of schema validation and primary key uniqueness checks
to verify they are fast enough for production use.

Usage:
    python benchmarks/schema_pk_benchmark.py [--quick]
"""

import argparse
import sys
import tempfile
import time

import pandas as pd
import pyarrow as pa

import rhizo
from rhizo.schema_utils import (
    compare_schemas,
    deserialize_schema,
    serialize_schema,
)
from rhizo.table_meta import TableMeta, TableMetaStore


def benchmark(name, fn, iterations=100):
    """Run a benchmark and print results."""
    # Warmup
    fn()

    times = []
    for _ in range(iterations):
        t0 = time.perf_counter()
        fn()
        times.append(time.perf_counter() - t0)

    avg_ms = sum(times) / len(times) * 1000
    min_ms = min(times) * 1000
    max_ms = max(times) * 1000
    p50 = sorted(times)[len(times) // 2] * 1000
    print(f"  {name}: avg={avg_ms:.3f}ms  min={min_ms:.3f}ms  max={max_ms:.3f}ms  p50={p50:.3f}ms")
    return avg_ms


def run_benchmarks(quick=False):
    iters = 20 if quick else 100

    print("=" * 70)
    print("Schema Evolution & Primary Key Benchmarks")
    print("=" * 70)

    # -----------------------------------------------------------------------
    # 1. Schema serialization roundtrip
    # -----------------------------------------------------------------------
    print("\n1. Schema Serialization Roundtrip")
    schema = pa.schema([
        pa.field(f"col_{i}", pa.int64() if i % 3 == 0 else pa.string())
        for i in range(50)
    ])

    def serialize_roundtrip():
        s = serialize_schema(schema)
        deserialize_schema(s)

    benchmark("50-column schema", serialize_roundtrip, iters)

    # -----------------------------------------------------------------------
    # 2. Schema comparison
    # -----------------------------------------------------------------------
    print("\n2. Schema Comparison (50 columns)")
    old_schema = pa.schema([pa.field(f"col_{i}", pa.int64()) for i in range(50)])
    new_schema = pa.schema(
        [pa.field(f"col_{i}", pa.int64()) for i in range(50)]
        + [pa.field("new_col", pa.string())]
    )

    def compare_additive():
        compare_schemas(old_schema, new_schema, "additive")

    benchmark("additive comparison", compare_additive, iters)

    # -----------------------------------------------------------------------
    # 3. TableMetaStore load/save
    # -----------------------------------------------------------------------
    print("\n3. TableMetaStore Load/Save")
    d = tempfile.mkdtemp()
    store = TableMetaStore(d)
    meta = TableMeta(primary_key=["id"], schema_mode="additive")
    store.save("bench_table", meta)

    def meta_load():
        store.load("bench_table")

    def meta_save():
        store.save("bench_table", meta)

    benchmark("load", meta_load, iters)
    benchmark("save", meta_save, iters)

    # -----------------------------------------------------------------------
    # 4. Write baseline (no PK, no schema)
    # -----------------------------------------------------------------------
    print("\n4. Write Baseline vs Write with PK + Schema")
    n_rows = 10_000 if quick else 100_000
    df = pd.DataFrame({"id": range(n_rows), "value": range(n_rows)})

    # Baseline write (no PK)
    d_base = tempfile.mkdtemp()
    db_base = rhizo.open(d_base)

    def write_baseline():
        db_base.write("bench", df, schema_mode="flexible")

    t0 = time.perf_counter()
    write_baseline()
    baseline_ms = (time.perf_counter() - t0) * 1000
    print(f"  baseline write ({n_rows:,} rows): {baseline_ms:.1f}ms")
    db_base.close()

    # Write with PK enforcement
    d_pk = tempfile.mkdtemp()
    db_pk = rhizo.open(d_pk)

    def write_with_pk():
        db_pk.write("bench", df, primary_key=["id"])

    t0 = time.perf_counter()
    write_with_pk()
    pk_ms = (time.perf_counter() - t0) * 1000
    overhead = pk_ms - baseline_ms
    pct = (overhead / baseline_ms * 100) if baseline_ms > 0 else 0
    print(f"  write with PK ({n_rows:,} rows): {pk_ms:.1f}ms")
    print(f"  overhead: {overhead:+.1f}ms ({pct:+.1f}%)")
    db_pk.close()

    # -----------------------------------------------------------------------
    # 5. PK uniqueness check at scale
    # -----------------------------------------------------------------------
    print("\n5. PK Uniqueness Check at Scale")
    for n in [10_000, 100_000] if quick else [10_000, 100_000, 1_000_000]:
        df_scale = pd.DataFrame({"id": range(n), "value": range(n)})
        d_scale = tempfile.mkdtemp()
        db_scale = rhizo.open(d_scale)
        t0 = time.perf_counter()
        db_scale.write("bench", df_scale, primary_key=["id"])
        elapsed_ms = (time.perf_counter() - t0) * 1000
        print(f"  {n:>10,} rows: {elapsed_ms:.1f}ms")
        db_scale.close()

    # -----------------------------------------------------------------------
    # 6. Diff auto-PK vs explicit
    # -----------------------------------------------------------------------
    print("\n6. Diff: auto-PK vs explicit key_columns")
    d_diff = tempfile.mkdtemp()
    db_diff = rhizo.open(d_diff)
    n_diff = 10_000 if quick else 50_000
    df1 = pd.DataFrame({"id": range(n_diff), "v": range(n_diff)})
    df2 = pd.DataFrame({"id": range(n_diff), "v": [x + 1 for x in range(n_diff)]})
    db_diff.write("t", df1, primary_key=["id"])
    db_diff.write("t", df2)

    def diff_auto():
        db_diff.diff("t")

    def diff_explicit():
        db_diff.diff("t", key_columns=["id"])

    auto_ms = benchmark("auto-PK", diff_auto, min(iters, 10))
    explicit_ms = benchmark("explicit key", diff_explicit, min(iters, 10))
    ratio = auto_ms / explicit_ms if explicit_ms > 0 else 1
    print(f"  ratio (auto/explicit): {ratio:.2f}x")
    db_diff.close()

    # -----------------------------------------------------------------------
    # Summary
    # -----------------------------------------------------------------------
    print("\n" + "=" * 70)
    print("Summary")
    print("=" * 70)
    print(f"  Schema roundtrip:       < 0.1ms target")
    print(f"  Schema comparison:      < 0.1ms target")
    print(f"  TableMeta load/save:    < 1ms target")
    print(f"  PK write overhead:      {overhead:+.1f}ms ({pct:+.1f}%)")
    print(f"  Diff auto-PK same as explicit: {ratio:.2f}x")
    print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--quick", action="store_true", help="Run fewer iterations")
    args = parser.parse_args()
    run_benchmarks(quick=args.quick)
