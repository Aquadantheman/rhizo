# Rhizo Development Roadmap

## Executive Summary

Rhizo is the next generation of data infrastructure—one system that replaces the fragmented landscape of transactional databases, data warehouses, streaming platforms, and feature stores. This is not incremental improvement; it's architectural unification.

**Current Progress:** Phases 1-4 complete. We have a working proof-of-concept with content-addressable storage, versioned catalogs, time travel queries, DuckDB integration, and Git-like branching with zero-copy semantics.

**Tech Stack:**
- **Core:** Rust (performance, safety, concurrency)
- **Bindings:** PyO3 (Python interoperability)
- **Query Engine:** DuckDB (via Python)
- **Hashing:** BLAKE3
- **Serialization:** Apache Arrow / Parquet

---

## Progress Overview

| Phase | Status | Key Deliverable |
|-------|--------|-----------------|
| Phase 0: Environment | Complete | Rust + PyO3 working |
| Phase 1: Chunk Store | Complete | Content-addressable storage with deduplication |
| Phase 2: Catalog | Complete | Versioned tables with time travel |
| Phase 3: Query Layer | Complete | DuckDB + SQL + time travel queries |
| Phase 4: Branching | Complete | Git-like branches with zero-copy semantics |
| Phase 5: Transactions | Complete | Cross-table ACID with recovery & robustness |
| Phase 6: Changelog | Complete | Unified batch/stream via subscriptions |
| Phase 6.5: QC & CI | Complete | Ruff + Clippy linting, GitHub Actions |
| Phase P: Performance | Complete | Native Rust Parquet, parallel I/O |
| Phase DF: OLAP | Complete | DataFusion engine, time travel SQL, changelog SQL |
| Phase 7: Production | Planned | Real workload migration |
| Phase 8: Release | Planned | Documentation and publication |
| Phase 9: Advanced Storage | Future | Delta encoding, CDC chunking |

---

## COMPLETED PHASES

### Phase 0: Environment & Rust Foundations

**Status:** Complete

- [x] Rust toolchain installed and configured
- [x] VS Code with rust-analyzer
- [x] Cargo workspace with `rhizo_core` and `rhizo_python`
- [x] PyO3 bindings working (`maturin develop`)
- [x] Git repository initialized and pushed to GitHub

### Phase 1: Content-Addressable Chunk Store

**Status:** Complete (22 Rust tests passing)

**What We Built:**
- [x] `ChunkStore` struct with BLAKE3 hashing
- [x] Hash-based path derivation (`ab/cd/abcdef...`)
- [x] Atomic writes (write-to-temp-rename pattern)
- [x] Hash validation (64 hex character format)
- [x] `get_verified()` with integrity checking
- [x] Deduplication (same content → same hash → single copy)
- [x] Python bindings (`PyChunkStore`)
- [x] Comprehensive error types (`HashMismatch`, `NotFound`, etc.)

**Key Files:**
- `rhizo_core/src/chunk_store/store.rs`
- `rhizo_core/src/chunk_store/error.rs`
- `rhizo_python/src/lib.rs`

### Phase 2: Table Catalog & Versioning

**Status:** Complete

**What We Built:**
- [x] `TableVersion` struct with metadata
- [x] `FileCatalog` with JSON-based version storage
- [x] Sequential version enforcement
- [x] Time travel (`get_version(table, Some(v))`)
- [x] Latest version resolution
- [x] Atomic commits with latest pointer
- [x] `list_versions()` and `list_tables()`
- [x] Python bindings (`PyCatalog`, `PyTableVersion`)

**Key Files:**
- `rhizo_core/src/catalog/file_catalog.rs`
- `rhizo_core/src/catalog/version.rs`
- `rhizo_core/src/catalog/error.rs`

### Phase 3: Query Layer (DuckDB Integration)

**Status:** Complete (26 Python tests passing, 46 total)

