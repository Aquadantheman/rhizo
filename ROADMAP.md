# Rhizo Roadmap

## Current Status

**1,426 tests passing (476 Rust + 950 Python)**

Rhizo is feature-complete for single-node deployments with full ACID transactions, time travel, branching, and OLAP queries.

| Phase | Status | Key Deliverable |
|-------|--------|-----------------|
| Phase 1: Chunk Store | Complete | Content-addressable storage with BLAKE3 |
| Phase 2: Catalog | Complete | Versioned tables with time travel |
| Phase 3: Query Layer | Complete | DataFusion SQL engine |
| Phase 4: Branching | Complete | Git-like branches, zero-copy semantics |
| Phase 5: Transactions | Complete | Cross-table ACID with snapshot isolation |
| Phase 6: Changelog | Complete | Unified batch/stream via subscriptions |
| Phase A: Merkle Storage | Complete | O(change) deduplication |
| Phase P: Performance | Complete | Native Rust Parquet, parallel I/O, Arrow cache |
| Phase DF: OLAP | Complete | DataFusion engine, 32x faster than DuckDB |
| Phase CF: Coordination-Free | Complete | Algebraic transactions, 59x measured vs localhost 2PC |
| Phase SE: Schema & Keys | Complete | Schema evolution, primary key constraints |

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
- **BranchManager**: Git-like branching with zero-copy semantics (~140 bytes per branch)

### Transaction Layer (Rust)
- **TransactionManager**: Cross-table ACID with snapshot isolation
- **Conflict detection**: 3-layer defense-in-depth
- **Recovery**: Automatic crash recovery with consistency verification
- **Algebraic operations**: Coordination-free merge for commutative operations

### Query Layer (Python + Rust)
- **QueryEngine**: DataFusion-powered SQL with time travel support
- **OLAPEngine**: DataFusion-based, 32x faster reads than DuckDB
- **Extended SQL**: `VERSION` keyword, `@branch` notation, `__changelog` table
- **Arrow cache**: 15x speedup on repeated reads, 97.2%+ hit rate

### Export
- **Parquet, CSV, JSON**: `db.export("users", "users.parquet")` — format auto-detected from extension
- **Streaming Parquet**: One chunk in memory at a time via `pq.ParquetWriter`
- **Single-chunk fast path**: Raw byte copy (53M rows/s, 2.5x faster than DuckDB COPY TO)
- **Column projection**: Export subset of columns for smaller files
- **Atomic writes**: Temp file + rename for crash safety

### Garbage Collection
- **Two-phase GC**: Phase 1 deletes old versions, Phase 2 sweeps unreferenced chunks
- **Time-based TTL**: `db.gc(max_age_seconds=86400)` — delete versions older than N seconds
- **Count-based retention**: `db.gc(max_versions_per_table=5)` — keep only N most recent
- **AutoGC**: Background daemon thread with configurable interval (`auto_gc=GCPolicy(...)`)
- **Safety-first**: Never deletes latest, branch-referenced, or transaction-referenced versions
- **Crash-safe**: Orphaned chunks cleaned on next GC run
- **~13ms per version deletion**, <5ms protection collection for 50 branches

### Version & Branch Diff
- **Schema diff**: Added/removed columns, type changes detected automatically
- **Row-level diff**: Added, removed, modified rows via DuckDB vectorized comparison
- **Modified row detail**: `__old_{col}` / `__new_{col}` pairs for each changed column
- **Merkle acceleration**: Skip unchanged chunks — 100% skip on identical data (767us fast path)
- **Semantic diffs**: Algebraic-aware descriptions ("counter increased by 47", "new maximum: 100")
- **Branch diff**: `db.diff("t", branch_a="main", branch_b="feature", key_columns=["id"])`
- **3M rows/s** at 100K rows, sub-ms stats-only mode, <2% semantic overhead

### Changelog & Streaming
- **get_changes()**: Query changes since a checkpoint
- **subscribe()**: Continuous change notifications
- **Background processing**: Event-driven pipelines

### Schema Evolution & Primary Keys
- **Schema evolution**: Additive-only by default (new columns OK, removals/type changes error)
- **Flexible mode**: `schema_mode="flexible"` allows any schema change
- **Primary keys**: `primary_key=["id"]` enforces uniqueness at write time via DuckDB
- **Immutable PK**: Once set, primary key cannot be changed
- **Schema API**: `db.schema()`, `db.schema_history()`, `db.primary_key()`, `db.set_primary_key()`, `db.set_schema_mode()`
- **Diff auto-resolve**: `db.diff()` auto-uses PK as key_columns
- **Table metadata**: `_table_meta.json` per table for persistent configuration
- **Backwards compatible**: Existing databases work without changes

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
| Transaction latency | 0.001ms | 160,000x vs cross-continent 2PC, 30,000x vs same-region 2PC, 59x vs localhost 2PC, 355x vs SQLite FULL sync (all measured) |
| Energy per transaction | 2.2e-11 kWh | 97,943x less than consensus |
| OLAP read (100K rows) | 0.9ms | 32x faster than DuckDB |
| Branch creation | <10ms | 450,000x smaller than Delta Lake |
| Write throughput | 2,277 MB/s | Competitive with DuckDB |
| Storage deduplication | 84% | Best in class |
| Version diff (100K rows) | 35ms | 3M rows/s with Merkle skip |
| Identical version detect | 0.8ms | 100% chunk skip via Merkle |

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
cargo test --all      # 476 Rust tests
pytest tests/ -v      # 837 Python tests
```

---

## Contributing

See [CONTRIBUTING.md](./CONTRIBUTING.md) for development setup and guidelines.

---

*Last updated: January 2026*
