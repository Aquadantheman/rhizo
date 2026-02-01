#!/usr/bin/env python3
"""
GC benchmark: Version deletion and chunk sweep performance.

Measures:
  1. Version count scaling (10/100/1K/10K versions per table)
  2. Table count scaling (1/10/50/100 tables x 100 versions)
  3. Chunk sweep scaling (1K/10K/100K chunks)
  4. Protection overhead (0/10/50 branches)
  5. Bytes freed tracking
  6. AutoGC overhead (background thread cost)

Usage:
    python benchmarks/gc_benchmark.py
    python benchmarks/gc_benchmark.py --quick   # Fast mode for CI
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
from rhizo.gc import GCPolicy, GCResult, GarbageCollector, AutoGC
from rhizo.writer import TableWriter
import _rhizo


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def generate_data(num_rows: int, seed: int = 42) -> pd.DataFrame:
    """Generate a mixed-type dataset."""
    rng = np.random.default_rng(seed)
    return pd.DataFrame({
        "id": np.arange(num_rows, dtype=np.int64),
        "value": rng.standard_normal(num_rows),
        "category": rng.choice(["A", "B", "C", "D", "E"], num_rows),
    })


def time_fn(fn, warmup: int = 0, iterations: int = 3):
    """Time a function, returning (median_seconds, all_times)."""
    for _ in range(warmup):
        fn()
    times = []
    for _ in range(iterations):
        t0 = time.perf_counter()
        result = fn()
        times.append(time.perf_counter() - t0)
    return statistics.median(times), times, result


def make_env(base_dir):
    """Create low-level GC environment."""
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


def write_versions(store, catalog, table_name, num_versions, rows_per_version=100):
    """Write N versions of a table."""
    writer = TableWriter(store, catalog)
    for i in range(num_versions):
        df = generate_data(rows_per_version, seed=i)
        writer.write(table_name, df)


def fmt_time(seconds):
    """Format time for display."""
    if seconds < 0.001:
        return f"{seconds * 1_000_000:.0f}us"
    elif seconds < 1.0:
        return f"{seconds * 1000:.1f}ms"
    else:
        return f"{seconds:.2f}s"


def fmt_rate(count, seconds):
    """Format operations per second."""
    if seconds == 0:
        return "inf"
    rate = count / seconds
    if rate > 1_000_000:
        return f"{rate / 1_000_000:.1f}M/s"
    elif rate > 1_000:
        return f"{rate / 1_000:.1f}K/s"
    return f"{rate:.0f}/s"


def print_header(title):
    """Print a section header."""
    print(f"\n{'=' * 70}")
    print(f"  {title}")
    print(f"{'=' * 70}")


def print_row(label, value, extra=""):
    """Print a formatted result row."""
    print(f"  {label:<40} {value:>12}  {extra}")


# ---------------------------------------------------------------------------
# Benchmarks
# ---------------------------------------------------------------------------

def bench_version_count_scaling(quick=False):
    """Benchmark GC over increasing version counts per table."""
    print_header("1. Version Count Scaling (single table)")
    counts = [10, 100, 1000] if quick else [10, 100, 1000, 10000]

    for n in counts:
        tmp = tempfile.mkdtemp(prefix="gc_bench_vcnt_")
        try:
            store, catalog, branch_mgr = make_env(tmp)
            write_versions(store, catalog, "bench", n, rows_per_version=50)

            gc = GarbageCollector(catalog, store, branch_mgr)
            keep = max(1, n // 5)  # Keep 20%

            med, times, result = time_fn(
                lambda: gc.collect(GCPolicy(max_versions_per_table=keep)),
                iterations=1,
            )
            per_version = med / max(1, result.versions_deleted) if result.versions_deleted else 0
            print_row(
                f"{n} versions (keep {keep})",
                fmt_time(med),
                f"deleted={result.versions_deleted}  per_ver={fmt_time(per_version)}  freed={result.bytes_freed:,}B"
            )
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


def bench_table_count_scaling(quick=False):
    """Benchmark GC over increasing table counts."""
    print_header("2. Table Count Scaling (100 versions each)")
    counts = [1, 10, 50] if quick else [1, 10, 50, 100]

    for n_tables in counts:
        tmp = tempfile.mkdtemp(prefix="gc_bench_tcnt_")
        try:
            store, catalog, branch_mgr = make_env(tmp)
            for t in range(n_tables):
                write_versions(store, catalog, f"table_{t}", 100, rows_per_version=50)

            gc = GarbageCollector(catalog, store, branch_mgr)

            med, times, result = time_fn(
                lambda: gc.collect(GCPolicy(max_versions_per_table=10)),
                iterations=1,
            )
            per_table = med / n_tables
            print_row(
                f"{n_tables} tables x 100 versions",
                fmt_time(med),
                f"deleted={result.versions_deleted}  per_table={fmt_time(per_table)}  freed={result.bytes_freed:,}B"
            )
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


def bench_chunk_sweep_scaling(quick=False):
    """Benchmark chunk sweep with varying referenced percentages."""
    print_header("3. Chunk Sweep Scaling")
    # We'll create versions with unique data to get unique chunks, then GC
    counts = [100, 1000] if quick else [100, 1000, 5000]

    for n_versions in counts:
        tmp = tempfile.mkdtemp(prefix="gc_bench_sweep_")
        try:
            store, catalog, branch_mgr = make_env(tmp)
            write_versions(store, catalog, "sweep_test", n_versions, rows_per_version=100)

            all_hashes = store.list_chunk_hashes()
            n_chunks = len(all_hashes)

            gc = GarbageCollector(catalog, store, branch_mgr)

            # GC keeping only 10% of versions
            keep = max(1, n_versions // 10)
            med, times, result = time_fn(
                lambda: gc.collect(GCPolicy(max_versions_per_table=keep)),
                iterations=1,
            )
            print_row(
                f"{n_versions} versions -> {n_chunks} chunks",
                fmt_time(med),
                f"swept={result.chunks_deleted}  freed={result.bytes_freed:,}B  rate={fmt_rate(result.chunks_deleted, med)}"
            )
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


def bench_protection_overhead(quick=False):
    """Benchmark cost of collecting protected versions with many branches."""
    print_header("4. Protection Collection Overhead")
    branch_counts = [0, 10, 50] if quick else [0, 10, 50, 100]

    for n_branches in branch_counts:
        tmp = tempfile.mkdtemp(prefix="gc_bench_prot_")
        try:
            store, catalog, branch_mgr = make_env(tmp)
            write_versions(store, catalog, "prot_test", 200, rows_per_version=20)

            # Create branches pointing at various versions
            for b in range(n_branches):
                name = f"branch_{b}"
                try:
                    branch_mgr.create(name, from_branch="main")
                except (ValueError, OSError):
                    pass
                branch_mgr.update_head(name, "prot_test", (b % 200) + 1)

            gc = GarbageCollector(catalog, store, branch_mgr)

            # Time just the protection collection
            def collect_protected():
                return gc._collect_protected_versions()

            med, times, result = time_fn(collect_protected, iterations=3)
            n_protected = sum(len(v) for v in result.values())
            print_row(
                f"{n_branches} branches",
                fmt_time(med),
                f"protected={n_protected} versions"
            )
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


def bench_bytes_freed(quick=False):
    """Benchmark actual disk space reclaimed."""
    print_header("5. Disk Space Reclamation")
    row_counts = [1000, 10000] if quick else [1000, 10000, 100000]

    for n_rows in row_counts:
        tmp = tempfile.mkdtemp(prefix="gc_bench_bytes_")
        try:
            store, catalog, branch_mgr = make_env(tmp)
            write_versions(store, catalog, "bytes_test", 20, rows_per_version=n_rows)

            # Measure disk before
            def dir_size(path):
                total = 0
                for dirpath, dirnames, filenames in os.walk(path):
                    for f in filenames:
                        fp = os.path.join(dirpath, f)
                        total += os.path.getsize(fp)
                return total

            before = dir_size(tmp)

            gc = GarbageCollector(catalog, store, branch_mgr)
            result = gc.collect(GCPolicy(max_versions_per_table=2))

            after = dir_size(tmp)
            actual_freed = before - after

            print_row(
                f"20 versions x {n_rows:,} rows",
                f"{actual_freed:,}B freed",
                f"reported={result.bytes_freed:,}B  before={before:,}B  after={after:,}B"
            )
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


def bench_auto_gc_overhead():
    """Benchmark AutoGC background thread CPU overhead."""
    print_header("6. AutoGC Background Thread Overhead")

    tmp = tempfile.mkdtemp(prefix="gc_bench_auto_")
    try:
        store, catalog, branch_mgr = make_env(tmp)
        write_versions(store, catalog, "auto_test", 50, rows_per_version=100)

        gc = GarbageCollector(catalog, store, branch_mgr)
        auto = AutoGC(gc, GCPolicy(max_versions_per_table=10), interval_seconds=0.5)

        auto.start()
        time.sleep(3.0)  # Let it run several cycles
        auto.stop(timeout=10.0)

        result = auto.last_result
        if result:
            print_row("AutoGC last run", fmt_time(result.elapsed_seconds),
                       f"deleted={result.versions_deleted}  chunks={result.chunks_deleted}")
        else:
            print_row("AutoGC", "no runs completed", "")

        print_row("Thread cleanup", "clean" if not auto.is_running else "STILL RUNNING", "")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def bench_db_gc_integration(quick=False):
    """Benchmark db.gc() end-to-end."""
    print_header("7. db.gc() End-to-End Integration")
    version_counts = [50, 200] if quick else [50, 200, 1000]

    for n_versions in version_counts:
        tmp = tempfile.mkdtemp(prefix="gc_bench_db_")
        try:
            with rhizo.open(tmp) as db:
                for i in range(n_versions):
                    db.write("integration", generate_data(100, seed=i))

                t0 = time.perf_counter()
                result = db.gc(max_versions_per_table=5)
                elapsed = time.perf_counter() - t0

                print_row(
                    f"db.gc() over {n_versions} versions",
                    fmt_time(elapsed),
                    f"deleted={result.versions_deleted}  chunks={result.chunks_deleted}  freed={result.bytes_freed:,}B"
                )
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="GC benchmark")
    parser.add_argument("--quick", action="store_true", help="Quick mode (smaller scales)")
    args = parser.parse_args()

    print("=" * 70)
    print("  Rhizo GC Benchmark")
    print("=" * 70)

    bench_version_count_scaling(quick=args.quick)
    bench_table_count_scaling(quick=args.quick)
    bench_chunk_sweep_scaling(quick=args.quick)
    bench_protection_overhead(quick=args.quick)
    bench_bytes_freed(quick=args.quick)
    bench_auto_gc_overhead()
    bench_db_gc_integration(quick=args.quick)

    print(f"\n{'=' * 70}")
    print("  Benchmark complete")
    print(f"{'=' * 70}\n")


if __name__ == "__main__":
    main()