**What We Built:**
- [x] `TableWriter` - DataFrame/Arrow → Parquet chunks → catalog
- [x] `TableReader` - catalog → chunks → Arrow/DataFrame
- [x] `QueryEngine` - DuckDB wrapper with time travel
- [x] SQL queries over versioned tables
- [x] Time travel in queries (`versions={"table": 1}`)
- [x] `diff_versions()` for comparing versions
- [x] JOIN queries across multiple tables
- [x] Multiple result formats (pandas, Arrow, dict)
- [x] Chunking for large tables
- [x] Type stubs for IDE support (`_rhizo.pyi`)

**Key Files:**
- `python/rhizo/writer.py`
- `python/rhizo/reader.py`
- `python/rhizo/engine.py`
- `tests/test_query_layer.py`

---

## RECENTLY COMPLETED

### Phase 4: Branching

**Status:** Complete

**Goal:** Git-like branching for data development workflows

**Why This Matters:**
- Experiment with data transformations without affecting production
- A/B test algorithm changes on real data
- Collaborate on data changes with isolated workspaces
- Zero storage cost until changes are made (content addressing!)

**Data Model:**
```
Branch:
  - name: String
  - head: HashMap<String, u64>  # table_name → version
  - created_at: Timestamp
  - parent_branch: Option<String>
  - description: Option<String>
```

**Completed:**

- [x] **4.1** Define `Branch` struct in Rust
  - `rhizo_core/src/branch/branch.rs`
  - Includes `BranchDiff` for comparing branches

- [x] **4.2** Implement `BranchManager`
  - `rhizo_core/src/branch/manager.rs`
  - Full CRUD: create, get, list, delete
  - Head operations: update_head, get_table_version
  - Default branch management

- [x] **4.3** Implement branch diff
  - Compare head pointers
  - Report: unchanged, modified, added_in_source, added_in_target
  - Conflict detection

- [x] **4.4** Implement fast-forward merge
  - If target is ancestor of source: update pointers
  - If diverged: `BranchError::MergeConflict` with table list

- [x] **4.5** Zero-copy branch creation
  - Verified: 50 branches created in < 1 second
  - Branch metadata < 1KB per branch

- [x] **4.6** Add Python bindings
  ```python
  manager = udr.PyBranchManager("./data/branches")
  manager.create("feature/new-scoring", from_branch="main")
  manager.list()
  manager.get("feature/new-scoring")
  manager.diff("feature/new-scoring", "main")
  manager.merge("feature/new-scoring", into="main")
  ```

- [x] **4.7** Write tests (20 Python tests)
  - Branch creation and isolation
  - Head operations
  - Diff and merge
  - Zero-copy verification
  - Branch name validation

- [x] **4.8** QueryEngine integration (15 new tests)
  - Add `branch_manager` parameter to `QueryEngine.__init__`
  - Add `current_branch` state (default: "main")
  - Add `checkout(branch_name)` method
  - Update `_ensure_registered()` to resolve via branch heads
  - Update `write_table()` to update branch heads
  - Add `branch` parameter to `query()` method
  - Backward compatible (works without branch_manager)

**Merge Strategy Evolution:**

| Phase | Strategy | Capability |
|-------|----------|------------|
| Phase 4 (now) | Fast-forward + conflict detection | Simple merges work; diverged branches report conflict |
| Phase 4.5 (future) | Three-way merge | Full merge with ancestor tracking |
| Phase 5+ | Transactional merge | Atomic cross-table merge with MVCC |

**Milestone Checkpoint:**
- [x] Create branch from main
- [x] Modify head pointers on branch
- [x] Verify branch isolation
- [x] Diff branches
- [x] Merge back to main (fast-forward)
- [x] Storage increase ≈ 0 (verified)
- [x] Query via branch-aware QueryEngine

---

## RECENTLY COMPLETED

### Phase 5: Cross-Table Transactions

**Goal:** ACID transactions across multiple tables

**Why This Matters:**
- Update customer + orders + audit log atomically
- No more saga patterns and compensation logic
- Snapshot isolation: readers never block

**Design Decisions:**
- **Concurrency Control:** Optimistic (MVCC)
- **Isolation Level:** Snapshot Isolation
- **Scope:** Single-process first, distributed later

