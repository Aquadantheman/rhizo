#!/usr/bin/env python3
"""
Export benchmark: Rhizo export vs DuckDB COPY TO.

Compares Parquet, CSV, and JSON export performance at 1M and 10M rows.
Tests streaming export, single-chunk fast path, and column projection.

Usage:
    python benchmarks/export_benchmark.py
    python benchmarks/export_benchmark.py --scale 1000000
"""

import argparse
import os
import shutil
import sys
import statistics
import tempfile
import time

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "python"))

import pyarrow as pa
import pyarrow.parquet as pq
import duckdb

import rhizo


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def generate_data(num_rows: int, seed: int = 42) -> pd.DataFrame:
    """Generate a realistic mixed-type dataset."""
    rng = np.random.default_rng(seed)
    return pd.DataFrame({
        "id": np.arange(num_rows, dtype=np.int64),
        "value": rng.standard_normal(num_rows),
        "category": rng.choice(["A", "B", "C", "D", "E"], num_rows),
        "timestamp": pd.date_range("2020-01-01", periods=num_rows, freq="s"),
        "amount": rng.uniform(0, 10_000, num_rows).astype(np.float64),
        "flag": rng.choice([True, False], num_rows),
    })


def time_fn(fn, warmup: int = 0, iterations: int = 3):
    """Time a function, returning (median_seconds, all_times)."""
    for _ in range(warmup):
        fn()
    times = []
    for _ in range(iterations):
        t0 = time.perf_counter()
        fn()
        times.append(time.perf_counter() - t0)
    return statistics.median(times), times


def fmt_size(nbytes: int) -> str:
    if nbytes >= 1 << 20:
        return f"{nbytes / (1 << 20):.1f} MB"
    if nbytes >= 1 << 10:
        return f"{nbytes / (1 << 10):.1f} KB"
    return f"{nbytes} B"


def fmt_rate(rows: int, seconds: float) -> str:
    rate = rows / seconds
    if rate >= 1e6:
        return f"{rate / 1e6:.2f}M rows/s"
    return f"{rate / 1e3:.1f}K rows/s"


def print_header(title: str):
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print(f"{'=' * 60}")


def print_result(label: str, median: float, rows: int, file_size: int):
    print(f"  {label:<40s} {median:.3f}s  {fmt_rate(rows, median):>14s}  {fmt_size(file_size):>10s}")


# ---------------------------------------------------------------------------
# Benchmarks
# ---------------------------------------------------------------------------

def bench_parquet_export(db, table_name: str, out_dir: str, num_rows: int):
    """Benchmark Parquet export: Rhizo streaming vs DuckDB COPY TO."""
    print_header(f"Parquet Export — {num_rows:,} rows")

    # --- Rhizo export ---
    out_rhizo = os.path.join(out_dir, "rhizo_export.parquet")

    def rhizo_export():
        db.export(table_name, out_rhizo)

    med, _ = time_fn(rhizo_export)
    sz = os.path.getsize(out_rhizo)
    print_result("Rhizo (streaming zstd)", med, num_rows, sz)

    # --- DuckDB COPY TO ---
    arrow_table = db.read(table_name)
    con = duckdb.connect()
    con.register("tbl", arrow_table)
    out_duckdb = os.path.join(out_dir, "duckdb_export.parquet")

    def duckdb_export():
        con.execute(f"COPY tbl TO '{out_duckdb}' (FORMAT PARQUET, COMPRESSION ZSTD)")

    med_duck, _ = time_fn(duckdb_export)
    sz_duck = os.path.getsize(out_duckdb)
    print_result("DuckDB COPY TO (zstd)", med_duck, num_rows, sz_duck)

    ratio = med / med_duck if med_duck > 0 else float("inf")
    print(f"  {'Ratio (Rhizo / DuckDB):':<40s} {ratio:.2f}x")
    con.close()
    return med, med_duck


