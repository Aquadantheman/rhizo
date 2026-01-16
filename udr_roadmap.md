# Unified Data Runtime (UDR) Development Roadmap

## Executive Summary

UDR is building the next generation of data infrastructure—one system that replaces the fragmented landscape of transactional databases, data warehouses, streaming platforms, and feature stores. This is not incremental improvement; it's architectural unification.

**Current Progress:** Phases 1-3 complete. We have a working proof-of-concept with content-addressable storage, versioned catalogs, time travel queries, and DuckDB integration.

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
| Phase 4: Branching | 🔄 Next | Git-like branches for data |
| Phase 5: Transactions | ⏳ Planned | Cross-table ACID |
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

## 🔄 NEXT PHASE

### Phase 4: Branching

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
```

**Action Items:**

- [ ] **4.1** Define `Branch` struct in Rust
  ```rust
  #[derive(Debug, Clone, Serialize, Deserialize)]
  pub struct Branch {
      pub name: String,
      pub head: HashMap<String, u64>,
      pub created_at: i64,
      pub parent_branch: Option<String>,
  }
  ```

- [ ] **4.2** Implement `BranchManager`
  ```rust
  impl BranchManager {
      pub fn create(&self, name: &str, from: Option<&str>) -> Result<Branch>;
      pub fn get(&self, name: &str) -> Result<Branch>;
      pub fn update_head(&self, name: &str, table: &str, version: u64) -> Result<()>;
      pub fn list(&self) -> Result<Vec<String>>;
      pub fn delete(&self, name: &str) -> Result<()>;
  }
  ```

- [ ] **4.3** Implement branch-aware catalog
  - Wrap `FileCatalog` with branch context
  - `get_version()` resolves via branch head
  - `commit()` updates branch head

- [ ] **4.4** Zero-copy branch creation
  - Copy only head pointers, not data
  - Verify no storage increase

- [ ] **4.5** Implement branch diff
  ```rust
  pub fn diff(branch_a: &str, branch_b: &str) -> BranchDiff {
      // Compare head pointers
      // Report: same, different, added, removed tables
  }
  ```

- [ ] **4.6** Implement fast-forward merge
  - If target is ancestor of source: update pointers
  - If diverged: report conflict

- [ ] **4.7** Add Python bindings
  ```python
  manager.create_branch("feature/new-scoring")
  manager.list_branches()
  manager.checkout("feature/new-scoring")
  manager.diff("main", "feature/new-scoring")
  manager.merge("feature/new-scoring", into="main")
  ```

- [ ] **4.8** Write tests
  - Create branch, modify, verify isolation
  - Branch storage overhead = 0
  - Diff shows correct changes
  - Merge updates target

**Milestone Checkpoint:**
- [ ] Create branch from main
- [ ] Modify data on branch
- [ ] Query both branches, see different results
- [ ] Merge back to main
- [ ] Storage increase ≈ 0 until actual changes

---

## ⏳ PLANNED PHASES

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

**Key Components:**
1. `Transaction` struct with read/write sets
2. `TransactionManager` with conflict detection
3. Write-ahead logging (WAL) for recovery
4. Python context manager interface

**Example API:**
```python
with udr.transaction(store, catalog) as tx:
    parcels = tx.read_table("parcels")
    scores = tx.read_table("scores")

    # Modify
    tx.write_table("parcels", new_parcels)
    tx.write_table("scores", new_scores)

    # Commits atomically on exit
# Rolls back on exception
```

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
Phase 1-3: Single-node POC ← WE ARE HERE
    ↓
Phase 4-6: Single-node with full features
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
- Rust: 22 tests
- Python: 46 tests (20 core + 26 query layer)

---

*Last updated: January 2026*