#### Phase 5.0: Core Transaction Infrastructure (Complete)

**What We Built:**
- [x] `TransactionRecord` struct with read/write sets
- [x] `TransactionManager` with conflict detection
- [x] `TransactionLog` - Epoch-based WAL with persistence
- [x] `EpochConfig` - Configurable epoch modes (single_node, high_throughput, low_latency)
- [x] `ConflictDetector` trait with `TableLevelConflictDetector`
- [x] `RecoveryManager` for crash recovery
- [x] Python bindings (`PyTransactionManager`, `PyTransactionInfo`, `PyRecoveryReport`)
- [x] Type stubs in `_rhizo.pyi`

**Key Files:**
- `rhizo_core/src/transaction/types.rs` - Core types (TxId, TransactionRecord, WriteGranularity)
- `rhizo_core/src/transaction/epoch.rs` - Epoch configuration and metadata
- `rhizo_core/src/transaction/error.rs` - TransactionError types
- `rhizo_core/src/transaction/log.rs` - Persistent transaction log
- `rhizo_core/src/transaction/conflict.rs` - Conflict detection (table-level)
- `rhizo_core/src/transaction/manager.rs` - TransactionManager coordinator
- `rhizo_core/src/transaction/recovery.rs` - RecoveryManager

**Test Count:** 71 new Rust tests (110 total)

#### Phase 5.1: QueryEngine Integration (Complete)

**Goal:** Seamless Python API with context manager

**What We Built:**
- [x] `engine.transaction()` context manager
- [x] `TransactionContext` class with query/write_table methods
- [x] Read-your-writes within transaction (buffered writes visible to queries)
- [x] Automatic rollback on exception
- [x] Branch head auto-updates on commit
- [x] Snapshot conflict detection

**Key Files:**
- `python/rhizo/transaction.py` - TransactionContext class
- `python/rhizo/engine.py` - Added transaction() method
- `python/rhizo/writer.py` - Added write_chunks_only() for transactions
- `tests/test_transactions.py` - 28 integration tests

**Test Count:** 28 new Python tests (109 total Python, 219 total)

**Example API:**
```python
with engine.transaction() as tx:
    # Query data (sees snapshot at transaction start)
    users = tx.query("SELECT * FROM users")

    # Buffer writes (not visible outside transaction)
    tx.write_table("users", updated_df)
    tx.write_table("audit_log", log_df)

    # Read-your-writes: see buffered data
    count = tx.query("SELECT COUNT(*) FROM users")

    # Commits atomically on exit
# Rolls back on exception
```

#### Phase 5.2: Recovery & Robustness (Complete)

**Goal:** Production-grade crash recovery

**What We Built:**
- [x] Python-level recovery integration via `PyTransactionManager.recover()` and `recover_and_apply()`
- [x] Consistency verification via `verify_consistency()` method
- [x] Auto-recovery option on `PyTransactionManager.__init__(auto_recover=True)`
- [x] QueryEngine convenience methods: `verify_integrity()` and `recover()`
- [x] Comprehensive recovery test suite (22 Python tests)

**Key Features:**
- `recover()` - Scan transaction log, identify incomplete transactions (read-only)
- `recover_and_apply()` - Mark pending transactions as aborted (for startup)
- `verify_consistency()` - Check transaction log integrity
- `auto_recover` parameter - Automatically run recovery on TransactionManager init

**Key Files:**
- `rhizo_core/src/transaction/manager.rs` - Added recover(), recover_and_apply(), verify_consistency()
- `rhizo_python/src/lib.rs` - Updated Python bindings with recovery methods
- `python/rhizo/engine.py` - Added verify_integrity() and recover() convenience methods
- `tests/test_recovery.py` - 22 recovery integration tests

**Test Count:** 22 new Python tests (131 total Python, 241 total)

