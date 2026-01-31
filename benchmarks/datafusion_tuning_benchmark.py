#!/usr/bin/env python3
"""
DataFusion config tuning experiment.

Tests each SessionConfig knob in isolation against a 10M row dataset
to find which settings close the aggregation gap vs DuckDB.
"""

import os
import sys
import time
import statistics
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "python"))

import pyarrow as pa
import datafusion
import duckdb


# ── Queries ──────────────────────────────────────────────────────────

QUERIES = {
    "read": "SELECT * FROM test",
    "filter": "SELECT * FROM test WHERE id < {cutoff}",
    "agg": "SELECT category, COUNT(*), AVG(amount), SUM(score) FROM test GROUP BY category",
    "complex": (
        "SELECT category, status, COUNT(*) as cnt, AVG(amount) as avg_amt "
        "FROM test WHERE score > 50 AND count > 100 "
        "GROUP BY category, status ORDER BY avg_amt DESC"
    ),
}


# ── Helpers ──────────────────────────────────────────────────────────

def generate_data(num_rows: int, seed: int = 42) -> pa.Table:
    rng = np.random.default_rng(seed)
    df = pd.DataFrame({
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
    return pa.Table.from_pandas(df)


def bench_query(ctx, sql, warmup=2, iterations=5):
    for _ in range(warmup):
        ctx.sql(sql).collect()
    times = []
    for _ in range(iterations):
        t0 = time.perf_counter()
        ctx.sql(sql).collect()
        times.append((time.perf_counter() - t0) * 1000)
    return statistics.median(times)


def run_queries(ctx, scale, label=""):
    cutoff = scale // 20
    results = {}
    for name, sql in QUERIES.items():
        q = sql.format(cutoff=cutoff) if "{cutoff}" in sql else sql
        results[name] = bench_query(ctx, q)
    return results


def make_ctx(config=None):
    if config is None:
        return datafusion.SessionContext()
    return datafusion.SessionContext(config)


def register_table(ctx, table):
    ctx.register_record_batches("test", [table.to_batches()])


def print_results(label, results, baseline=None):
    print(f"\n  {label}:")
    for name in ["read", "filter", "agg", "complex"]:
        ms = results[name]
        suffix = ""
        if baseline and name in baseline:
            ratio = baseline[name] / ms
            if ratio > 1:
                suffix = f"  ({ratio:.2f}x faster)"
            else:
                suffix = f"  ({1/ratio:.2f}x slower)"
        print(f"    {name:<12} {ms:>8.1f}ms{suffix}")


# ── Experiments ──────────────────────────────────────────────────────

def main():
    SCALE = 10_000_000

    print("=" * 70)
    print("DATAFUSION CONFIG TUNING EXPERIMENT")
    print("=" * 70)
    print(f"Scale: {SCALE:,} rows")
    print(f"CPU count: {os.cpu_count()}")
    print(f"DataFusion version: {datafusion.__version__}")

    print(f"\nGenerating {SCALE // 1_000_000}M rows...", end=" ", flush=True)
    t0 = time.perf_counter()
    table = generate_data(SCALE)
    print(f"done ({(time.perf_counter() - t0) * 1000:.0f}ms, "
          f"{table.nbytes / 1e6:.0f}MB)")

    all_results = {}

    # ── Step 1: Baseline ─────────────────────────────────────────────
    print(f"\n{'-' * 70}")
    print("STEP 1: BASELINE (bare SessionContext)")
    print(f"{'-' * 70}")
    ctx = make_ctx()
    register_table(ctx, table)
    baseline = run_queries(ctx, SCALE)
    print_results("Baseline", baseline)
    all_results["baseline"] = baseline
    del ctx

    # ── Step 2: Batch size sweep ─────────────────────────────────────
    print(f"\n{'-' * 70}")
    print("STEP 2: BATCH SIZE SWEEP")
    print(f"{'-' * 70}")
    for batch_size in [8192, 32768, 65536, 131072, 262144]:
        config = datafusion.SessionConfig().with_batch_size(batch_size)
        ctx = make_ctx(config)
        register_table(ctx, table)
        results = run_queries(ctx, SCALE)
        label = f"batch_size={batch_size:,}"
        print_results(label, results, baseline)
        all_results[f"batch_{batch_size}"] = results
        del ctx

    # ── Step 3: Target partitions sweep ──────────────────────────────
    print(f"\n{'-' * 70}")
    print("STEP 3: TARGET PARTITIONS SWEEP")
    print(f"{'-' * 70}")
    for partitions in [1, 4, 8, 16, os.cpu_count()]:
        config = datafusion.SessionConfig().with_target_partitions(partitions)
        ctx = make_ctx(config)
        register_table(ctx, table)
        results = run_queries(ctx, SCALE)
        label = f"target_partitions={partitions}"
        print_results(label, results, baseline)
        all_results[f"partitions_{partitions}"] = results
        del ctx

    # ── Step 4: Filter pushdown + reorder ────────────────────────────
    print(f"\n{'-' * 70}")
    print("STEP 4: FILTER PUSHDOWN + REORDER")
    print(f"{'-' * 70}")
    config = datafusion.SessionConfig()
    config.set("datafusion.execution.parquet.pushdown_filters", "true")
    config.set("datafusion.execution.parquet.reorder_filters", "true")
    ctx = make_ctx(config)
    register_table(ctx, table)
    results = run_queries(ctx, SCALE)
    print_results("pushdown + reorder filters", results, baseline)
    all_results["filter_pushdown"] = results
    del ctx

    # ── Step 5: Repartition aggregations explicitly ──────────────────
    print(f"\n{'-' * 70}")
    print("STEP 5: REPARTITION AGGREGATIONS")
    print(f"{'-' * 70}")
    config = (datafusion.SessionConfig()
              .with_repartition_aggregations(True)
              .with_target_partitions(os.cpu_count()))
    ctx = make_ctx(config)
    register_table(ctx, table)
    results = run_queries(ctx, SCALE)
    print_results("repartition_agg + full partitions", results, baseline)
    all_results["repartition_agg"] = results
    del ctx

    # ── Step 6: Combined optimal ─────────────────────────────────────
    print(f"\n{'-' * 70}")
    print("STEP 6: COMBINED OPTIMAL")
    print(f"{'-' * 70}")
    config = (datafusion.SessionConfig()
              .with_batch_size(131072)
              .with_target_partitions(os.cpu_count())
              .with_repartition_aggregations(True)
              .with_repartition_joins(True)
              .with_repartition_sorts(True))
    config.set("datafusion.execution.parquet.pushdown_filters", "true")
    config.set("datafusion.execution.parquet.reorder_filters", "true")
    ctx = make_ctx(config)
    register_table(ctx, table)
    combined = run_queries(ctx, SCALE)
    print_results("COMBINED OPTIMAL", combined, baseline)
    all_results["combined"] = combined
    del ctx

    # ── Step 7: DuckDB comparison ────────────────────────────────────
    print(f"\n{'-' * 70}")
    print("STEP 7: DUCKDB COMPARISON")
    print(f"{'-' * 70}")
    conn = duckdb.connect()
    conn.register("test_arrow", table)
    conn.execute("CREATE TABLE test AS SELECT * FROM test_arrow")

    cutoff = SCALE // 20
    duck_results = {}
    for name, sql in QUERIES.items():
        q = sql.format(cutoff=cutoff) if "{cutoff}" in sql else sql
        # warmup
        for _ in range(2):
            conn.execute(q).df()
        times = []
        for _ in range(5):
            t0 = time.perf_counter()
            conn.execute(q).df()
            times.append((time.perf_counter() - t0) * 1000)
        duck_results[name] = statistics.median(times)
    conn.close()

    print_results("DuckDB", duck_results)
    all_results["duckdb"] = duck_results

    # ── Final comparison ─────────────────────────────────────────────
    print(f"\n{'=' * 70}")
    print("FINAL COMPARISON: Baseline vs Tuned vs DuckDB")
    print(f"{'=' * 70}")
    print(f"\n  {'Op':<12} {'Baseline':>10} {'Tuned':>10} {'DuckDB':>10} {'Tuned vs DuckDB':>16}")
    print(f"  {'-' * 60}")
    for name in ["read", "filter", "agg", "complex"]:
        b = baseline[name]
        t = combined[name]
        d = duck_results[name]
        ratio = d / t if t > 0 else 0
        winner = f"Rhizo {ratio:.1f}x" if ratio > 1 else f"DuckDB {1/ratio:.1f}x"
        print(f"  {name:<12} {b:>9.1f}ms {t:>9.1f}ms {d:>9.1f}ms {winner:>16}")

    improvement = baseline["agg"] / combined["agg"] if combined["agg"] > 0 else 0
    print(f"\n  Aggregation improvement from tuning: {improvement:.2f}x")

    gap_before = duck_results["agg"] / baseline["agg"]
    gap_after = duck_results["agg"] / combined["agg"]
    print(f"  Agg gap vs DuckDB: {gap_before:.2f}x before -> {gap_after:.2f}x after")


if __name__ == "__main__":
    main()
