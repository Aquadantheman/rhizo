# Changelog

All notable changes to Rhizo will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.5.8] - 2026-01-31

### Added

#### PyO3 Bindings Test Suite
- **New `tests/test_pyo3_bindings.py`**: 127 tests covering the entire Rust↔Python bridge
  - `TestPyBranchManager`: 10 tests — create, list, delete, update_head, diff, merge, default branch
  - `TestPyTransactionManager`: 8 tests — begin/commit/abort, info, active list, recover, verify, latest_tx_id
  - `TestPyChangelog`: 6 tests — entries, attributes, changes, table_change, repr, changed_tables
  - `TestPyMerkle`: 11 tests — build tree, config, chunks, hashes, offset, range, diff, verify, repr
  - `TestPyAlgebraicTypes`: 28 tests — OpType, AlgebraicValue, merge, TableAlgebraicSchema, SchemaRegistry
  - `TestPyDistributedTypes`: 22 tests — NodeId, VectorClock, CausalOrder, LocalCommitProtocol
  - `TestPyAlgebraicTransaction`: 8 tests — empty tx, operations, properties, metadata
  - `TestPyFilterPredicates`: 29 tests — FilterOp names/symbols, ScalarValue types, PredicateFilter
  - `TestPyParquetAdvanced`: 5 tests — column projection, filtering, pruning stats
- **1,210 total tests** (468 Rust + 742 Python)

## [0.5.7] - 2026-01-31

### Changed

#### Write Path Performance Optimization
- **35% write speed improvement**: 79ms -> 51ms (100K rows), now within 5% of DuckDB
- **10.8x storage throughput**: 211 MB/s -> 2,277 MB/s (BLAKE3 hash + file write)
- **Multithreaded BLAKE3 hashing**: Enabled `rayon` feature for `blake3` crate, uses `update_rayon()` for buffers >= 128KB (2.7x hash speedup)
- **Zero-copy single-chunk writes**: Single-chunk writes now use `put()` with borrowed `&[u8]` instead of `put_batch(Vec<Vec<u8>>)`, eliminating a full buffer copy across the Python-Rust FFI boundary
- **GIL release during storage**: `put_batch` now releases the Python GIL via `py.detach()` during hashing and disk I/O
- 2 new Rust tests for BLAKE3 rayon hash correctness verification
- **1,083 total tests** (468 Rust + 615 Python)

## [0.5.6] - 2026-01-30

### Added

#### Concurrent/Stress Test Suite
- **New `tests/test_stress.py`**: 13 stress tests covering concurrent operations at scale
  - `TestHighConcurrencyStress`: 20-thread writes, 15-thread mixed R/W, 10-thread contention, read atomicity (4 tests)
  - `TestSustainedLoadStress`: 50 rapid transactions, 30 sequential versions (2 tests)
  - `TestBranchStress`: 10 concurrent branch creates, merge under read pressure (2 tests)
  - `TestDistributedConvergenceStress`: 10-node/100-op convergence, partition-heal-converge, mixed algebraic ops (3 tests)
  - `TestCacheStress`: 10 threads × 50 ops on shared CacheManager (1 test)
  - `TestRecoveryStress`: 3 committers + 2 aborters, verify only committed data persists (1 test)
- Registered `slow` pytest marker in `pyproject.toml`

### Changed

- **467 Python tests** (was 454, +13 stress tests)
- **910 total tests** (443 Rust + 467 Python)

## [0.5.5] - 2026-01-30

### Added

#### Cloud Benchmark Infrastructure
- **Multi-region 2PC benchmark**: Real geo-distributed coordination measurement across AWS regions
- **`2pc_participant_server.py`**: Standalone participant server for deployment on cloud VMs
- **`CLOUD_BENCHMARK.md`**: Step-by-step deployment guide for AWS/GCP cloud benchmarks
- **Statistical reporting**: p50, p95, p99, stddev, and sample size for all benchmark results

### Performance

