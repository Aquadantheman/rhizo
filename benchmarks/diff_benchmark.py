#!/usr/bin/env python3
"""
Diff benchmark: Version and branch diff performance.

Measures:
  1. Row count scaling (1K/10K/100K/1M rows, 5% change)
  2. Change percentage scaling (1%/5%/25%/50%/100% changes)
  3. Merkle skip ratio (large table, small change)
  4. Column count scaling (5/20/50 columns)
  5. Semantic diff overhead (with vs without algebraic schema)
  6. End-to-end db.diff() (full integration)

Usage:
    python benchmarks/diff_benchmark.py
    python benchmarks/diff_benchmark.py --quick   # Fast mode for CI
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

import rhizo
from rhizo.diff import DiffEngine
from rhizo.writer import TableWriter
from rhizo.reader import TableReader
import _rhizo


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def generate_data(num_rows: int, num_cols: int = 3, seed: int = 42) -> pd.DataFrame:
    """Generate a dataset with an id column + value columns."""
    rng = np.random.default_rng(seed)
    data = {"id": np.arange(num_rows, dtype=np.int64)}
    for i in range(num_cols - 1):
        data[f"v{i}"] = rng.integers(0, 1_000_000, num_rows, dtype=np.int64)
    return pd.DataFrame(data)


def mutate_data(df: pd.DataFrame, change_pct: float, seed: int = 99) -> pd.DataFrame:
    """Mutate change_pct of rows (non-id columns) and return new DataFrame."""
    rng = np.random.default_rng(seed)
    df2 = df.copy()
    n_change = max(1, int(len(df) * change_pct))
    idx = rng.choice(len(df), n_change, replace=False)
    value_cols = [c for c in df.columns if c != "id"]
    for col in value_cols:
        df2.loc[df2.index[idx], col] = rng.integers(0, 1_000_000, n_change)
    return df2


def time_fn(fn, warmup: int = 0, iterations: int = 3):
    """Time a function, returning (median_seconds, all_times, last_result)."""
    for _ in range(warmup):
        fn()
    times = []
    result = None
    for _ in range(iterations):
        t0 = time.perf_counter()
        result = fn()
        times.append(time.perf_counter() - t0)
    return statistics.median(times), times, result


def make_env(base_dir):
    """Create low-level diff environment."""
    chunks_dir = os.path.join(base_dir, "chunks")
    catalog_dir = os.path.join(base_dir, "catalog")
    branches_dir = os.path.join(base_dir, "branches")

    store = _rhizo.PyChunkStore(chunks_dir)
    catalog = _rhizo.PyCatalog(catalog_dir)
    branch_mgr = _rhizo.PyBranchManager(branches_dir)

    try:
        branch_mgr.create("main")
    except (ValueError, OSError):
        pass

    return store, catalog, branch_mgr


def fmt_time(seconds):
    """Format time for display."""
    if seconds < 0.001:
        return f"{seconds * 1_000_000:.0f}us"
    elif seconds < 1.0:
        return f"{seconds * 1000:.1f}ms"
    else:
        return f"{seconds:.2f}s"


def fmt_rate(count, seconds):
    """Format rows per second."""
    if seconds == 0:
        return "inf"
    rate = count / seconds
    if rate > 1_000_000:
        return f"{rate / 1_000_000:.1f}M rows/s"
    elif rate > 1_000:
        return f"{rate / 1_000:.1f}K rows/s"
    return f"{rate:.0f} rows/s"


def print_header(title):
    """Print a section header."""
    print(f"\n{'=' * 70}")
    print(f"  {title}")
    print(f"{'=' * 70}")


def print_row(label, value, extra=""):
    """Print a formatted result row."""
    if extra:
        print(f"  {label:<40} {value:>12}  {extra}")
    else:
        print(f"  {label:<40} {value:>12}")


# ---------------------------------------------------------------------------
# Benchmarks
# ---------------------------------------------------------------------------

def bench_row_scaling(quick: bool = False):
    """Benchmark 1: Diff time vs row count (5% change)."""
    print_header("Benchmark 1: Row Count Scaling (5% change)")
    sizes = [1_000, 10_000, 100_000] if quick else [1_000, 10_000, 100_000, 1_000_000]

    for n_rows in sizes:
        with tempfile.TemporaryDirectory() as tmp:
            store, catalog, _ = make_env(tmp)
            writer = TableWriter(store, catalog)
            reader = TableReader(store, catalog)

            df1 = generate_data(n_rows)
            df2 = mutate_data(df1, 0.05)
            writer.write("t", df1)
            writer.write("t", df2)

            engine = DiffEngine(catalog, store, reader)
            med, _, result = time_fn(
                lambda: engine.diff("t", 1, 2, key_columns=["id"]),
                iterations=3,
            )
            total_changes = (
                result.rows.added.num_rows
                + result.rows.removed.num_rows
                + result.rows.modified.num_rows
            )
            print_row(
                f"{n_rows:>10,} rows",
                fmt_time(med),
                f"{fmt_rate(n_rows, med)}  changes={total_changes}",
            )


def bench_change_pct(quick: bool = False):
    """Benchmark 2: Diff time vs change percentage (100K rows)."""
    print_header("Benchmark 2: Change Percentage Scaling (100K rows)")
    n_rows = 10_000 if quick else 100_000
    pcts = [0.01, 0.05, 0.25, 0.50, 1.0]

    for pct in pcts:
        with tempfile.TemporaryDirectory() as tmp:
            store, catalog, _ = make_env(tmp)
            writer = TableWriter(store, catalog)
            reader = TableReader(store, catalog)

            df1 = generate_data(n_rows)
            df2 = mutate_data(df1, pct)
            writer.write("t", df1)
            writer.write("t", df2)

            engine = DiffEngine(catalog, store, reader)
            med, _, result = time_fn(
                lambda: engine.diff("t", 1, 2, key_columns=["id"]),
                iterations=3,
            )
            n_mod = result.rows.modified.num_rows
            print_row(
                f"{pct * 100:>5.0f}% changed",
                fmt_time(med),
                f"modified={n_mod}",
            )


def bench_merkle_skip(quick: bool = False):
    """Benchmark 3: Merkle chunk skip ratio."""
    print_header("Benchmark 3: Merkle Chunk Skip Ratio")
    n_rows = 10_000 if quick else 100_000

    with tempfile.TemporaryDirectory() as tmp:
        store, catalog, _ = make_env(tmp)
        writer = TableWriter(store, catalog)
        reader = TableReader(store, catalog)

        df1 = generate_data(n_rows)
        # Identical data rewrite
        writer.write("t", df1)
        writer.write("t", df1)

        engine = DiffEngine(catalog, store, reader)
        med, _, result = time_fn(
            lambda: engine.diff("t", 1, 2, key_columns=["id"]),
            iterations=3,
        )
        skip_pct = (
            result.chunks_skipped / max(1, result.chunks_scanned + result.chunks_skipped) * 100
        )
        print_row("Identical data (fast path)", fmt_time(med), f"skip={skip_pct:.0f}%")

        # 1% change
        df2 = mutate_data(df1, 0.01, seed=77)
        catalog2 = _rhizo.PyCatalog(os.path.join(tmp, "catalog2"))
        store2 = _rhizo.PyChunkStore(os.path.join(tmp, "chunks2"))
        writer2 = TableWriter(store2, catalog2)
        reader2 = TableReader(store2, catalog2)
        writer2.write("t", df1)
        writer2.write("t", df2)

        engine2 = DiffEngine(catalog2, store2, reader2)
        med2, _, result2 = time_fn(
            lambda: engine2.diff("t", 1, 2, key_columns=["id"]),
            iterations=3,
        )
        skip2 = (
            result2.chunks_skipped
            / max(1, result2.chunks_scanned + result2.chunks_skipped)
            * 100
        )
        print_row(
            "1% change",
            fmt_time(med2),
            f"scanned={result2.chunks_scanned} skipped={result2.chunks_skipped} ({skip2:.0f}%)",
        )


def bench_column_scaling(quick: bool = False):
    """Benchmark 4: Diff time vs column count."""
    print_header("Benchmark 4: Column Count Scaling (10K rows, 5% change)")
    n_rows = 5_000 if quick else 10_000
    col_counts = [5, 20, 50]

    for n_cols in col_counts:
        with tempfile.TemporaryDirectory() as tmp:
            store, catalog, _ = make_env(tmp)
            writer = TableWriter(store, catalog)
            reader = TableReader(store, catalog)

            df1 = generate_data(n_rows, num_cols=n_cols)
            df2 = mutate_data(df1, 0.05)
            writer.write("t", df1)
            writer.write("t", df2)

            engine = DiffEngine(catalog, store, reader)
            med, _, result = time_fn(
                lambda: engine.diff("t", 1, 2, key_columns=["id"]),
                iterations=3,
            )
            print_row(f"{n_cols} columns", fmt_time(med), f"modified={result.rows.modified.num_rows}")


def bench_semantic_overhead(quick: bool = False):
    """Benchmark 5: Semantic diff overhead."""
    print_header("Benchmark 5: Semantic Diff Overhead (10K rows, 5% change)")
    n_rows = 5_000 if quick else 10_000

    with tempfile.TemporaryDirectory() as tmp:
        store, catalog, _ = make_env(tmp)
        writer = TableWriter(store, catalog)
        reader = TableReader(store, catalog)

        df1 = generate_data(n_rows)
        df2 = mutate_data(df1, 0.05)
        writer.write("t", df1)
        writer.write("t", df2)

        engine = DiffEngine(catalog, store, reader)

        # Without semantic schema
        med_plain, _, _ = time_fn(
            lambda: engine.diff("t", 1, 2, key_columns=["id"]),
            iterations=3,
        )
        print_row("Without schema", fmt_time(med_plain))

        # With semantic schema
        schema = _rhizo.PyTableAlgebraicSchema("t")
        schema.add_column("v0", _rhizo.PyOpType("add"))
        schema.add_column("v1", _rhizo.PyOpType("max"))

        med_semantic, _, result = time_fn(
            lambda: engine.diff("t", 1, 2, key_columns=["id"], schema=schema),
            iterations=3,
        )
        overhead = (med_semantic / max(med_plain, 1e-9) - 1) * 100
        n_semantic = sum(len(v) for v in (result.semantic_changes or {}).values())
        print_row(
            "With schema",
            fmt_time(med_semantic),
            f"overhead={overhead:+.1f}%  semantic_entries={n_semantic}",
        )


def bench_db_diff(quick: bool = False):
    """Benchmark 6: End-to-end db.diff()."""
    print_header("Benchmark 6: End-to-End db.diff()")
    n_rows = 10_000 if quick else 100_000

    with tempfile.TemporaryDirectory() as tmp:
        with rhizo.open(tmp) as db:
            df1 = generate_data(n_rows)
            df2 = mutate_data(df1, 0.05)
            db.write("t", df1)
            db.write("t", df2)

            # Stats only (no key_columns)
            med_stats, _, _ = time_fn(
                lambda: db.diff("t"),
                iterations=3,
            )
            print_row("Stats only (no key)", fmt_time(med_stats))

            # Full row diff
            med_full, _, result = time_fn(
                lambda: db.diff("t", key_columns=["id"]),
                iterations=3,
            )
            print_row(
                "Full row diff",
                fmt_time(med_full),
                f"{fmt_rate(n_rows, med_full)}  modified={result.rows.modified.num_rows}",
            )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Rhizo diff benchmark")
    parser.add_argument("--quick", action="store_true", help="Fast mode for CI")
    args = parser.parse_args()

    print("=" * 70)
    print("  Rhizo Diff Benchmark")
    print("=" * 70)

    t0 = time.perf_counter()

    bench_row_scaling(args.quick)
    bench_change_pct(args.quick)
    bench_merkle_skip(args.quick)
    bench_column_scaling(args.quick)
    bench_semantic_overhead(args.quick)
    bench_db_diff(args.quick)

    elapsed = time.perf_counter() - t0
    print(f"\n{'=' * 70}")
    print(f"  Total benchmark time: {elapsed:.1f}s")
    print(f"{'=' * 70}")


if __name__ == "__main__":
    main()
