# Unified Data Runtime (UDR)

**A next-generation data infrastructure that unifies transactional, analytical, and streaming workloads through content-addressable storage, cross-table ACID transactions, and Git-like versioning.**

[![Rust Tests](https://img.shields.io/badge/tests-22%20passed-brightgreen)]()
[![Python Tests](https://img.shields.io/badge/python%20tests-46%20passed-brightgreen)]()
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)]()

## Why UDR?

Modern data infrastructure is fundamentally fragmented. Organizations maintain separate systems for:
- Transactional workloads (PostgreSQL, MySQL)
- Analytical processing (Snowflake, BigQuery)
- Streaming computation (Kafka, Flink)
- Feature stores for ML pipelines

**The result:** 60-80% of data engineering effort goes to integration, synchronization, and pipeline maintenance rather than generating insights.

UDR eliminates this fragmentation through five foundational innovations:

| Innovation | Benefit |
|------------|---------|
| **Content-Addressable Storage** | Automatic deduplication, corruption detection, zero-copy operations |
| **Cross-Table ACID Transactions** | Atomic updates across your entire data estate |
| **Time Travel** | Query any historical version instantly |
| **Git-like Branching** | Experiment safely, merge when ready |
| **Unified Batch/Stream** | One semantic model for all workloads |

## Current Status

| Phase | Status | Description |
|-------|--------|-------------|
| Phase 1: Storage | ✅ Complete | Content-addressable chunk store with BLAKE3 hashing |
| Phase 2: Catalog | ✅ Complete | Versioned file catalog with time travel |
| Phase 3: Query | ✅ Complete | DuckDB integration with SQL + time travel |
| Phase 4: Branching | 🔄 Next | Git-like branching for data development |
| Phase 5: Transactions | ⏳ Planned | Cross-table ACID with MVCC |
| Phase 6: Changelog | ⏳ Planned | Unified batch/stream via subscriptions |

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                      Application Layer                       │
│          Python (udr_query) │ Rust │ CLI (planned)          │
│      TableWriter │ TableReader │ QueryEngine (DuckDB)        │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                        FileCatalog                           │
│   • Versioned table metadata                                 │
│   • Sequential version enforcement                           │
│   • Time travel queries                                      │
│   • Atomic commits                                           │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                        ChunkStore                            │
│   • Content-addressed storage (BLAKE3)                       │
│   • Automatic deduplication                                  │
│   • Atomic writes (write-to-temp-rename)                     │
│   • Integrity verification (get_verified)                    │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                       File System                            │
│   • 2-level directory tree for O(1) lookups (ab/cd/...)     │
│   • JSON metadata files                                      │
│   • Parquet data chunks                                      │
└─────────────────────────────────────────────────────────────┘
```

## Quick Start

### Prerequisites
- Rust 1.70+ (`rustup install stable`)
- Python 3.9+
- maturin (`pip install maturin`)

### Installation

```bash
# Clone and build
git clone https://github.com/yourusername/unifieddataruntime.git
cd unifieddataruntime

# Build Rust core and Python bindings
cd udr_python && maturin develop --release && cd ..

# Install Python dependencies
pip install pyarrow duckdb pandas pytest
```

### Basic Usage (Python)

```python
import udr
from udr_query import TableWriter, TableReader, QueryEngine
import pandas as pd

# Initialize storage
store = udr.PyChunkStore("./data/chunks")
catalog = udr.PyCatalog("./data/catalog")

# Write data using the query layer
engine = QueryEngine(store, catalog)

df = pd.DataFrame({
    "id": [1, 2, 3],
    "name": ["Alice", "Bob", "Charlie"],
    "score": [85.5, 92.0, 78.5]
})

engine.write_table("users", df)

# SQL queries with DuckDB
result = engine.query("SELECT * FROM users WHERE score > 80")
print(result.to_pandas())
```

### Time Travel

```python
# Write version 1
engine.write_table("users", df_v1)

# Write version 2 with updated data
df_v2 = df_v1.copy()
df_v2["score"] = df_v2["score"] + 10
engine.write_table("users", df_v2)

# Query any historical version
result_v1 = engine.query(
    "SELECT AVG(score) FROM users",
    versions={"users": 1}  # Time travel!
)

result_v2 = engine.query(
    "SELECT AVG(score) FROM users",
    versions={"users": 2}
)

# Compare versions
diff = engine.diff_versions("users", 1, 2, key_columns=["id"])
print(f"Rows added: {diff['rows_added']}")
print(f"Rows removed: {diff['rows_removed']}")
```

### Low-Level API (Rust Core)

```python
import udr

# Content-addressable storage
store = udr.PyChunkStore("./data/chunks")

# Store data - returns BLAKE3 hash
hash_str = store.put(b"Hello, World!")
print(f"Stored with hash: {hash_str}")

# Retrieve with integrity verification
data = store.get_verified(hash_str)  # Raises if corrupted

# Versioned catalog
catalog = udr.PyCatalog("./data/catalog")

version = udr.PyTableVersion("users", 1, [hash_str])
catalog.commit(version)

# Time travel to any version
v1 = catalog.get_version("users", 1)
latest = catalog.get_version("users")  # Gets latest
```

## Testing

```bash
# Run all Rust tests (22 tests)
cargo test --all

# Run Python tests (46 tests)
pytest tests/ -v
```

## Project Structure

```
unifieddataruntime/
├── Cargo.toml                    # Workspace configuration
├── README.md                     # This file
├── udr_roadmap.md               # Development roadmap
├── unified_data_runtime_whitepaper.docx  # Technical whitepaper
│
├── udr_core/                     # Rust core library
│   └── src/
│       ├── lib.rs               # Public exports
│       ├── chunk_store/         # Content-addressable storage
│       │   ├── store.rs         # ChunkStore (BLAKE3, atomic writes)
│       │   └── error.rs         # ChunkStoreError, HashMismatch
│       └── catalog/             # Versioned catalog
│           ├── file_catalog.rs  # FileCatalog implementation
│           ├── version.rs       # TableVersion struct
│           └── error.rs         # CatalogError types
│
├── udr_python/                   # PyO3 bindings
│   └── src/lib.rs               # Python interface
│
├── python/                       # Python packages
│   ├── udr.pyi                  # Type stubs for IDE support
│   └── udr_query/               # Query layer
│       ├── writer.py            # TableWriter (DataFrame → Parquet chunks)
│       ├── reader.py            # TableReader (chunks → DataFrame)
│       └── engine.py            # QueryEngine (DuckDB + time travel)
│
├── tests/                        # Test suites
│   ├── test_udr.py              # Core Rust binding tests
│   └── test_query_layer.py      # Query layer tests (26 tests)
│
└── examples/
    └── time_travel_demo.py      # Interactive demo
```

## Design Principles

1. **Immutability**: All data is immutable once written. Updates create new versions.
2. **Content Addressing**: Data identified by BLAKE3 hash enables automatic deduplication.
3. **Atomic Operations**: Write-to-temp-rename pattern prevents corruption.
4. **Layered Architecture**: ChunkStore and FileCatalog are independent and composable.
5. **Time Travel by Default**: Every version is preserved and queryable.

## Performance Characteristics

| Operation | Complexity | Notes |
|-----------|------------|-------|
| Write chunk | O(n) | n = data size; hashing + file write |
| Read chunk | O(n) | n = data size; direct file read |
| Check exists | O(1) | Hash → path lookup |
| Time travel query | O(1) | Direct version lookup, no log replay |
| Commit version | O(k) | k = number of chunk hashes |
| Branch creation | O(k) | k = tables; zero data copy |

## Theoretical Impact

Based on the technical whitepaper analysis:

| Metric | Current State | With UDR |
|--------|--------------|----------|
| Data engineering overhead | 60-80% on integration | Focus on insights |
| Storage efficiency | Multiple copies per system | 60-85% deduplication |
| Query consistency | Eventually consistent | ACID across tables |
| Historical queries | Limited or impossible | Instant time travel |
| Experimentation | Risky, requires copies | Zero-copy branching |

## Roadmap

See [udr_roadmap.md](./udr_roadmap.md) for the complete development roadmap.

**Next milestone (Phase 4: Branching):**
- Create named branches from any version
- Zero-copy branch creation (only pointers copied)
- Branch diff and merge capabilities
- Git-like workflow for data development

## References

- [Technical Whitepaper](./unified_data_runtime_whitepaper.docx) - Full architectural specification
- [BLAKE3](https://github.com/BLAKE3-team/BLAKE3) - Cryptographic hash function
- [DuckDB](https://duckdb.org/) - Analytical SQL engine
- [Apache Arrow](https://arrow.apache.org/) - Columnar memory format
- [PyO3](https://pyo3.rs/) - Rust bindings for Python

## License

MIT

## Contributing

Contributions welcome! Please read the development roadmap for planned features and architecture decisions.
