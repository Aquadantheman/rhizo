#!/usr/bin/env python3
"""
Lazy Parquet loading experiment.

Compares three approaches for registering data with DataFusion:
  A) Pre-materialized Arrow (current approach)
  B) Lazy via pyarrow.dataset pointing at Parquet chunk files
  C) Lazy via register_parquet (single-chunk shortcut)

Tests at 10M rows across read, projection, filter, aggregation, and complex queries.
"""

import os
import sys
import time
import statistics
import tempfile
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "python"))

import pyarrow as pa
import pyarrow.dataset as ds
import datafusion
import duckdb
from _rhizo import PyChunkStore, PyCatalog, PyParquetEncoder


# -- Queries ------------------------------------------------------------------

def get_queries(scale):
    cutoff = scale // 20
    return {
        "read":       "SELECT * FROM test",
        "projection": "SELECT id, amount FROM test",
        "filter_5pct": f"SELECT * FROM test WHERE id < {cutoff}",
        "agg":        "SELECT category, COUNT(*), AVG(amount), SUM(score) FROM test GROUP BY category",
        "complex":    ("SELECT category, status, COUNT(*) as cnt, AVG(amount) as avg_amt "
                       "FROM test WHERE score > 50 AND count > 100 "
                       "GROUP BY category, status ORDER BY avg_amt DESC"),
        "proj_filter": f"SELECT id, amount, category FROM test WHERE id < {cutoff}",
    }


# -- Helpers ------------------------------------------------------------------

def generate_data(num_rows, seed=42):
    rng = np.random.default_rng(seed)
    return pd.DataFrame({
        "id": np.arange(num_rows, dtype=np.int64),
        "value": rng.standard_normal(num_rows),
        "category": rng.choice(["A", "B", "C", "D"], num_rows),
        "timestamp": rng.integers(0, 1_000_000, num_rows),
        "amount": rng.uniform(0, 10000, num_rows),
        "count": rng.integers(0, 1000, num_rows),
        "name": np.array([f"item_{i % 1000}" for i in range(num_rows)]),
        "score": rng.uniform(0, 100, num_rows),
        "status": rng.choice(["active", "inactive", "pending"], num_rows),
        "flag": rng.choice([True, False], num_rows),
    })


def bench(fn, warmup=2, iterations=5):
    for _ in range(warmup):
        fn()
    times = []
    for _ in range(iterations):
        t0 = time.perf_counter()
        fn()
        times.append((time.perf_counter() - t0) * 1000)
    return statistics.median(times)


def chunk_path(store_base, h):
    return os.path.join(store_base, h[:2], h[2:4], h)


def print_comparison(results_dict, query_names):
    approaches = list(results_dict.keys())
    header = f"  {'Query':<16}" + "".join(f"{a:>14}" for a in approaches)
    print(header)
    print(f"  {'-' * (16 + 14 * len(approaches))}")
    for q in query_names:
        row = f"  {q:<16}"
        vals = [results_dict[a].get(q) for a in approaches]
        base = vals[0]  # first approach is baseline
        for v in vals:
            if v is None:
                row += f"{'N/A':>14}"
            else:
                ratio = base / v if v > 0 else 0
                marker = ""
                if ratio > 1.05:
                    marker = f" ({ratio:.1f}x)"
                elif ratio < 0.95:
                    marker = f" ({1/ratio:.1f}x slow)"
                row += f"{v:>8.1f}ms{marker:>5}"
        print(row)


# -- Main ---------------------------------------------------------------------