#### Measured Cloud Results (Real 2PC Over Network)
- **160,000x faster** than cross-continent 2PC (NYC → AWS Oregon + AWS Ireland, ~100ms RTT, 500 iterations)
  - Rhizo algebraic commit: 0.001ms
  - Remote 2PC (3 machines): 187.9ms (p50: 188.1ms, p95: 191.2ms, p99: 194.3ms)
- **30,000x faster** than same-region 2PC (NYC → AWS Virginia, ~18ms RTT, 500 iterations)
  - Remote 2PC (3 machines): 33.3ms
- **59x faster** than localhost 2PC (3 OS processes, real TCP sockets)
- **355x faster** than SQLite WAL with FULL sync (fsync per commit)

All measurements use real TCP coordination between separate machines. No simulated delays.

### Documentation
- All docs updated with measured cloud numbers (README, PERFORMANCE, TECHNICAL_FOUNDATIONS, VISION, ROADMAP)
- Papers updated with geo-distributed evaluation results
- Benchmark README updated with multi-region results summary

---

## [0.5.4] - 2026-01-20

### Security

#### Error Message Sanitization
- **Path sanitization in error messages**: PyO3 bindings now sanitize filesystem paths from all error messages
  - Prevents information leakage about internal directory structure
  - Windows paths (`C:\Users\...`) and Unix paths (`/home/...`) are masked to `<path>/filename`
  - All 15+ error conversion functions updated with consistent sanitization

#### SQL Injection Hardening
- **Improved SQL table extraction**: Regex-based extraction now handles:
  - Quoted identifiers (`"my_table"`, `` `my_table` ``)
  - Schema-qualified names (`schema.table`)
  - CTEs (WITH clauses) and subqueries
  - 80+ SQL keywords excluded from false-positive extraction
  - String literals removed before extraction to prevent injection via strings

### Added

#### Comprehensive Security Test Suite
- **New `tests/test_security.py`**: 112 security/fuzzing tests covering:
  - Path traversal attacks (57 parametrized malicious inputs)
  - SQL injection attempts
  - Size limit enforcement
  - Column name validation
  - Resource exhaustion prevention
  - Concurrent access safety

#### Transaction Robustness
- **Proper cleanup on all code paths**: `TransactionContext` now uses `finally` blocks for temp table cleanup
- **Context manager protocol**: Added `__enter__`/`__exit__` methods for proper `with` statement support
- **Defensive registration**: Temp tables tracked before DuckDB registration, ensuring cleanup even on failure

#### Rust Storage Robustness
- **Temp file cleanup logging**: Failed cleanup operations now logged via `tracing` crate (was silent)
- **Orphaned file cleanup**: New `cleanup_orphaned_temp_files()` method for maintenance
- **Mmap file handle fix**: New `ChunkMmap` wrapper keeps file handle alive on Windows

#### Transaction Conflict Tests
- **20 new conflict scenario tests** covering:
  - Concurrent write detection
  - Three-way conflicts
  - Read-write isolation (snapshot isolation)
  - Transaction reuse prevention
  - Partial table conflicts

### Testing
- **492 Python tests** (was 348, +144 new)
- **443 Rust tests** (was 370, +73 new)
- All linting clean (clippy, ruff)

---

## [0.5.3] - 2026-01-20

### Security

#### Input Validation & Bounds Checking
- **TableWriter size limits**: Configurable maximum table size (default 10GB) and column count (default 1000)
  - Prevents OOM attacks from oversized inputs
  - Mathematical basis: 10GB table → ~20-30GB peak RAM with Arrow/Parquet overhead
  - Override via `max_table_size_bytes` and `max_columns` constructor parameters
- **Parquet decoder bounds checking** (Rust):
  - Maximum file size: 100GB (`MAX_DECODE_SIZE`)
  - Maximum batch size: 1M rows (`MAX_BATCH_SIZE`)
  - Checked arithmetic for row counts (prevents integer overflow)

### Added

