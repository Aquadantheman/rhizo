# Benchmarks

Performance benchmarks comparing Rhizo against industry alternatives.

## Benchmarks

| File | Purpose | Compares Against |
|------|---------|------------------|
| `comprehensive_benchmark.py` | Main benchmark - OLAP, JOINs, scale, branching, dedup | Delta Lake, DuckDB, Parquet |
| `real_consensus_benchmark.py` | **Empirical validation** against real systems | Localhost 2PC, SQLite WAL (NORMAL + FULL sync), Redis, etcd |
| `2pc_participant_server.py` | Remote 2PC participant for cloud benchmarks | Deploy on VMs, coordinator connects over network |
| `benchmark_suite.py` | arXiv paper benchmarks - throughput, branching, dedup | N/A (Rhizo-only) |
| `unique_features_benchmark.py` | Rhizo-only features | Branching, CDC, Merkle verification |
| `distributed_benchmark.py` | Coordination-free transactions (simulated multi-node) | Simulated consensus baseline |
| `energy_benchmark.py` | Energy/CO2 measurements | Consensus-based systems (simulated) |
| `merkle_benchmark.py` | Merkle tree deduplication | Full file storage |
| `parallel_encoding_benchmark.py` | Parallel Parquet encoding | Sequential encoding |
| `row_group_pruning_benchmark.py` | Row group statistics pruning | Full table scan |
| `export_benchmark.py` | Export to Parquet/CSV/JSON | DuckDB COPY TO |

## Running Benchmarks

```bash
# From repo root
python benchmarks/comprehensive_benchmark.py
python benchmarks/distributed_benchmark.py

# Consensus benchmark (localhost)
python benchmarks/real_consensus_benchmark.py

# Consensus benchmark (cloud — see CLOUD_BENCHMARK.md)
python benchmarks/real_consensus_benchmark.py \
  --remote-2pc us-east-vm:9000,eu-west-vm:9000
```

See [CLOUD_BENCHMARK.md](CLOUD_BENCHMARK.md) for geo-distributed benchmark setup and measured results.

## Key Results (January 2026)

| Benchmark | Rhizo | Baseline | Speedup | Methodology |
|-----------|-------|----------|---------|-------------|
| Cross-continent 2PC | 0.001ms | 187.9ms | **160,000x** | NYC → AWS Oregon + Ireland, 500 iters |
| Same-region 2PC | 0.001ms | 33.3ms | **30,000x** | NYC → AWS Virginia, 500 iters |
| Localhost 2PC | 0.001ms | 0.065ms | **59x** | 3 OS processes, real TCP |
| SQLite FULL sync | 0.001ms | 0.386ms | **355x** | fsync per commit |
| OLAP read (100K) | 0.9ms | 26ms (DuckDB) | **32x** | DataFusion vs DuckDB |
| OLAP read (1M) | 3.7ms | 283ms (DuckDB) | **76x** | DataFusion vs DuckDB |
| Parquet export (1M) | 0.019s | 0.048s (DuckDB COPY) | **2.5x** | Streaming + fast path |

Raw result files: `CLOUD_MULTI_REGION_RESULTS.json`, `CLOUD_SAME_REGION_RESULTS.json`, `REAL_CONSENSUS_BENCHMARK_RESULTS.json`

## Dependencies

Core benchmarks require Rhizo to be installed. Some benchmarks need additional packages:

| Benchmark | Extra Dependencies |
|-----------|-------------------|
| `comprehensive_benchmark.py` | `deltalake`, `duckdb` |
| `energy_benchmark.py` | `codecarbon`, `psutil` |

Install optional dependencies:
```bash
pip install deltalake duckdb codecarbon psutil
```

## Output

Results are saved to `benchmarks/results/` (gitignored - machine-specific).

## Archive

Historical/redundant benchmarks are in `benchmarks/archive/` (gitignored).
