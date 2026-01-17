# Armillaria

**Named after the largest organism on Earth — a 2,400-year-old honey fungus spanning 2,385 acres, connecting an entire forest underground.**

Armillaria is a next-generation data infrastructure that unifies transactional, analytical, and streaming workloads through content-addressable storage, cross-table ACID transactions, and Git-like versioning.

[![CI](https://github.com/aquadantheman/unifieddataruntime/actions/workflows/ci.yml/badge.svg)](https://github.com/aquadantheman/unifieddataruntime/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Rust](https://img.shields.io/badge/rust-173%20tests-blue)](https://github.com/aquadantheman/unifieddataruntime)
[![Python](https://img.shields.io/badge/python-179%20tests-blue)](https://github.com/aquadantheman/unifieddataruntime)

---

## The Problem

Modern data infrastructure is fragmented. Organizations maintain separate systems for transactional workloads, analytical processing, streaming computation, and ML feature stores. The result: **60-80% of data engineering effort goes to integration and pipeline maintenance** rather than generating insights.

Existing lakehouse formats (Delta Lake, Iceberg, Hudi) improve single-table versioning but cannot solve:

| Limitation | Why It Matters |
|------------|----------------|
| Single-table transactions only | Cannot atomically update customers + orders + audit log |
| No cross-table deduplication | Same data stored multiple times across tables |
| No branching | Cannot safely experiment on production data |
| Batch-stream semantic gap | Different APIs, different guarantees |

## The Solution

Armillaria replaces the architecture that makes these limitations inevitable.

| Capability | What It Enables |
|------------|-----------------|
| **Content-Addressable Storage** | Automatic deduplication, corruption detection, zero-copy operations |
| **Cross-Table ACID Transactions** | Atomic updates across your entire data estate |
| **Time Travel** | Query any historical version instantly |
| **Git-like Branching** | Experiment safely, merge when ready |
| **Unified Batch/Stream** | One semantic model for all workloads |

---

## Benchmarks

### Performance vs Competitors (100K rows)

| Metric | Armillaria | Delta Lake | Iceberg | Hudi | Winner |
|--------|------------|------------|---------|------|--------|
| **Write** | **59.8ms** (211 MB/s) | 53.4ms | 115.2ms | 46.9ms | Competitive |
| **Read** | 69.0ms (183 MB/s) | **34.6ms** | 72.5ms | N/A | Delta Lake |
| **Versioning** (5 ver) | **213.8ms** | 216.8ms | 428.8ms | N/A | **Armillaria** |
| **Storage** (5 ver) | **9.93 MB** (84%) | 14.70 MB (77%) | 11.11 MB (82%) | N/A | **Armillaria** |
| **Branching** | **7.5ms, 280 bytes** | 14.5ms, 14.70 MB | N/A | N/A | **Armillaria** |

**Key Wins:**
- **52,500x better** branching overhead (280 bytes vs 14.70 MB)
- **84% storage deduplication** (best in class)
- **Competitive write speed** with native Rust Parquet encoding

### Core Operations

| Operation | Performance | Notes |
|-----------|-------------|-------|
| Write throughput | 211 MB/s | Native Rust Parquet encoding |
| Read throughput | 183 MB/s | Zero-copy Arrow FFI |
| Branch creation | <10 ms | Zero-copy, 280 bytes overhead |
| Time travel query | ~26 ms | O(1) version lookup |
| Incremental dedup | 95% reuse | 5% change = 95% chunk reuse |

### Incremental Deduplication (Merkle Tree Storage)

| Change Percentage | Chunk Reuse | Storage Savings |
|-------------------|-------------|-----------------|
| 1% change | 98.8% reuse | ~49% vs naive |
| 5% change | 95.0% reuse | ~47% vs naive |
| 10% change | 90.0% reuse | ~45% vs naive |

**O(change) storage** instead of O(n) per version.

### Feature Comparison

| Feature | Armillaria | Delta Lake | Iceberg | Hudi |
|---------|------------|------------|---------|------|
| **Cross-table ACID** | **Yes** | No | No | No |
| **Zero-copy branching** | **Yes** | No | No* | No |
| **Global deduplication** | **Yes** | No | No | No |
| **Merkle tree dedup** | **Yes** | No | No | No |
| **Corruption detection** | **Built-in** | External | External | External |
| Time travel | Yes | Yes | Yes | Yes |
| SQL Query Engine | Yes | Yes | Yes | Yes |
| Cloud Storage | Planned | Yes | Yes | Yes |

*Iceberg branching requires Nessie catalog

---

## Quick Start

### Installation

```bash
pip install armillaria-query
```

### Basic Usage

```python
import armillaria
from armillaria_query import QueryEngine
import pandas as pd

# Initialize
store = armillaria.PyChunkStore("./data/chunks")
catalog = armillaria.PyCatalog("./data/catalog")
engine = QueryEngine(store, catalog)

# Write data
df = pd.DataFrame({
    "id": [1, 2, 3],
    "name": ["Alice", "Bob", "Charlie"],
    "score": [85.5, 92.0, 78.5]
})
engine.write_table("users", df)

# Query with DuckDB
result = engine.query("SELECT * FROM users WHERE score > 80")
```

### Time Travel

```python
# Query historical versions
result_v1 = engine.query(
    "SELECT AVG(score) FROM users",
    versions={"users": 1}
)

# Compare versions
diff = engine.diff_versions("users", 1, 2, key_columns=["id"])
```

### Branching

```python
# Create branch (instant, zero-copy)
engine.create_branch("experiment/new-scoring")
engine.checkout("experiment/new-scoring")

# Modify on branch (production unchanged)
engine.write_table("scores", updated_df)

# Query both branches
main_result = engine.query("SELECT * FROM scores", branch="main")
exp_result = engine.query("SELECT * FROM scores", branch="experiment/new-scoring")

# Merge when ready
engine.merge_branch("experiment/new-scoring", into="main")
```

### Cross-Table Transactions

```python
with engine.transaction() as tx:
    tx.write_table("customers", updated_customers)
    tx.write_table("orders", new_order)
    tx.write_table("audit_log", audit_entry)
    # All commit together, or all rollback
```

---

## Architecture

```
Application Layer
    Python (armillaria_query) | Rust | CLI (planned)
    TableWriter | TableReader | QueryEngine (DuckDB)
                            |
                            v
                      FileCatalog
    Versioned table metadata | Time travel queries | Atomic commits
                            |
                            v
                       ChunkStore
    Content-addressed storage (BLAKE3) | Automatic deduplication
    Atomic writes | Integrity verification
                            |
                            v
                       File System
    2-level directory tree | JSON metadata | Parquet chunks
```

---

## Current Status

| Phase | Description | Status |
|-------|-------------|--------|
| Phase 1: Storage | Content-addressable chunk store with BLAKE3 hashing | Complete |
| Phase 2: Catalog | Versioned file catalog with time travel | Complete |
| Phase 3: Query | DuckDB integration with SQL and time travel | Complete |
| Phase 4: Branching | Git-like branching with zero-copy semantics | Complete |
| Phase 5: Transactions | Cross-table ACID with recovery | Complete |
| Phase 6: Changelog | Unified batch/stream via subscriptions | Complete |
| Phase A: Merkle Storage | O(change) deduplication via Merkle trees | Complete |
| **Phase P: Performance** | Native Rust Parquet, parallel I/O | **Complete** |

**All phases complete. 352 tests passing (173 Rust + 179 Python).**

### Performance Optimization Journey

| Phase | Optimization | Result |
|-------|-------------|--------|
| P.1 | Parallel chunk I/O (Rayon) | 3-5x batch throughput |
| P.2 | Memory-mapped reads | Infrastructure for zero-copy |
| P.3 | Parallel Parquet parsing | 2.1x multi-chunk speedup |
| **P.4** | **Native Rust Parquet encoder** | **2.3x write improvement** |

Phase P.4 moved Parquet encoding from Python to Rust using `arrow-rs`, `parquet`, and `pyo3-arrow` for zero-copy Arrow FFI. Result: competitive with Delta Lake on writes.

---

## Project Structure

```
armillaria/
├── udr_core/                 # Rust core library
│   └── src/
│       ├── chunk_store/      # Content-addressable storage
│       ├── catalog/          # Versioned catalog
│       ├── branch/           # Git-like branching
│       ├── transaction/      # Cross-table ACID
│       ├── changelog/        # Change tracking
│       └── merkle/           # Merkle tree deduplication
│
├── udr_python/               # PyO3 bindings
├── python/armillaria_query/  # Python query layer
├── tests/                    # Test suites
└── examples/                 # Interactive demos
```

---

## Design Principles

1. **Immutability** — All data is immutable once written. Updates create new versions.
2. **Content Addressing** — Data identified by BLAKE3 hash enables automatic deduplication.
3. **Atomic Operations** — Write-to-temp-rename pattern prevents corruption.
4. **Layered Architecture** — ChunkStore, FileCatalog, and BranchManager are independent and composable.
5. **Time Travel by Default** — Every version is preserved and queryable.
6. **Zero-Copy Branching** — Branches are pointers to table versions, not data copies.

---

## Documentation

- [Technical Foundations](./docs/TECHNICAL_FOUNDATIONS.md) — Mathematical proofs and complexity analysis
- [Development Roadmap](./udr_roadmap.md) — Planned features and architecture
- [Changelog](./CHANGELOG.md) — Version history
- [Origin Story](./ORIGIN_STORY.md) — Why I built Armillaria
- [Contributing](./CONTRIBUTING.md) — How to contribute

---

## License

MIT — See [LICENSE](./LICENSE) for details.