#### Custom Exception Types
- **New `rhizo.exceptions` module**: Type-safe error handling without string matching
  - `RhizoError`: Base class for all Rhizo errors
  - `TableNotFoundError`: Raised when table doesn't exist (inherits from IOError)
  - `VersionNotFoundError`: Raised when version doesn't exist
  - `EmptyResultError`: Raised when query returns no results (inherits from ValueError)
  - `SizeLimitExceededError`: Raised when input exceeds configured limits
- **Backwards compatible**: New exceptions inherit from standard exception types

### Testing
- 443 Rust tests passing
- 454 Python tests passing
- All linting clean (clippy, ruff)

---

## [0.5.2] - 2026-01-20

### Changed

#### Production Safety Defaults
- **Integrity verification enabled by default**: `verify_integrity=True` is now the default for all read operations
  - Mathematical foundation: BLAKE3 collision probability (4.3×10⁻⁴⁸) is 10³⁵× less likely than RAM bit flips
  - The only practical risk is storage corruption, so verification should be opt-out, not opt-in
  - Override with `RHIZO_VERIFY_INTEGRITY=false` environment variable or `verify_integrity=False` parameter
- **Performance note**: Verification adds ~70% read overhead; disable in trusted environments for maximum speed

### Added

#### Structured Logging Infrastructure
- **New `rhizo.logging` module**: Centralized logging configuration with environment-based control
- **`RHIZO_LOG_LEVEL` environment variable**: Set to `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL` (default: `WARNING`)
- **Silent exception handlers now log**: OLAP fallbacks, deregistration errors, and subscriber errors are logged instead of silently swallowed
- **Zero overhead when disabled**: Default `WARNING` level produces no output for normal operations

#### Command Line Interface
- **New `rhizo` CLI**: Database inspection and verification from command line
  - `rhizo info <path>`: Show database summary (tables, versions, row counts)
  - `rhizo tables <path>`: List all tables
  - `rhizo versions <path> <table>`: List versions of a table
  - `rhizo verify <path>`: Verify database integrity using BLAKE3 hashes
- **`python -m rhizo` support**: Run CLI via Python module
- **Uses stdlib argparse**: No additional dependencies required

### Documentation
- **PERFORMANCE.md**: Added "Configuration" section documenting environment variables
- Updated docstrings for `verify_integrity` parameter across all modules

---

## [0.5.1] - 2026-01-20

### Improved

#### Benchmark Methodology Documentation
- **Footnotes added to README claims**: Performance claims now include context explaining measurement conditions
- **New `real_consensus_benchmark.py`**: Empirical validation against real systems (SQLite WAL, Redis, etcd) rather than simulated delays
- **PERFORMANCE.md expanded**: Added "Benchmark Methodology" section explaining algebraic speedup and OLAP cache conditions
- **TECHNICAL_FOUNDATIONS.md updated**: Added empirical validation reference for energy model

### Documentation
- Energy benchmark docstrings now clarify simulated vs real consensus comparison
- Distributed benchmark docstrings explain what is being measured and why speedups are valid
- Benchmarks README updated with new `real_consensus_benchmark.py` entry

---

## [0.5.0] - 2026-01-19

### Added

#### Coordination-Free Distributed Transactions (Phase CF)
- **Distributed transaction engine** (`rhizo_core::distributed`): Full implementation of coordination-free commits
- **Vector clocks**: Causality tracking for happened-before relationships
- **Gossip protocol**: Anti-entropy propagation between nodes
- **Automatic merge**: Concurrent algebraic operations merge without coordination
- **Convergence guarantees**: Mathematical proof of eventual consistency for algebraic workloads

#### Energy Efficiency Benchmarks
- **CodeCarbon integration**: Precise energy measurement per transaction
- **97,943x energy reduction** vs consensus-based systems (2.2e-11 kWh vs 2.1e-6 kWh per tx)
- **Annual projections**: 730 kWh/year saved at 1M tx/day (292 kg CO2)

### Performance
- **Local commit latency**: 0.022ms (31,000x faster than 100ms consensus)
- **Throughput (2 nodes)**: 255,297 ops/sec
- **Convergence rounds**: 3 (constant regardless of operation count)
- **Mathematical soundness**: Commutativity, associativity, idempotency verified