def bench_csv_export(db, table_name: str, out_dir: str, num_rows: int):
    """Benchmark CSV export: Rhizo vs DuckDB COPY TO."""
    print_header(f"CSV Export — {num_rows:,} rows")

    out_rhizo = os.path.join(out_dir, "rhizo_export.csv")

    def rhizo_export():
        db.export(table_name, out_rhizo)

    med, _ = time_fn(rhizo_export)
    sz = os.path.getsize(out_rhizo)
    print_result("Rhizo (pyarrow.csv)", med, num_rows, sz)

    arrow_table = db.read(table_name)
    con = duckdb.connect()
    con.register("tbl", arrow_table)
    out_duckdb = os.path.join(out_dir, "duckdb_export.csv")

    def duckdb_export():
        con.execute(f"COPY tbl TO '{out_duckdb}' (FORMAT CSV, HEADER)")

    med_duck, _ = time_fn(duckdb_export)
    sz_duck = os.path.getsize(out_duckdb)
    print_result("DuckDB COPY TO (csv)", med_duck, num_rows, sz_duck)

    ratio = med / med_duck if med_duck > 0 else float("inf")
    print(f"  {'Ratio (Rhizo / DuckDB):':<40s} {ratio:.2f}x")
    con.close()
    return med, med_duck


def bench_json_export(db, table_name: str, out_dir: str, num_rows: int):
    """Benchmark JSON export (Rhizo only — DuckDB JSON COPY is optional)."""
    print_header(f"JSON Export — {num_rows:,} rows")

    out_rhizo = os.path.join(out_dir, "rhizo_export.json")

    def rhizo_export():
        db.export(table_name, out_rhizo)

    med, _ = time_fn(rhizo_export)
    sz = os.path.getsize(out_rhizo)
    print_result("Rhizo (json.dump)", med, num_rows, sz)
    return med


def bench_column_projection(db, table_name: str, out_dir: str, num_rows: int):
    """Benchmark export with column projection vs full export.

    Note: single-chunk tables hit the raw-byte-copy fast path for full export,
    which skips deserialization entirely. Projected export must deserialize, so
    it may be *slower* in wall time despite producing a smaller file. This is
    expected — the fast path is the whole point.
    """
    print_header(f"Column Projection — {num_rows:,} rows")

    out_full = os.path.join(out_dir, "full.parquet")
    out_proj = os.path.join(out_dir, "projected.parquet")

    def export_full():
        db.export(table_name, out_full)

    def export_projected():
        db.export(table_name, out_proj, columns=["id", "value"])

    med_full, _ = time_fn(export_full)
    sz_full = os.path.getsize(out_full)
    print_result("Full (6 col, fast path)", med_full, num_rows, sz_full)

    med_proj, _ = time_fn(export_projected)
    sz_proj = os.path.getsize(out_proj)
    print_result("Projected (2 col, streaming)", med_proj, num_rows, sz_proj)

    size_ratio = sz_proj / sz_full if sz_full > 0 else 0
    print(f"  {'File size reduction:':<40s} {size_ratio:.1%} of full")
    print(f"  Note: full export uses raw-byte-copy fast path (no deser).")


