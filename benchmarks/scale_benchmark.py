#!/usr/bin/env python3
"""
Scale benchmark: Test Rhizo vs DuckDB at 1M, 5M, 10M rows.

Measures write, read, filter, aggregation, complex query, and JOIN
performance to find where scaling breaks down.
"""

import os
import sys
import json
import time
import shutil
import tempfile
import statistics
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "python"))

import duckdb
from _rhizo import PyChunkStore, PyCatalog
from rhizo.engine import QueryEngine


def generate_data(num_rows: int, seed: int = 42) -> pd.DataFrame:
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


def generate_join_data(num_users: int, num_orders: int, seed: int = 42):
    rng = np.random.default_rng(seed)
    users = pd.DataFrame({
        "user_id": np.arange(num_users, dtype=np.int64),
        "username": [f"user_{i}" for i in range(num_users)],
        "tier": rng.choice(["free", "pro", "enterprise"], num_users),
    })
    orders = pd.DataFrame({
        "order_id": np.arange(num_orders, dtype=np.int64),
        "user_id": rng.integers(0, num_users, num_orders),
        "amount": rng.uniform(1, 1000, num_orders),
        "status": rng.choice(["pending", "shipped", "delivered"], num_orders),
    })
    return users, orders


def benchmark(fn, warmup=2, iterations=5):
    for _ in range(warmup):
        fn()
    times = []
    for _ in range(iterations):
        t0 = time.perf_counter()
        fn()
        times.append((time.perf_counter() - t0) * 1000)
    return statistics.median(times)


def get_dir_size(path):
    if os.path.isfile(path):
        return os.path.getsize(path)
    total = 0
    for dirpath, _, filenames in os.walk(path):
        for f in filenames:
            total += os.path.getsize(os.path.join(dirpath, f))
    return total


