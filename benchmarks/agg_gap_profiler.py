#!/usr/bin/env python3
"""
Aggregation gap profiler.

Digs into WHY DataFusion is 1.3x slower than DuckDB on aggregation queries
at 10M rows. Tests specific hypotheses:

1. Query plan differences (EXPLAIN analysis)
2. Batch size impact on aggregation specifically
3. Hash table overhead (cardinality sweep)
4. Column encoding / type impact
5. Registration method (single batch vs many small batches)
6. Thread scaling for aggregation
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


SCALE = 10_000_000


def generate_data(num_rows, seed=42):
    rng = np.random.default_rng(seed)
    return pa.Table.from_pandas(pd.DataFrame({
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
    }))


def bench(fn, warmup=2, iterations=7):
    for _ in range(warmup):
        fn()
    times = []
    for _ in range(iterations):
        t0 = time.perf_counter()
        fn()
        times.append((time.perf_counter() - t0) * 1000)
    return statistics.median(times)


def section(title):
    print(f"\n{'=' * 70}")
    print(title)
    print(f"{'=' * 70}")


def main():
    print("=" * 70)
    print("AGGREGATION GAP PROFILER")
    print("=" * 70)
    print(f"Scale: {SCALE:,} rows")
    print(f"CPU count: {os.cpu_count()}")
    print(f"DataFusion: {datafusion.__version__}")

    print(f"\nGenerating data...", end=" ", flush=True)
    table = generate_data(SCALE)
    print(f"done ({table.nbytes / 1e6:.0f}MB)")

    # Queries we care about
    AGG_Q = "SELECT category, COUNT(*), AVG(amount), SUM(score) FROM test GROUP BY category"
    COMPLEX_Q = ("SELECT category, status, COUNT(*) as cnt, AVG(amount) as avg_amt "
                 "FROM test WHERE score > 50 AND count > 100 "
                 "GROUP BY category, status ORDER BY avg_amt DESC")

    # ── 1. DuckDB baseline ──────────────────────────────────────────────
    section("1. DUCKDB BASELINE")
    conn = duckdb.connect()
    conn.register("test_arrow", table)
    conn.execute("CREATE TABLE test AS SELECT * FROM test_arrow")

    duck_agg = bench(lambda: conn.execute(AGG_Q).df())
    duck_complex = bench(lambda: conn.execute(COMPLEX_Q).df())
    print(f"  Aggregation:    {duck_agg:.1f}ms")
    print(f"  Complex:        {duck_complex:.1f}ms")

    # DuckDB EXPLAIN
    print(f"\n  DuckDB EXPLAIN (agg):")
    plan = conn.execute(f"EXPLAIN {AGG_Q}").df()
    for _, row in plan.iterrows():
        for line in str(row.iloc[-1]).split("\n"):
            print(f"    {line.encode('ascii', 'replace').decode()}")

    conn.close()

    # ── 2. DataFusion default (current Rhizo) ───────────────────────────
    section("2. DATAFUSION DEFAULT (current Rhizo config)")
    ctx = datafusion.SessionContext()
    ctx.register_record_batches("test", [table.to_batches()])

    df_agg = bench(lambda: ctx.sql(AGG_Q).collect())
    df_complex = bench(lambda: ctx.sql(COMPLEX_Q).collect())
    print(f"  Aggregation:    {df_agg:.1f}ms (vs DuckDB: {df_agg/duck_agg:.2f}x)")
    print(f"  Complex:        {df_complex:.1f}ms (vs DuckDB: {df_complex/duck_complex:.2f}x)")

    # DataFusion EXPLAIN
    print(f"\n  DataFusion EXPLAIN (agg):")
    plan_batches = ctx.sql(f"EXPLAIN {AGG_Q}").collect()
    plan_table = pa.Table.from_batches(plan_batches)
    for i in range(plan_table.num_rows):
        plan_type = plan_table.column("plan_type")[i].as_py()
        plan_text = plan_table.column("plan")[i].as_py()
        print(f"    [{plan_type}]")
        for line in plan_text.split("\n"):
            print(f"      {line}")

    print(f"\n  DataFusion EXPLAIN (complex):")
    plan_batches = ctx.sql(f"EXPLAIN {COMPLEX_Q}").collect()
    plan_table = pa.Table.from_batches(plan_batches)
    for i in range(plan_table.num_rows):
        plan_type = plan_table.column("plan_type")[i].as_py()
        plan_text = plan_table.column("plan")[i].as_py()
        print(f"    [{plan_type}]")
        for line in plan_text.split("\n"):
            print(f"      {line}")
    del ctx

    # ── 3. Batch size impact on aggregation ─────────────────────────────
    section("3. BATCH SIZE IMPACT ON AGGREGATION")
    print(f"  Testing how batch size affects aggregation specifically...")

    for batch_size in [4096, 8192, 16384, 32768, 65536, 131072, 262144]:
        config = datafusion.SessionConfig().with_batch_size(batch_size)
        ctx = make_tuned_ctx(config, table)
        agg_ms = bench(lambda: ctx.sql(AGG_Q).collect())
        ratio = agg_ms / duck_agg
        print(f"  batch_size={batch_size:>7,}  agg: {agg_ms:>7.1f}ms  ({ratio:.2f}x DuckDB)")
        del ctx

    # ── 4. Target partitions impact ─────────────────────────────────────
    section("4. TARGET PARTITIONS IMPACT ON AGGREGATION")
    for partitions in [1, 2, 4, 8, 12, 16, os.cpu_count(), os.cpu_count() * 2]:
        config = datafusion.SessionConfig().with_target_partitions(partitions)
        ctx = make_tuned_ctx(config, table)
        agg_ms = bench(lambda: ctx.sql(AGG_Q).collect())
        ratio = agg_ms / duck_agg
        print(f"  partitions={partitions:>3}  agg: {agg_ms:>7.1f}ms  ({ratio:.2f}x DuckDB)")
        del ctx

    # ── 5. Registration method: single batch vs chunked ─────────────────
    section("5. REGISTRATION METHOD: BATCH CHUNKING")
    print("  Testing if how we chunk the Arrow table for registration matters...")

    for chunk_size in [SCALE, SCALE // 10, SCALE // 100, 100_000, 50_000]:
        batches = table.to_batches(max_chunksize=chunk_size)
        ctx = datafusion.SessionContext()
        ctx.register_record_batches("test", [batches])
        agg_ms = bench(lambda: ctx.sql(AGG_Q).collect())
        ratio = agg_ms / duck_agg
        n_batches = len(batches)
        print(f"  chunk={chunk_size:>10,} ({n_batches:>4} batches)  agg: {agg_ms:>7.1f}ms  ({ratio:.2f}x DuckDB)")
        del ctx

    # ── 6. Cardinality sweep ────────────────────────────────────────────
    section("6. CARDINALITY SWEEP (group by key count)")
    print("  Testing if hash table cardinality affects the gap...")

    rng = np.random.default_rng(42)
    for n_groups in [4, 10, 100, 1000, 10000, 100000]:
        # Create a table with exactly n_groups distinct categories
        cat_col = pa.array(rng.integers(0, n_groups, SCALE), type=pa.int64())
        amount_col = table.column("amount")
        score_col = table.column("score")
        test_table = pa.table({"cat": cat_col, "amount": amount_col, "score": score_col})

        q = "SELECT cat, COUNT(*), AVG(amount), SUM(score) FROM test GROUP BY cat"

        # DataFusion
        ctx = datafusion.SessionContext()
        ctx.register_record_batches("test", [test_table.to_batches()])
        df_ms = bench(lambda: ctx.sql(q).collect())

        # DuckDB
        conn = duckdb.connect()
        conn.register("test_arrow", test_table)
        conn.execute("CREATE TABLE test AS SELECT * FROM test_arrow")
        dk_ms = bench(lambda: conn.execute(q).df())
        conn.close()

        ratio = df_ms / dk_ms
        print(f"  groups={n_groups:>7,}  DF: {df_ms:>7.1f}ms  DuckDB: {dk_ms:>7.1f}ms  ratio: {ratio:.2f}x")
        del ctx

    # ── 7. Combined best config ─────────────────────────────────────────
    section("7. COMBINED BEST CONFIG")
    config = (datafusion.SessionConfig()
              .with_batch_size(65536)
              .with_target_partitions(os.cpu_count())
              .with_repartition_aggregations(True)
              .with_repartition_joins(True)
              .with_repartition_sorts(True))
    config.set("datafusion.execution.parquet.pushdown_filters", "true")
    config.set("datafusion.execution.parquet.reorder_filters", "true")

    ctx = datafusion.SessionContext(config)
    ctx.register_record_batches("test", [table.to_batches()])

    best_agg = bench(lambda: ctx.sql(AGG_Q).collect())
    best_complex = bench(lambda: ctx.sql(COMPLEX_Q).collect())

    print(f"  Aggregation:    {best_agg:.1f}ms (vs DuckDB: {best_agg/duck_agg:.2f}x)")
    print(f"  Complex:        {best_complex:.1f}ms (vs DuckDB: {best_complex/duck_complex:.2f}x)")

    # EXPLAIN for tuned config
    print(f"\n  EXPLAIN (tuned agg):")
    plan_batches = ctx.sql(f"EXPLAIN {AGG_Q}").collect()
    plan_table = pa.Table.from_batches(plan_batches)
    for i in range(plan_table.num_rows):
        plan_type = plan_table.column("plan_type")[i].as_py()
        plan_text = plan_table.column("plan")[i].as_py()
        print(f"    [{plan_type}]")
        for line in plan_text.split("\n"):
            print(f"      {line}")
    del ctx

    # ── 8. Isolate aggregation components ───────────────────────────────
    section("8. ISOLATE AGGREGATION COMPONENTS")
    print("  Breaking down aggregation into sub-operations...")

    ctx = datafusion.SessionContext()
    ctx.register_record_batches("test", [table.to_batches()])

    # Just COUNT (no hash table for grouping needed at low cardinality)
    q_count = "SELECT COUNT(*) FROM test"
    ms_count = bench(lambda: ctx.sql(q_count).collect())
    print(f"  COUNT(*) only:              {ms_count:>7.1f}ms")

    # COUNT with GROUP BY (hash table needed)
    q_count_group = "SELECT category, COUNT(*) FROM test GROUP BY category"
    ms_count_group = bench(lambda: ctx.sql(q_count_group).collect())
    print(f"  COUNT(*) + GROUP BY:        {ms_count_group:>7.1f}ms")

    # AVG only (needs accumulation)
    q_avg = "SELECT AVG(amount) FROM test"
    ms_avg = bench(lambda: ctx.sql(q_avg).collect())
    print(f"  AVG(amount) only:           {ms_avg:>7.1f}ms")

    # AVG + GROUP BY
    q_avg_group = "SELECT category, AVG(amount) FROM test GROUP BY category"
    ms_avg_group = bench(lambda: ctx.sql(q_avg_group).collect())
    print(f"  AVG(amount) + GROUP BY:     {ms_avg_group:>7.1f}ms")

    # SUM only
    q_sum = "SELECT SUM(score) FROM test"
    ms_sum = bench(lambda: ctx.sql(q_sum).collect())
    print(f"  SUM(score) only:            {ms_sum:>7.1f}ms")

    # Full agg (COUNT + AVG + SUM + GROUP BY)
    ms_full = bench(lambda: ctx.sql(AGG_Q).collect())
    print(f"  Full agg (3 funcs + GROUP): {ms_full:>7.1f}ms")

    # Same breakdown for DuckDB
    conn = duckdb.connect()
    conn.register("test_arrow", table)
    conn.execute("CREATE TABLE test AS SELECT * FROM test_arrow")

    dk_count = bench(lambda: conn.execute(q_count).df())
    dk_count_group = bench(lambda: conn.execute(q_count_group).df())
    dk_avg = bench(lambda: conn.execute(q_avg).df())
    dk_avg_group = bench(lambda: conn.execute(q_avg_group).df())
    dk_sum = bench(lambda: conn.execute(q_sum).df())
    dk_full = bench(lambda: conn.execute(AGG_Q).df())
    conn.close()

    print(f"\n  {'Operation':<32} {'DF':>8} {'DuckDB':>8} {'Ratio':>8}")
    print(f"  {'-'*58}")
    for label, df_val, dk_val in [
        ("COUNT(*)", ms_count, dk_count),
        ("COUNT(*) + GROUP BY", ms_count_group, dk_count_group),
        ("AVG(amount)", ms_avg, dk_avg),
        ("AVG(amount) + GROUP BY", ms_avg_group, dk_avg_group),
        ("SUM(score)", ms_sum, dk_sum),
        ("Full agg (3 func + GROUP)", ms_full, dk_full),
    ]:
        ratio = df_val / dk_val if dk_val > 0 else 0
        print(f"  {label:<32} {df_val:>7.1f}ms {dk_val:>7.1f}ms {ratio:>7.2f}x")

    del ctx

    # ── 9. Filter-then-aggregate timing ─────────────────────────────────
    section("9. FILTER-THEN-AGGREGATE (complex query breakdown)")
    ctx = datafusion.SessionContext()
    ctx.register_record_batches("test", [table.to_batches()])

    # Just filter
    q_filter = "SELECT * FROM test WHERE score > 50 AND count > 100"
    ms_filter = bench(lambda: ctx.sql(q_filter).collect())

    # Filter + group (no ORDER BY)
    q_filter_group = ("SELECT category, status, COUNT(*), AVG(amount) "
                      "FROM test WHERE score > 50 AND count > 100 "
                      "GROUP BY category, status")
    ms_filter_group = bench(lambda: ctx.sql(q_filter_group).collect())

    # Full complex (filter + group + ORDER BY)
    ms_complex = bench(lambda: ctx.sql(COMPLEX_Q).collect())

    print(f"  Filter only:          {ms_filter:>7.1f}ms")
    print(f"  Filter + GROUP BY:    {ms_filter_group:>7.1f}ms (agg overhead: {ms_filter_group - ms_filter:.1f}ms)")
    print(f"  Filter + GROUP + SORT:{ms_complex:>7.1f}ms (sort overhead: {ms_complex - ms_filter_group:.1f}ms)")
    del ctx

    # ── Summary ─────────────────────────────────────────────────────────
    section("SUMMARY")
    print(f"  DuckDB agg:           {duck_agg:.1f}ms")
    print(f"  DF default agg:       {df_agg:.1f}ms ({df_agg/duck_agg:.2f}x DuckDB)")
    print(f"  DF tuned agg:         {best_agg:.1f}ms ({best_agg/duck_agg:.2f}x DuckDB)")
    print(f"")
    print(f"  DuckDB complex:       {duck_complex:.1f}ms")
    print(f"  DF default complex:   {df_complex:.1f}ms ({df_complex/duck_complex:.2f}x DuckDB)")
    print(f"  DF tuned complex:     {best_complex:.1f}ms ({best_complex/duck_complex:.2f}x DuckDB)")

    gap_improvement = (df_agg / duck_agg) - (best_agg / duck_agg)
    print(f"\n  Tuning closes gap by: {gap_improvement:.2f}x ratio points")
    if best_agg <= duck_agg:
        print(f"  RESULT: Gap CLOSED - DataFusion matches or beats DuckDB on agg!")
    else:
        remaining = best_agg / duck_agg
        print(f"  RESULT: Remaining gap is {remaining:.2f}x - likely engine-level limitation")


def make_tuned_ctx(config, table):
    ctx = datafusion.SessionContext(config)
    ctx.register_record_batches("test", [table.to_batches()])
    return ctx


if __name__ == "__main__":
    main()