def bench_single_chunk_fast_path(out_dir: str, num_rows: int):
    """Benchmark single-chunk fast path vs multi-chunk streaming.

    Uses TableWriter directly with chunk_size_rows to create multiple chunks
    within a single version (as opposed to multiple versions).
    """
    print_header(f"Single-Chunk Fast Path — {num_rows:,} rows")

    from rhizo.writer import TableWriter
    from _rhizo import PyChunkStore, PyCatalog

    df = generate_data(num_rows)

    # Single-chunk DB (default chunk size is large enough for 1M rows)
    single_dir = os.path.join(out_dir, "single_chunk_db")
    with rhizo.open(single_dir) as db:
        db.write("data", df)
        out_single = os.path.join(out_dir, "single.parquet")

        def export_single():
            db.export("data", out_single)

        med_single, _ = time_fn(export_single)
        sz_single = os.path.getsize(out_single)
        print_result("Single chunk (raw copy)", med_single, num_rows, sz_single)

    # Multi-chunk DB: use small chunk_size_rows to force multiple chunks in ONE version
    multi_dir = os.path.join(out_dir, "multi_chunk_db")
    os.makedirs(os.path.join(multi_dir, "chunks"), exist_ok=True)
    os.makedirs(os.path.join(multi_dir, "catalog"), exist_ok=True)
    store = PyChunkStore(os.path.join(multi_dir, "chunks"))
    catalog = PyCatalog(os.path.join(multi_dir, "catalog"))
    chunk_rows = max(num_rows // 10, 1000)
    writer = TableWriter(store, catalog, chunk_size_rows=chunk_rows)
    writer.write("data", df)

    with rhizo.open(multi_dir) as db:
        out_multi = os.path.join(out_dir, "multi.parquet")

        def export_multi():
            db.export("data", out_multi)

        med_multi, _ = time_fn(export_multi)
        sz_multi = os.path.getsize(out_multi)
        n_chunks = len(db.engine.reader.get_metadata("data").chunk_hashes)
        print_result(f"Multi chunk ({n_chunks} chunks, streaming)", med_multi, num_rows, sz_multi)

    ratio = med_multi / med_single if med_single > 0 else float("inf")
    print(f"  {'Streaming / fast-path ratio:':<40s} {ratio:.2f}x")


def bench_version_export(db, table_name: str, out_dir: str, num_rows: int):
    """Benchmark exporting an older version (time travel)."""
    print_header(f"Version Export (time travel) — {num_rows:,} rows")

    out_latest = os.path.join(out_dir, "latest.parquet")
    out_v1 = os.path.join(out_dir, "v1.parquet")

    def export_latest():
        db.export(table_name, out_latest)

    def export_v1():
        db.export(table_name, out_v1, version=1)

    med_latest, _ = time_fn(export_latest)
    print_result("Latest version", med_latest, num_rows, os.path.getsize(out_latest))

    med_v1, _ = time_fn(export_v1)
    print_result("Version 1 (time travel)", med_v1, num_rows, os.path.getsize(out_v1))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run_benchmark(scale: int):
    print(f"Export Benchmark — {scale:,} rows")
    print(f"{'=' * 60}")

    tmp_root = tempfile.mkdtemp(prefix="rhizo_export_bench_")
    db_dir = os.path.join(tmp_root, "db")
    out_dir = os.path.join(tmp_root, "output")
    os.makedirs(out_dir, exist_ok=True)

    try:
        # Setup: write data
        print("\nGenerating data...")
        t0 = time.perf_counter()
        df = generate_data(scale)
        print(f"  Generated {scale:,} rows in {time.perf_counter() - t0:.2f}s")

        print("Writing to Rhizo...")
        t0 = time.perf_counter()
        db = rhizo.open(db_dir)
        db.write("bench", df)
        # Write a second version for time-travel test
        db.write("bench", df.iloc[: scale // 2])
        print(f"  Wrote 2 versions in {time.perf_counter() - t0:.2f}s")

        # Run benchmarks
        bench_parquet_export(db, "bench", out_dir, scale)
        bench_csv_export(db, "bench", out_dir, scale)

        # JSON is slow at large scale — only run at <= 1M
        if scale <= 1_000_000:
            bench_json_export(db, "bench", out_dir, scale)
        else:
            print(f"\n  (Skipping JSON export at {scale:,} rows — too slow)")

        bench_column_projection(db, "bench", out_dir, scale)
        bench_version_export(db, "bench", out_dir, scale)
        bench_single_chunk_fast_path(out_dir, min(scale, 1_000_000))

        db.close()

        # Summary
        print_header("Summary")
        print(f"  Scale:      {scale:,} rows")
        print(f"  Temp dir:   {tmp_root}")
        print(f"  All benchmarks completed successfully.")

    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Rhizo export benchmark")
    parser.add_argument("--scale", type=int, default=1_000_000, help="Number of rows (default: 1M)")
    args = parser.parse_args()
    run_benchmark(args.scale)