def run_scale(scale: int, temp_dir: str):
    label = f"{scale // 1_000_000}M" if scale >= 1_000_000 else f"{scale // 1000}K"
    print(f"\n{'='*70}")
    print(f"SCALE: {label} rows ({scale:,})")
    print(f"{'='*70}")

    print(f"  Generating data...", end=" ", flush=True)
    t0 = time.perf_counter()
    df = generate_data(scale)
    gen_time = (time.perf_counter() - t0) * 1000
    print(f"done ({gen_time:.0f}ms, {df.memory_usage(deep=True).sum() / 1e6:.0f}MB in memory)")

    results = {"scale": scale, "label": label}

    # --- Rhizo OLAP ---
    print(f"  Rhizo OLAP:")
    chunks_path = os.path.join(temp_dir, f"rhizo_{scale}_chunks")
    catalog_path = os.path.join(temp_dir, f"rhizo_{scale}_catalog")
    store = PyChunkStore(chunks_path)
    catalog = PyCatalog(catalog_path)
    engine = QueryEngine(store, catalog, enable_olap=True)

    # Write
    print(f"    Write...", end=" ", flush=True)
    t0 = time.perf_counter()
    engine.write_table("test", df)
    write_ms = (time.perf_counter() - t0) * 1000
    results["rhizo_write_ms"] = write_ms
    print(f"{write_ms:.1f}ms")

    # Read (SELECT *)
    print(f"    Read (full)...", end=" ", flush=True)
    read_ms = benchmark(lambda: engine.query("SELECT * FROM test"))
    results["rhizo_read_ms"] = read_ms
    print(f"{read_ms:.1f}ms")

    # Filter (5%)
    print(f"    Filter (5%)...", end=" ", flush=True)
    cutoff = scale // 20
    filter_ms = benchmark(lambda: engine.query(f"SELECT * FROM test WHERE id < {cutoff}"))
    results["rhizo_filter_ms"] = filter_ms
    print(f"{filter_ms:.1f}ms")

    # Aggregation
    print(f"    Aggregation...", end=" ", flush=True)
    agg_ms = benchmark(lambda: engine.query(
        "SELECT category, COUNT(*), AVG(amount), SUM(score) FROM test GROUP BY category"
    ))
    results["rhizo_agg_ms"] = agg_ms
    print(f"{agg_ms:.1f}ms")

    # Complex query
    print(f"    Complex query...", end=" ", flush=True)
    complex_ms = benchmark(lambda: engine.query(
        "SELECT category, status, COUNT(*) as cnt, AVG(amount) as avg_amt "
        "FROM test WHERE score > 50 AND count > 100 "
        "GROUP BY category, status ORDER BY avg_amt DESC"
    ))
    results["rhizo_complex_ms"] = complex_ms
    print(f"{complex_ms:.1f}ms")

    # Storage
    rhizo_storage = get_dir_size(chunks_path) + get_dir_size(catalog_path)
    results["rhizo_storage_mb"] = rhizo_storage / 1e6
    print(f"    Storage: {rhizo_storage / 1e6:.1f}MB")

    # --- DuckDB ---
    print(f"  DuckDB:")
    db_path = os.path.join(temp_dir, f"duckdb_{scale}.db")
    conn = duckdb.connect(db_path)

    # Write
    print(f"    Write...", end=" ", flush=True)
    conn.register("df", df)
    t0 = time.perf_counter()
    conn.execute("CREATE OR REPLACE TABLE test AS SELECT * FROM df")
    duck_write_ms = (time.perf_counter() - t0) * 1000
    results["duckdb_write_ms"] = duck_write_ms
    print(f"{duck_write_ms:.1f}ms")

    # Read
    print(f"    Read (full)...", end=" ", flush=True)
    duck_read_ms = benchmark(lambda: conn.execute("SELECT * FROM test").df())
    results["duckdb_read_ms"] = duck_read_ms
    print(f"{duck_read_ms:.1f}ms")

    # Filter
    print(f"    Filter (5%)...", end=" ", flush=True)
    duck_filter_ms = benchmark(lambda: conn.execute(
        f"SELECT * FROM test WHERE id < {cutoff}"
    ).df())
    results["duckdb_filter_ms"] = duck_filter_ms
    print(f"{duck_filter_ms:.1f}ms")

    # Aggregation
    print(f"    Aggregation...", end=" ", flush=True)
    duck_agg_ms = benchmark(lambda: conn.execute(
        "SELECT category, COUNT(*), AVG(amount), SUM(score) FROM test GROUP BY category"
    ).df())
    results["duckdb_agg_ms"] = duck_agg_ms
    print(f"{duck_agg_ms:.1f}ms")

    # Complex query
    print(f"    Complex query...", end=" ", flush=True)
    duck_complex_ms = benchmark(lambda: conn.execute(
        "SELECT category, status, COUNT(*) as cnt, AVG(amount) as avg_amt "
        "FROM test WHERE score > 50 AND count > 100 "
        "GROUP BY category, status ORDER BY avg_amt DESC"
    ).df())
    results["duckdb_complex_ms"] = duck_complex_ms
    print(f"{duck_complex_ms:.1f}ms")

    # Storage
    conn.close()
    duck_storage = get_dir_size(db_path)
    results["duckdb_storage_mb"] = duck_storage / 1e6
    print(f"    Storage: {duck_storage / 1e6:.1f}MB")

    # --- Comparison ---
    print(f"\n  {'Operation':<20} {'Rhizo':>10} {'DuckDB':>10} {'Speedup':>10}")
    print(f"  {'-'*50}")
    for op in ["write", "read", "filter", "agg", "complex"]:
        r = results[f"rhizo_{op}_ms"]
        d = results[f"duckdb_{op}_ms"]
        ratio = d / r if r > 0 else float('inf')
        winner = "Rhizo" if ratio > 1 else "DuckDB"
        print(f"  {op:<20} {r:>9.1f}ms {d:>9.1f}ms {ratio:>8.1f}x ({winner})")

    print(f"  {'storage':<20} {results['rhizo_storage_mb']:>8.1f}MB {results['duckdb_storage_mb']:>8.1f}MB")

    return results


