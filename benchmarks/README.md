# Benchmarks

Performance benchmarks comparing Rhizo against industry alternatives.

## Benchmarks

| File | Purpose | Compares Against |
|------|---------|------------------|
| `comprehensive_benchmark.py` | Main benchmark - OLAP, JOINs, scale, branching, dedup | Delta Lake, DuckDB, Parquet |
| `benchmark_suite.py` | arXiv paper benchmarks - throughput, branching, dedup | N/A (Rhizo-only) |
| `unique_features_benchmark.py` | Rhizo-only features | Branching, CDC, Merkle verification |
| `distributed_benchmark.py` | Coordination-free transactions | Simulated consensus baseline |
| `energy_benchmark.py` | Energy/CO2 measurements | Consensus-based systems |
| `merkle_benchmark.py` | Merkle tree deduplication | Full file storage |
| `parallel_encoding_benchmark.py` | Parallel Parquet encoding | Sequential encoding |
| `row_group_pruning_benchmark.py` | Row group statistics pruning | Full table scan |

## Running Benchmarks

```bash
# From repo root
python benchmarks/comprehensive_benchmark.py
python benchmarks/distributed_benchmark.py
# etc.
```

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
