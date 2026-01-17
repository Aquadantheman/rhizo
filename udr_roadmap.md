# Unified Data Runtime (UDR) Development Roadmap

## Executive Summary

UDR is building the next generation of data infrastructure—one system that replaces the fragmented landscape of transactional databases, data warehouses, streaming platforms, and feature stores. This is not incremental improvement; it's architectural unification.

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
| Phase 0: Environment | ✅ Complete | Rust + PyO3 working |
| Phase 1: Chunk Store | ✅ Complete | Content-addressable storage with deduplication |
| Phase 2: Catalog | ✅ Complete | Versioned tables with time travel |
| Phase 3: Query Layer | ✅ Complete | DuckDB + SQL + time travel queries |
| Phase 4: Branching | ✅ Complete | Git-like branches with zero-copy semantics |
| Phase 5: Transactions | 🚧 In Progress | Cross-table ACID (Core complete, QueryEngine integration next) |
| Phase 6: Changelog | ⏳ Planned | Unified batch/stream |
| Phase 7: Production | ⏳ Planned | Real workload migration |
| Phase 8: Release | ⏳ Planned | Documentation and publication |

---

## ✅ COMPLETED PHASES

### Phase 0: Environment & Rust Foundations ✅

**Status:** Complete

- [x] Rust toolchain installed and configured
- [x] VS Code with rust-analyzer
- [x] Cargo workspace with `udr_core` and `udr_python`
- [x] PyO3 bindings working (`maturin develop`)
- [x] Git repository initialized and pushed to GitHub

### Phase 1: Content-Addressable Chunk Store ✅

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
- `udr_core/src/chunk_store/store.rs`
- `udr_core/src/chunk_store/error.rs`
- `udr_python/src/lib.rs`

### Phase 2: Table Catalog & Versioning ✅

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
- `udr_core/src/catalog/file_catalog.rs`
- `udr_core/src/catalog/version.rs`
- `udr_core/src/catalog/error.rs`

### Phase 3: Query Layer (DuckDB Integration) ✅

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
- [x] Type stubs for IDE support (`udr.pyi`)

**Key Files:**
- `python/udr_query/writer.py`
- `python/udr_query/reader.py`
- `python/udr_query/engine.py`
- `tests/test_query_layer.py`

---

## ✅ RECENTLY COMPLETED

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
  - `udr_core/src/branch/branch.rs`
  - Includes `BranchDiff` for comparing branches

- [x] **4.2** Implement `BranchManager`
  - `udr_core/src/branch/manager.rs`
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

## 🚧 IN PROGRESS

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

#### Phase 5.0: Core Transaction Infrastructure ✅ COMPLETE

**What We Built:**
- [x] `TransactionRecord` struct with read/write sets
- [x] `TransactionManager` with conflict detection
- [x] `TransactionLog` - Epoch-based WAL with persistence
- [x] `EpochConfig` - Configurable epoch modes (single_node, high_throughput, low_latency)
- [x] `ConflictDetector` trait with `TableLevelConflictDetector`
- [x] `RecoveryManager` for crash recovery
- [x] Python bindings (`PyTransactionManager`, `PyTransactionInfo`, `PyRecoveryReport`)
- [x] Type stubs in `udr.pyi`

**Key Files:**
- `udr_core/src/transaction/types.rs` - Core types (TxId, TransactionRecord, WriteGranularity)
- `udr_core/src/transaction/epoch.rs` - Epoch configuration and metadata
- `udr_core/src/transaction/error.rs` - TransactionError types
- `udr_core/src/transaction/log.rs` - Persistent transaction log
- `udr_core/src/transaction/conflict.rs` - Conflict detection (table-level)
- `udr_core/src/transaction/manager.rs` - TransactionManager coordinator
- `udr_core/src/transaction/recovery.rs` - RecoveryManager

**Test Count:** 71 new Rust tests (110 total)

#### Phase 5.1: QueryEngine Integration (Next)

**Goal:** Seamless Python API with context manager

**Planned:**
- [ ] `engine.transaction()` context manager
- [ ] Read-your-writes within transaction
- [ ] Rollback on exception
- [ ] Branch head auto-updates

**Example API:**
```python
with engine.transaction() as tx:
    parcels = tx.read_table("parcels")
    scores = tx.read_table("scores")

    # Modify
    tx.write_table("parcels", new_parcels)
    tx.write_table("scores", new_scores)

    # Commits atomically on exit
# Rolls back on exception
```

---

## ⏳ PLANNED PHASES

### Phase 6: Changelog & Subscriptions

**Goal:** Unified batch and streaming via changelog

**Why This Matters:**
- Batch query: "What is the state at version V?"
- Stream query: "What changed since version V?"
- Same data model, same guarantees

**Key Components:**
1. `ChangelogEntry` for each commit
2. `Subscriber` API for change notifications
3. `diff_versions()` for computing deltas

**Example API:**
```python
def on_change(entry):
    for change in entry.changes:
        if change.table_name == "parcels":
            print(f"Parcels: v{change.old_version} → v{change.new_version}")

subscriber = udr.Subscriber(changelog, from_commit=100)
subscriber.subscribe(on_change)
```

### Phase 7: Production Migration

**Goal:** Run real workloads on UDR

**Key Steps:**
1. Audit current data storage and flows
2. Design UDR table schema
3. Zero-copy import from existing Parquet/Iceberg
4. Adapt queries to UDR
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
- GitHub Actions CI/CD

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
| Write | O(n) in data size | ✅ |
| Read | O(n) in data size | ✅ |
| Time travel | O(1) version lookup | ✅ |
| Branch creation | O(k) where k = tables | ✅ |
| Collision probability | ~10^-47 at exabyte scale | ✅ |
| Deduplication ratio | 60-85% typical | ✅ |

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

# Python bindings
cd udr_python && maturin develop --release

# Python tests
pytest tests/ -v
```

**Key Imports:**
```python
import udr
from udr_query import TableWriter, TableReader, QueryEngine
```

**Test Counts:**
- Rust: 110 tests (22 core + 17 branch + 71 transaction)
- Python: 81 tests (20 core + 26 query layer + 20 branching + 15 branch-query integration)

---

*Last updated: January 2026*