def run_join_scale(num_users: int, num_orders: int, temp_dir: str):
    label = f"{num_users // 1000}K users x {num_orders // 1_000_000}M orders" if num_orders >= 1_000_000 else f"{num_users // 1000}K users x {num_orders // 1000}K orders"
    print(f"\n{'='*70}")
    print(f"JOIN SCALE: {label}")
    print(f"{'='*70}")

    print(f"  Generating data...", end=" ", flush=True)
    users, orders = generate_join_data(num_users, num_orders)
    print("done")

    results = {"users": num_users, "orders": num_orders, "label": label}

    # --- Rhizo ---
    print(f"  Rhizo OLAP JOINs:")
    chunks_path = os.path.join(temp_dir, f"join_rhizo_{num_orders}_chunks")
    catalog_path = os.path.join(temp_dir, f"join_rhizo_{num_orders}_catalog")
    store = PyChunkStore(chunks_path)
    catalog = PyCatalog(catalog_path)
    engine = QueryEngine(store, catalog, enable_olap=True)
    engine.write_table("users", users)
    engine.write_table("orders", orders)

    join_q = "SELECT u.username, o.amount FROM users u JOIN orders o ON u.user_id = o.user_id"
    join_filter_q = join_q + " WHERE o.amount > 500"
    join_agg_q = "SELECT u.tier, COUNT(*) as cnt, SUM(o.amount) as total FROM users u JOIN orders o ON u.user_id = o.user_id GROUP BY u.tier"

    print(f"    Simple JOIN...", end=" ", flush=True)
    r_join = benchmark(lambda: engine.query(join_q))
    print(f"{r_join:.1f}ms")

    print(f"    JOIN + Filter...", end=" ", flush=True)
    r_join_f = benchmark(lambda: engine.query(join_filter_q))
    print(f"{r_join_f:.1f}ms")

    print(f"    JOIN + Aggregate...", end=" ", flush=True)
    r_join_a = benchmark(lambda: engine.query(join_agg_q))
    print(f"{r_join_a:.1f}ms")

    results["rhizo_join_ms"] = r_join
    results["rhizo_join_filter_ms"] = r_join_f
    results["rhizo_join_agg_ms"] = r_join_a

    # --- DuckDB ---
    print(f"  DuckDB JOINs:")
    db_path = os.path.join(temp_dir, f"join_duckdb_{num_orders}.db")
    conn = duckdb.connect(db_path)
    conn.register("users_df", users)
    conn.register("orders_df", orders)
    conn.execute("CREATE TABLE users AS SELECT * FROM users_df")
    conn.execute("CREATE TABLE orders AS SELECT * FROM orders_df")

    print(f"    Simple JOIN...", end=" ", flush=True)
    d_join = benchmark(lambda: conn.execute(join_q).df())
    print(f"{d_join:.1f}ms")

    print(f"    JOIN + Filter...", end=" ", flush=True)
    d_join_f = benchmark(lambda: conn.execute(join_filter_q).df())
    print(f"{d_join_f:.1f}ms")

    print(f"    JOIN + Aggregate...", end=" ", flush=True)
    d_join_a = benchmark(lambda: conn.execute(join_agg_q).df())
    print(f"{d_join_a:.1f}ms")

    conn.close()

    results["duckdb_join_ms"] = d_join
    results["duckdb_join_filter_ms"] = d_join_f
    results["duckdb_join_agg_ms"] = d_join_a

    print(f"\n  {'Operation':<20} {'Rhizo':>10} {'DuckDB':>10} {'Speedup':>10}")
    print(f"  {'-'*50}")
    for op, rlabel in [("join", "Simple JOIN"), ("join_filter", "JOIN+Filter"), ("join_agg", "JOIN+Agg")]:
        r = results[f"rhizo_{op}_ms"]
        d = results[f"duckdb_{op}_ms"]
        ratio = d / r if r > 0 else float('inf')
        winner = "Rhizo" if ratio > 1 else "DuckDB"
        print(f"  {rlabel:<20} {r:>9.1f}ms {d:>9.1f}ms {ratio:>8.1f}x ({winner})")

    return results


def main():
    scales = [1_000_000, 5_000_000, 10_000_000]
    join_scales = [(10_000, 100_000), (50_000, 1_000_000), (100_000, 5_000_000)]

    print("="*70)
    print("RHIZO SCALE BENCHMARK: Finding the limits")
    print("="*70)
    print(f"Testing scales: {[f'{s//1_000_000}M' for s in scales]}")
    print(f"Testing JOINs:  {[f'{u//1000}K x {o//1_000_000}M' if o >= 1_000_000 else f'{u//1000}K x {o//1000}K' for u, o in join_scales]}")

    all_results = {"scales": [], "joins": []}

    with tempfile.TemporaryDirectory() as temp_dir:
        for scale in scales:
            try:
                result = run_scale(scale, temp_dir)
                all_results["scales"].append(result)
            except Exception as e:
                print(f"\n  FAILED at {scale:,} rows: {e}")
                all_results["scales"].append({"scale": scale, "error": str(e)})
                break

        for num_users, num_orders in join_scales:
            try:
                result = run_join_scale(num_users, num_orders, temp_dir)
                all_results["joins"].append(result)
            except Exception as e:
                print(f"\n  FAILED JOIN at {num_users}x{num_orders}: {e}")
                all_results["joins"].append({"users": num_users, "orders": num_orders, "error": str(e)})
                break

    # Summary
    print(f"\n{'='*70}")
    print("SCALE SUMMARY")
    print(f"{'='*70}")
    print(f"\n{'Scale':<8} {'Op':<15} {'Rhizo':>10} {'DuckDB':>10} {'Winner':>10}")
    print("-"*55)
    for r in all_results["scales"]:
        if "error" in r:
            print(f"{r.get('label', '?'):<8} ERROR: {r['error']}")
            continue
        for op in ["write", "read", "filter", "agg", "complex"]:
            rv = r[f"rhizo_{op}_ms"]
            dv = r[f"duckdb_{op}_ms"]
            ratio = dv / rv if rv > 0 else 0
            winner = f"Rhizo {ratio:.1f}x" if ratio > 1 else f"DuckDB {1/ratio:.1f}x"
            print(f"{r['label']:<8} {op:<15} {rv:>9.1f}ms {dv:>9.1f}ms {winner:>10}")

    out_path = os.path.join(os.path.dirname(__file__), "SCALE_BENCHMARK_RESULTS.json")
    with open(out_path, "w") as f:
        json.dump(all_results, f, indent=2)
    print(f"\nResults saved to {out_path}")


if __name__ == "__main__":
    main()
