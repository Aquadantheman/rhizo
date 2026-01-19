# Rhizo Roadmap

## Current Status

**632 tests passing (370 Rust + 262 Python)**

Rhizo is feature-complete for single-node deployments with full ACID transactions, time travel, branching, and OLAP queries.

| Phase | Status | Key Deliverable |
|-------|--------|-----------------|
| Phase 1: Chunk Store | Complete | Content-addressable storage with BLAKE3 |
| Phase 2: Catalog | Complete | Versioned tables with time travel |
| Phase 3: Query Layer | Complete | DuckDB integration with SQL |
| Phase 4: Branching | Complete | Git-like branches, zero-copy semantics |
| Phase 5: Transactions | Complete | Cross-table ACID with snapshot isolation |
| Phase 6: Changelog | Complete | Unified batch/stream via subscriptions |
| Phase A: Merkle Storage | Complete | O(change) deduplication |
| Phase P: Performance | Complete | Native Rust Parquet, parallel I/O, Arrow cache |
| Phase DF: OLAP | Complete | DataFusion engine, 26x faster than DuckDB |
| Phase CF: Coordination-Free | Complete | Algebraic transactions, 31,000x faster than consensus |

---

## What We've Built

### Storage Layer (Rust)
- **ChunkStore**: Content-addressable storage with BLAKE3 hashing
- **Atomic writes**: Write-to-temp-rename pattern prevents corruption
- **Automatic deduplication**: Same content = same hash = single copy
- **Merkle trees**: O(change) incremental storage

### Catalog Layer (Rust)
- **FileCatalog**: Versioned table metadata with JSON persistence
- **Time travel**: Query any historical version in O(1)
- **BranchManager**: Git-like branching with zero-copy semantics (280 bytes per branch)

### Transaction Layer (Rust)
- **TransactionManager**: Cross-table ACID with snapshot isolation
- **Conflict detection**: 3-layer defense-in-depth
- **Recovery**: Automatic crash recovery with consistency verification
- **Algebraic operations**: Coordination-free merge for commutative operations

### Query Layer (Python + Rust)
- **QueryEngine**: DuckDB-based SQL with time travel support
- **OLAPEngine**: DataFusion-based, 26x faster reads than DuckDB
- **Extended SQL**: `VERSION` keyword, `@branch` notation, `__changelog` table
- **Arrow cache**: 15x speedup on repeated reads, 97%+ hit rate

### Changelog & Streaming
- **get_changes()**: Query changes since a checkpoint
- **subscribe()**: Continuous change notifications
- **Background processing**: Event-driven pipelines

---

## What's Next

### Phase 7: Production Validation
- Real workload migration and testing
- Performance profiling under production conditions
- Edge case discovery and hardening

### Phase 8: Release
- PyPI publication (`pip install rhizo`)
- API documentation (Rust docs, Python docstrings)
- Tutorial notebooks and examples
- Architecture deep-dive document

### Future: Distributed Deployment
- Consistent hashing for chunk distribution
- Multi-node coordination for non-algebraic operations
- Cloud storage backends (S3, GCS)

---

## Performance Highlights

| Metric | Rhizo | Comparison |
|--------|-------|------------|
| Transaction latency | 0.022ms | 31,000x faster than consensus |
| Energy per transaction | 2.2e-11 kWh | 97,943x less than consensus |
| OLAP read (100K rows) | 0.9ms | 26x faster than DuckDB |
| Branch creation | <10ms | 52,500x smaller than Delta Lake |
| Write throughput | 211 MB/s | Competitive with Delta Lake |
| Storage deduplication | 84% | Best in class |

---

## Quick Start

```bash
# Build from source
git clone https://github.com/rhizodata/rhizo.git
cd rhizo

# Create virtual environment
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate

# Install dependencies and build
pip install maturin pytest pandas pyarrow duckdb
cd rhizo_python && maturin develop --release && cd ..
pip install -e python/

# Run tests
cargo test --all      # 370 Rust tests
pytest tests/ -v      # 262 Python tests
```

---

## Contributing

See [CONTRIBUTING.md](./CONTRIBUTING.md) for development setup and guidelines.

---

*Last updated: January 2026*