### Testing
- 370 Rust tests (+64 distributed/coordination-free tests)
- 262 Python tests (+15 energy/distributed tests)
- All algebraic properties experimentally verified

### Documentation
- Technical Foundations updated with coordination-free proofs
- Paper draft complete: "ACID Without Consensus: Algebraic Transactions for Geo-Distributed Data"

---

## [0.4.0] - 2026-01-18

### Added

#### Algebraic Classification for Conflict-Free Merge (Phase AF)
- **Core algebraic module** (`rhizo_core::algebraic`): Complete implementation of CRDT-style algebraic operations
- **OpType enum**: Classification of operations into semilattice (MAX, MIN, UNION, INTERSECT), Abelian (ADD, MULTIPLY), and generic (OVERWRITE, CONDITIONAL, UNKNOWN)
- **AlgebraicValue**: Type-safe wrapper for mergeable values (Integer, Float, StringSet, IntSet, Boolean, Null)
- **AlgebraicMerger**: Stateless merger with mathematical guarantees (commutativity, associativity, idempotency)
- **TableAlgebraicSchema**: Per-table column annotations for merge behavior
- **AlgebraicSchemaRegistry**: Centralized lookup for table/column operation types
- **MergeAnalyzer**: Branch-level merge compatibility analysis
- **Python bindings**: Full PyO3 integration with `PyOpType`, `PyAlgebraicValue`, `algebraic_merge()`, schema classes

### Performance
- **ADD operations**: 4,398 K ops/sec
- **MAX operations**: 4,483 K ops/sec
- **UNION operations**: 745 K ops/sec
- **Schema lookups**: 9,097 K ops/sec

### Testing
- 306 Rust tests (283 unit + 23 integration)
- Comprehensive property verification (commutativity, idempotency, associativity)
- Overflow handling with checked arithmetic
- Null propagation and type mismatch detection
- Branch merge analysis integration tests

### Documentation
- Updated POAC paper (Section 6) with implementation details and benchmark results
- Type stubs for Python IDE support (`_rhizo.pyi`)

---

## [0.3.2] - 2026-01-18

### Fixed
- **Transaction cache invalidation**: Fixed ordering bug where cache invalidation ran after clearing buffered writes, resulting in no-op invalidation
- Cache now properly invalidates tables written during transactions

### Added
- New test `test_conflict_detection_after_epoch_boundary_clear` verifying 3-layer conflict protection works even after epoch boundary clears `recent_committed`

### Testing
- 204 Rust tests (+1 new conflict detection test)
- 247 Python tests
- All tests passing

---

## [0.3.1] - 2026-01-18

### Added

#### Arrow Chunk Cache (Phase P.5)
- `ArrowChunkCache` class for caching decoded Arrow RecordBatches
- LRU eviction with configurable size limit (default: 100MB)
- **15x faster repeated reads** (0.24ms vs 3.6ms uncached)
- Content-addressed caching leverages immutable chunks (no invalidation needed)
- Cache shared across tables, versions, and branches
- 17 new unit tests for cache functionality

### Changed
- `TableReader` now has caching enabled by default
- New parameters: `enable_chunk_cache`, `chunk_cache_size_mb`
- New methods: `cache_stats()`, `clear_cache()`

### Performance
- **Arrow cache read**: 0.24ms (15x faster than uncached)
- **Cache hit rate**: 91%+ for typical workloads
- **Memory overhead**: Configurable, default 100MB

---

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
- **450,000x better branching overhead** (~140 bytes vs 63 MB)

### Testing
- 181 Rust tests (+8 new projection tests, +13 Parquet tests)
- 38 Query Layer Python tests (+7 projection pushdown tests)
- Full competition benchmark against Delta Lake, Iceberg, and Hudi

---

## [0.1.0] - 2026-01-17

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

## What's Next

See [ROADMAP.md](./ROADMAP.md) for current status and planned features.