def main():
    SCALE = 10_000_000

    print("=" * 70)
    print("LAZY PARQUET LOADING EXPERIMENT")
    print("=" * 70)
    print(f"Scale: {SCALE:,} rows")
    print(f"DataFusion: {datafusion.__version__}")

    queries = get_queries(SCALE)

    # -- Generate data and write to Rhizo chunk store -------------------------
    print(f"\nPhase 1: Generate data and write chunks...")
    temp_dir = tempfile.mkdtemp()
    store_base = os.path.join(temp_dir, "chunks")
    cat_base = os.path.join(temp_dir, "catalog")
    store = PyChunkStore(store_base)
    catalog = PyCatalog(cat_base)
    encoder = PyParquetEncoder("zstd")

    print(f"  Generating {SCALE // 1_000_000}M rows...", end=" ", flush=True)
    df = generate_data(SCALE)
    arrow_table = pa.Table.from_pandas(df)
    print(f"done ({arrow_table.nbytes / 1e6:.0f}MB)")

    # Write as multiple chunks (like real Rhizo writer does)
    CHUNK_ROWS = 100_000
    chunk_hashes = []
    batches = arrow_table.to_batches(max_chunksize=CHUNK_ROWS)
    print(f"  Writing {len(batches)} chunks ({CHUNK_ROWS:,} rows each)...", end=" ", flush=True)
    t0 = time.perf_counter()
    for batch in batches:
        parquet_bytes = encoder.encode(batch)
        h = store.put(parquet_bytes)
        chunk_hashes.append(h)
    write_ms = (time.perf_counter() - t0) * 1000
    catalog.commit_next("test", chunk_hashes)
    print(f"done ({write_ms:.0f}ms)")

    chunk_paths = [chunk_path(store_base, h) for h in chunk_hashes]
    total_size = sum(os.path.getsize(p) for p in chunk_paths)
    print(f"  Chunks on disk: {len(chunk_paths)} files, {total_size / 1e6:.1f}MB")

    all_results = {}

    # -- Approach A: Pre-materialized Arrow (baseline) ------------------------
    print(f"\n{'=' * 70}")
    print("APPROACH A: Pre-materialized Arrow (current Rhizo approach)")
    print(f"{'=' * 70}")

    # Measure materialization cost
    print(f"  Materializing...", end=" ", flush=True)
    t0 = time.perf_counter()
    ctx_a = datafusion.SessionContext()
    ctx_a.register_record_batches("test", [arrow_table.to_batches()])
    mat_ms = (time.perf_counter() - t0) * 1000
    print(f"done ({mat_ms:.1f}ms)")

    results_a = {"_materialize": mat_ms}
    for name, sql in queries.items():
        ms = bench(lambda s=sql: ctx_a.sql(s).collect())
        results_a[name] = ms
        print(f"  {name:<16} {ms:>8.1f}ms")
    all_results["Materialized"] = results_a
    del ctx_a

    # -- Approach B: Lazy via pyarrow.dataset ---------------------------------
    print(f"\n{'=' * 70}")
    print("APPROACH B: Lazy via pyarrow.dataset (no pre-materialization)")
    print(f"{'=' * 70}")

    print(f"  Registering dataset ({len(chunk_paths)} files)...", end=" ", flush=True)
    t0 = time.perf_counter()
    ctx_b = datafusion.SessionContext()
    dataset = ds.dataset(chunk_paths, format="parquet")
    ctx_b.register_table("test", dataset)
    reg_ms = (time.perf_counter() - t0) * 1000
    print(f"done ({reg_ms:.1f}ms)")

    results_b = {"_register": reg_ms}
    for name, sql in queries.items():
        ms = bench(lambda s=sql: ctx_b.sql(s).collect())
        results_b[name] = ms
        print(f"  {name:<16} {ms:>8.1f}ms")
    all_results["Lazy dataset"] = results_b
    del ctx_b

    # -- Approach B2: Lazy dataset with pushdown enabled ----------------------
    print(f"\n{'=' * 70}")
    print("APPROACH B2: Lazy dataset + pushdown filters + reorder")
    print(f"{'=' * 70}")

    config = datafusion.SessionConfig()
    config.set("datafusion.execution.parquet.pushdown_filters", "true")
    config.set("datafusion.execution.parquet.reorder_filters", "true")

    print(f"  Registering...", end=" ", flush=True)
    t0 = time.perf_counter()
    ctx_b2 = datafusion.SessionContext(config)
    dataset2 = ds.dataset(chunk_paths, format="parquet")
    ctx_b2.register_table("test", dataset2)
    reg_ms2 = (time.perf_counter() - t0) * 1000
    print(f"done ({reg_ms2:.1f}ms)")

    results_b2 = {"_register": reg_ms2}
    for name, sql in queries.items():
        ms = bench(lambda s=sql: ctx_b2.sql(s).collect())
        results_b2[name] = ms
        print(f"  {name:<16} {ms:>8.1f}ms")
    all_results["Lazy+pushdown"] = results_b2
    del ctx_b2

    # -- DuckDB comparison ----------------------------------------------------
    print(f"\n{'=' * 70}")
    print("DUCKDB COMPARISON")
    print(f"{'=' * 70}")

    conn = duckdb.connect()
    conn.register("test_arrow", arrow_table)
    conn.execute("CREATE TABLE test AS SELECT * FROM test_arrow")

    results_duck = {}
    for name, sql in queries.items():
        ms = bench(lambda s=sql: conn.execute(s).df())
        results_duck[name] = ms
        print(f"  {name:<16} {ms:>8.1f}ms")
    conn.close()
    all_results["DuckDB"] = results_duck

    # -- Final comparison -----------------------------------------------------
    print(f"\n{'=' * 70}")
    print("FINAL COMPARISON")
    print(f"{'=' * 70}")

    query_names = ["read", "projection", "filter_5pct", "agg", "complex", "proj_filter"]
    print_comparison(all_results, query_names)

    # -- Summary --------------------------------------------------------------
    print(f"\n{'=' * 70}")
    print("SUMMARY: Key metrics")
    print(f"{'=' * 70}")

    for q in ["agg", "complex", "filter_5pct", "projection"]:
        mat = results_a[q]
        lazy = results_b2[q]
        duck = results_duck[q]
        improvement = mat / lazy if lazy > 0 else 0
        vs_duck = duck / lazy if lazy > 0 else 0
        print(f"  {q:<16} Materialized: {mat:>7.1f}ms  Lazy+PD: {lazy:>7.1f}ms  "
              f"DuckDB: {duck:>7.1f}ms  "
              f"Lazy speedup: {improvement:.2f}x  "
              f"{'Rhizo' if vs_duck > 1 else 'DuckDB'} wins: {max(vs_duck, 1/vs_duck):.1f}x")

    print(f"\n  Registration overhead:")
    print(f"    Materialized: {results_a['_materialize']:.1f}ms (full Arrow load)")
    print(f"    Lazy dataset: {results_b['_register']:.1f}ms (file list only)")

    # Cleanup
    import shutil
    shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