**Example API:**
```python
# Auto-recovery on startup
tx_manager = udr.PyTransactionManager(
    tx_path, catalog_path, branch_path,
    auto_recover=True  # Automatically clean up after crash
)

# Manual recovery via QueryEngine
engine = QueryEngine(store, catalog, transaction_manager=tx_manager)
report = engine.recover()
if not report["is_clean"]:
    print(f"Recovered transactions: {report['rolled_back']}")

# Verify system integrity
health = engine.verify_integrity()
if not health["is_healthy"]:
    print(f"Issues found: {health['issues']}")
```

---

## RECENTLY COMPLETED

### Phase 6: Changelog & Subscriptions

**Status:** Complete

**Goal:** Unified batch and streaming via changelog

**Why This Matters:**
- Batch query: "What is the state at version V?" → `engine.query()`
- Stream query: "What changed since version V?" → `engine.get_changes()`
- Continuous stream: "Notify me of changes" → `engine.subscribe()`
- Same data model, same guarantees, no separate Kafka needed

**What We Built:**

#### Phase 6.0: Core Changelog API (Rust)
- [x] `ChangelogEntry` struct with table changes
- [x] `TableChange` struct tracking old/new versions
- [x] `ChangelogQuery` builder with filtering (tx_id, timestamp, tables, branch)
- [x] `TransactionLog.list_committed_transactions()` and `latest_committed_tx_id()`
- [x] `TransactionManager.get_changelog()` and `latest_tx_id()`
- [x] 17 new Rust tests (127 total)

**Key Files:**
- `rhizo_core/src/changelog/entry.rs` - ChangelogEntry, TableChange
- `rhizo_core/src/changelog/query.rs` - ChangelogQuery builder
- `rhizo_core/src/changelog/tests.rs` - Comprehensive tests

#### Phase 6.1: Python Bindings
- [x] `PyChangelogEntry` with helper methods (changed_tables, contains_table, get_change)
- [x] `PyTableChange` with is_new_table()
- [x] `PyTransactionManager.get_changelog()` and `latest_tx_id()`
- [x] Type stubs in `_rhizo.pyi`

#### Phase 6.2: Subscriber API (Python)
- [x] `Subscriber` class with multiple interfaces:
  - `poll()` - Non-blocking check for new events
  - `__iter__()` - Blocking iterator for continuous processing
  - `subscribe(callback)` - Blocking callback interface
  - `start_background(callback)` / `stop()` - Background thread processing
- [x] `ChangeEvent` dataclass for per-table change notifications
- [x] QueryEngine integration: `get_changes()`, `subscribe()`, `latest_tx_id()`
- [x] 24 new Python tests (155 total)

**Key Files:**
- `python/rhizo/subscriber.py` - Subscriber class, ChangeEvent
- `python/rhizo/engine.py` - get_changes(), subscribe(), latest_tx_id()
- `tests/test_changelog.py` - Comprehensive tests

#### Phase 6.3: Demo & Documentation
- [x] `examples/changelog_demo.py` - Interactive demo showing all patterns
- [x] Updated README and roadmap

**Test Count:** 127 Rust tests, 155 Python tests (282 total)

**Example API:**
```python
# BATCH: What is the current state?
result = engine.query("SELECT * FROM orders WHERE status = 'pending'")

# CHANGE QUERY: What changed since last check?
changes = engine.get_changes(since_tx_id=checkpoint)
for entry in changes:
    for change in entry['changes']:
        print(f"{change['table_name']}: v{change['old_version']} -> v{change['new_version']}")

# STREAMING: Continuous change notifications
for event in engine.subscribe(tables=["orders"]):
    print(f"Change: {event.table_name} v{event.new_version}")
    process_event(event)

# BACKGROUND: Event-driven processing
def on_change(event):
    trigger_downstream(event)

subscriber = engine.subscribe()
subscriber.start_background(on_change)
```

---

## RECENTLY COMPLETED

### Phase 6.5: Quality Control & CI

**Status:** Complete

**Goal:** Establish gentle linting and automated CI

**Why This Matters:**
- Catch issues early before they become habits
- Ensure consistent code quality across Rust and Python
- Automated verification on every PR

**What We Built:**

