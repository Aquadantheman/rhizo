# Changelog

All notable changes to Rhizo will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.3.0] - 2026-01-17

### Added

#### DataFusion OLAP Engine (Phase DF.1-4)
- Native DataFusion integration for high-performance OLAP queries
- LRU cache with size-based eviction for Arrow tables
- Parallel table loading with ThreadPoolExecutor
- **26x faster reads** than DuckDB (0.9ms vs 23.8ms at 100K rows)
- **50x faster reads** at 1M scale (5.1ms vs 257.2ms)

#### Time Travel SQL Syntax (Phase DF.3)
- `VERSION` keyword for inline time travel: `SELECT * FROM users VERSION 5`
- Case-insensitive parsing for SQL compatibility
- Works with aggregations, JOINs, and complex queries

#### Branch Query Syntax (Phase DF.3)
- `@branch` notation: `SELECT * FROM users@feature-branch`
- Automatic branch head resolution for queries
- Combined with VERSION for specific branch versions

#### Changelog SQL Queries (Phase DF.4)
- Virtual `__changelog` table for CDC via SQL
- Query transaction history: `SELECT * FROM __changelog`
- Filter by table, transaction ID, branch, time range
- Aggregations over changelog data

### Performance
- **OLAP read (cached)**: 0.9ms (26x faster than DuckDB)
- **Filtered query (5%)**: 1.2ms
- **Projection query**: 0.7ms
- **Complex query**: 2.9ms (2.3x faster than DuckDB)
- **JOIN performance**: Wins all 3 categories vs DuckDB
- **1M row scale**: 50x faster reads, 7.5x faster filters

### Testing
- 173 Rust tests
- 247 Python tests (+68 new OLAP/time travel/changelog tests)
- All tests passing

---

## [0.2.0] - 2026-01-17

### Added

#### Projection Pushdown (Phase R.1) - Read Optimization
- Native column projection in Parquet decoder (`decode_columns`, `decode_columns_by_name`)
- TableReader `columns` parameter for selective column reading
- **5.1x speedup** for single-column queries (vs full scan)
- **2.1x speedup** for 2-column queries from 10-column tables
- Python bindings for projection pushdown
- Mathematical model: Speedup ≈ n/k where n=total cols, k=requested cols

#### Native Rust Parquet Encoding (Phase P.4)
- Native Parquet encoder using `arrow-rs` and `parquet` crates
- Native Parquet decoder with parallel batch support via Rayon
- Zero-copy Arrow FFI between Python and Rust using `pyo3-arrow`
- `PyParquetEncoder` and `PyParquetDecoder` Python bindings
- `use_native_parquet` flag for TableWriter and TableReader (default: True)

#### Merkle Tree Storage (Phase A)
- Content-addressed Merkle tree for incremental deduplication
- O(change) storage instead of O(n) per version
- 95% chunk reuse for 5% data changes
- `merkle_build_tree`, `merkle_diff_trees`, `merkle_verify_tree` functions

### Changed
- TableWriter now uses Rust Parquet encoder by default (2.3x faster)
- TableReader now uses Rust Parquet decoder by default
- Upgraded `pyo3` from 0.23 to 0.27 for pyo3-arrow compatibility

### Performance
- **Write throughput**: ~90 MB/s → **211 MB/s** (2.3x improvement)
- **Write time (100K rows)**: ~143ms → **59.8ms**
- **Competitive with Delta Lake** on write performance
- **84% storage deduplication** (best in class vs 77% Delta Lake)
- **52,500x better branching overhead** (280 bytes vs 14.70 MB)

### Testing
- 181 Rust tests (+8 new projection tests, +13 Parquet tests)
- 38 Query Layer Python tests (+7 projection pushdown tests)
- Full competition benchmark against Delta Lake, Iceberg, and Hudi

---

## [0.1.0] - 2025-01-17

### Added

#### Core Storage (Phase 1)
- Content-addressable chunk store with BLAKE3 hashing
- Automatic deduplication via content addressing
- Atomic writes using write-to-temp-rename pattern
- Integrity verification with `get_verified()`

#### Versioned Catalog (Phase 2)
- File-based catalog with JSON metadata
- Sequential version enforcement
- Time travel queries to any historical version
- Table listing and version history

#### Query Layer (Phase 3)
- DuckDB integration for SQL queries
- TableWriter for DataFrame/Arrow table ingestion
- TableReader with chunked reading support
- QueryEngine with caching and multiple output formats

#### Git-like Branching (Phase 4)
- Zero-copy branch creation (branches are pointers, not copies)
- Branch isolation for safe experimentation
- Branch diffing and comparison
- Fast-forward merge support
- Checkout and branch switching

#### Cross-table ACID Transactions (Phase 5)
- Atomic multi-table commits
- Snapshot isolation with conflict detection
- Read-your-writes within transactions
- Automatic rollback on exceptions
- Transaction recovery after crashes

#### Changelog & Subscriptions (Phase 6)
- ChangelogEntry tracking for all commits
- ChangelogQuery builder with filtering
- Subscriber API for change notifications
- Polling, iterator, and background processing modes

#### Python Bindings
- PyO3-based bindings for all Rust functionality
- Type stubs for IDE support
- Pythonic API with context managers

### Security
- SQL injection protection in `diff_versions()`
- Path traversal protection in table names
- Input validation throughout

### Testing
- 127 Rust tests
- 153 Python tests including concurrency tests
- Clippy and Ruff linting (clean)

---

## Future Releases

See [udr_roadmap.md](./udr_roadmap.md) for planned features:
- Phase 7: Production migration tooling
- Phase 8: Distributed storage backends
- Phase 9: Advanced merge strategies