#### Ruff Linting (Python)
- [x] Gentle configuration in `pyproject.toml`
- [x] Essential checks only: `E` (errors), `F` (pyflakes), `W` (warnings)
- [x] Per-file ignores for `__init__.py` and tests
- [x] All Python code passes (155 tests, 0 lint errors)

#### Clippy Linting (Rust)
- [x] Clean codebase with no warnings
- [x] `#[allow(dead_code)]` annotations for planned-but-unused features
- [x] All Rust code passes (127 tests, 0 clippy warnings)

#### GitHub Actions CI
- [x] `.github/workflows/ci.yml` - Automated CI on push/PR
- [x] Rust job: Clippy + cargo test
- [x] Python job: Ruff + pytest

**Commands:**
```bash
# Linting
cargo clippy --all       # Rust - should be clean
python -m ruff check .   # Python - should be clean

# Tests
cargo test --all         # 127 tests
pytest tests/ -v         # 155 tests
```

---

## RECENTLY COMPLETED

### Phase P: Performance Optimization

**Status:** Complete

**Goal:** Close the performance gap with Delta Lake while preserving architectural advantages

**What We Built:**
- [x] P.1: Parallel chunk I/O with Rayon (3-5x batch throughput)
- [x] P.2: Memory-mapped reads (infrastructure for zero-copy)
- [x] P.3: Parallel Parquet parsing (2.1x multi-chunk speedup)
- [x] P.4: Native Rust Parquet encoder (2.3x write improvement)

**Results:**
- Write throughput: 211 MB/s (competitive with Delta Lake)
- 84% storage deduplication (best in class)
- 52,500x better branching overhead (280 bytes vs 14.70 MB)

**Documentation:** See [Performance Optimization Roadmap](./docs/PERFORMANCE_OPTIMIZATION_ROADMAP.md)

---

### Phase DF: DataFusion OLAP Integration

**Status:** Complete

**Goal:** Native OLAP engine for high-performance analytical queries

**What We Built:**
- [x] DF.1: DataFusion integration with LRU cache
- [x] DF.2: Parallel table loading with ThreadPoolExecutor
- [x] DF.3: Extended SQL syntax (VERSION keyword, @branch notation)
- [x] DF.4: Changelog SQL via virtual __changelog table

**Results:**
- **26x faster reads** than DuckDB (0.9ms vs 23.8ms at 100K rows)
- **50x faster reads** at 1M scale (5.1ms vs 257.2ms)
- Time travel SQL: `SELECT * FROM users VERSION 5`
- Branch queries: `SELECT * FROM users@feature-branch`
- CDC via SQL: `SELECT * FROM __changelog WHERE table_name = 'users'`

**Documentation:** See [DataFusion Integration Roadmap](./docs/DATAFUSION_INTEGRATION_ROADMAP.md)

---

## PLANNED PHASES

### Phase 7: Production Migration

**Goal:** Run real workloads on Rhizo

**Key Steps:**
1. Audit current data storage and flows
2. Design Rhizo table schema
3. Zero-copy import from existing Parquet/Iceberg
4. Adapt queries to Rhizo
5. Parallel operation and validation
6. Cutover with rollback path

### Phase 8: Documentation & Release

**Goal:** Publishable, usable by others

**Deliverables:**
- Comprehensive README (done!)
- API documentation (Rust docs, Python docstrings)
- Tutorial notebooks
- Architecture document
- Demo video/GIF
- Blog post
- GitHub Actions CI/CD (done in Phase 6.5!)

---

### Phase 9: Advanced Storage Optimization (Future)

**Goal:** Dramatically reduce storage for versioned data

**Techniques to Explore:**
1. **Delta Encoding** - Store diffs between chunk versions (like git packfiles)
2. **Content-Defined Chunking (CDC)** - Rolling hash for natural boundaries (like rsync)
3. **Column-Level Deltas** - Store only changed columns
4. **Tiered Storage** - Hot data full, cold data delta-compressed

**Potential Impact:**
| Scenario | Current | With Delta Encoding |
|----------|---------|---------------------|
| 1% change/version | ~1% new storage | ~0.01% new storage |
| 10% change/version | ~10% new storage | ~1% new storage |

**Priority:** Low - current deduplication is sufficient for launch. Revisit after production validation.

---

## Scaling Strategy

### Horizontal Scaling Path

```
Phase 1-3: Single-node POC (Complete)
    ↓
Phase 4-6: Single-node with full features ← WE ARE HERE
    ↓
Phase 7: Production validation
    ↓
Future: Distributed deployment
```

**Distributed Architecture (Future):**
- Consistent hashing for chunk distribution
- Raft consensus for catalog coordination
- Epoch-based transaction ordering
- Read replicas for query scaling

### Storage Scaling

| Scale | Strategy | Notes |
|-------|----------|-------|
| GB | Local filesystem | Current implementation |
| TB | Local SSD + cold storage | Tiered storage |
| PB | Distributed chunk store | S3/GCS backend |
| EB | Federated deployments | Multi-region |

### Query Scaling

| Scale | Strategy | Notes |
|-------|----------|-------|
| Simple | Single DuckDB | Current implementation |
| Complex | DuckDB + caching | Materialized views |
| Large | Distributed query | Trino/Spark integration |
| Real-time | Streaming layer | Kafka/Flink integration |

---

## Risk Mitigation

| Risk | Mitigation |
|------|------------|
| Complexity growth | Phase-based delivery with clear milestones |
| Performance issues | Benchmark early, profile before optimizing |
| Scope creep | "Nice to have" explicitly separated |
| Lock-in | Open standards (Arrow, Parquet, SQL) |
| Single point of failure | Content addressing enables federation |

---

## Mathematical Foundations (Proven)

From the whitepaper verification:

| Property | Complexity | Verified |
|----------|------------|----------|
| Write | O(n) in data size | Yes |
| Read | O(n) in data size | Yes |
| Time travel | O(1) version lookup | Yes |
| Branch creation | O(k) where k = tables | Yes |
| Collision probability | ~10^-47 at exabyte scale | Yes |
| Deduplication ratio | 60-85% typical | Yes |

---

## Decision Log

### 2026-01-16: Phase 4 Branching Design
**Decision:** Git-style branch names with slashes, implicit "main" branch, fast-forward merge with evolution to three-way merge
**Rationale:**
- Slash-based names (e.g., `feature/scoring-v2`) are familiar to developers
- Implicit "main" branch ensures backward compatibility with branchless code
- Fast-forward merge is simple and sufficient for POC; three-way merge added after proving viability
**Alternatives:**
- Hyphen-only names (rejected for familiarity)
- Explicit branch required (rejected for backward compatibility)
- Full three-way merge now (rejected for complexity)

### 2026-01-16: Phase 3 Architecture
**Decision:** Build query layer in Python with DuckDB, not Rust
**Rationale:** Faster iteration, better ecosystem integration, DuckDB handles heavy lifting
**Alternatives:** DataFusion (Rust-native), direct Parquet reading

### 2026-01-16: Phase 1 Hardening
**Decision:** Add hash validation and atomic writes before Phase 3
**Rationale:** Foundation must be solid before building query layer
**Alternatives:** Move fast, fix later (rejected for robustness)

### 2026-01-16: PyO3 0.23 Upgrade
**Decision:** Upgrade from PyO3 0.20 to 0.23
**Rationale:** Better ergonomics, `Bound<'py, T>` API, future-proof
**Alternatives:** Stay on 0.20 (rejected due to deprecation warnings)

---

## Quick Reference

**Build Commands:**
```bash
# Rust
cargo build --release
cargo test --all
cargo clippy --all       # Linting

# Python bindings
cd rhizo_python && maturin develop --release

# Python tests & linting
pytest tests/ -v
python -m ruff check .   # Linting
```

**Key Imports:**
```python
import _rhizo
from rhizo import TableWriter, TableReader, QueryEngine
```

**Test Counts:**
- Rust: 204 tests
- Python: 247 tests (including OLAP, time travel SQL, changelog SQL)

---

*Last updated: January 2026*
