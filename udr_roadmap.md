# Unified Data Runtime (UDR) Development Roadmap

## Project Overview

**Goal:** Build a working Unified Data Runtime that powers Lotitude and demonstrates the core architectural concepts from the whitepaper.

**Tech Stack:**
- **Core:** Rust (performance, safety, concurrency)
- **Bindings:** PyO3 (Python interoperability)
- **Query Engine:** DuckDB (via Python initially)
- **Hashing:** BLAKE3
- **Serialization:** Apache Arrow / Parquet
- **CLI:** Python (rapid iteration) or Rust (clap)

**Timeline:** 8-10 weeks to production-ready for Lotitude

**Repository:** `C:\Users\linde\dev\unifieddataruntime`

---

## Phase 0: Environment & Rust Foundations
**Duration:** Week 1 (7-10 days)
**Goal:** Development environment ready, basic Rust proficiency, project skeleton

### Learning Track (Parallel with Setup)

#### Action Items

- [ ] **0.1** Install Rust toolchain
  ```powershell
  # Download and run rustup-init.exe from https://rustup.rs
  rustup --version
  cargo --version
  ```

- [ ] **0.2** Install VS Code extensions
  - rust-analyzer (essential)
  - CodeLLDB (debugging)
  - crates (dependency management)
  - Even Better TOML (config files)

- [ ] **0.3** Complete Rust fundamentals (3-4 days)
  - Resource: [Rust Book Chapters 1-10](https://doc.rust-lang.org/book/)
  - Focus areas:
    - Ownership & borrowing (Ch 4) — **critical**
    - Structs & enums (Ch 5-6)
    - Error handling with Result (Ch 9)
    - Generics & traits (Ch 10)
  - Practice: Rustlings exercises (`cargo install rustlings`)

- [ ] **0.4** Learn Rust for data/systems (2 days)
  - File I/O: `std::fs`, `std::path`
  - Serialization: serde basics
  - Concurrency: `std::sync`, `Arc`, `Mutex` (overview)

### Project Setup Track

#### Action Items

- [ ] **0.5** Initialize Rust workspace
  ```powershell
  cd C:\Users\linde\dev\unifieddataruntime
  cargo init --name udr_core --lib
  ```

- [ ] **0.6** Set up project structure
  ```
  unifieddataruntime/
  ├── Cargo.toml              # Workspace root
  ├── README.md
  ├── .gitignore
  ├── udr_core/               # Rust core library
  │   ├── Cargo.toml
  │   └── src/
  │       ├── lib.rs
  │       ├── chunk_store/
  │       ├── catalog/
  │       ├── transaction/
  │       └── branch/
  ├── udr_python/             # PyO3 bindings
  │   ├── Cargo.toml
  │   ├── pyproject.toml
  │   └── src/
  │       └── lib.rs
  ├── tests/                  # Integration tests
  ├── benches/                # Benchmarks
  └── examples/               # Example usage
  ```

- [ ] **0.7** Configure Cargo.toml (workspace)
  ```toml
  [workspace]
  members = ["udr_core", "udr_python"]
  resolver = "2"

  [workspace.dependencies]
  blake3 = "1.5"
  serde = { version = "1.0", features = ["derive"] }
  serde_json = "1.0"
  thiserror = "1.0"
  parking_lot = "0.12"        # Better mutexes
  bytes = "1.5"
  ```

- [ ] **0.8** Configure udr_core/Cargo.toml
  ```toml
  [package]
  name = "udr_core"
  version = "0.1.0"
  edition = "2021"

  [dependencies]
  blake3 = { workspace = true }
  serde = { workspace = true }
  serde_json = { workspace = true }
  thiserror = { workspace = true }
  parking_lot = { workspace = true }
  bytes = { workspace = true }
  ```

- [ ] **0.9** Set up PyO3 bindings skeleton
  ```toml
  # udr_python/Cargo.toml
  [package]
  name = "udr_python"
  version = "0.1.0"
  edition = "2021"

  [lib]
  name = "udr"
  crate-type = ["cdylib"]

  [dependencies]
  pyo3 = { version = "0.20", features = ["extension-module"] }
  udr_core = { path = "../udr_core" }
  ```

- [ ] **0.10** Verify PyO3 setup with hello world
  ```rust
  // udr_python/src/lib.rs
  use pyo3::prelude::*;

  #[pyfunction]
  fn hello() -> PyResult<String> {
      Ok("Hello from UDR!".to_string())
  }

  #[pymodule]
  fn udr(_py: Python, m: &PyModule) -> PyResult<()> {
      m.add_function(wrap_pyfunction!(hello, m)?)?;
      Ok(())
  }
  ```

- [ ] **0.11** Set up maturin for building Python package
  ```powershell
  pip install maturin
  cd udr_python
  maturin develop
  python -c "import udr; print(udr.hello())"
  ```

- [ ] **0.12** Initialize git repository
  ```powershell
  git init
  git add .
  git commit -m "Initial project structure"
  ```

### Phase 0 Milestone
**Checkpoint:** Run `python -c "import udr; print(udr.hello())"` and see "Hello from UDR!"

---

## Phase 1: Content-Addressable Chunk Store
**Duration:** Week 2
**Goal:** Core storage primitive working with deduplication

### Rust Implementation

#### Action Items

- [ ] **1.1** Define chunk store types
  ```rust
  // udr_core/src/chunk_store/mod.rs
  pub mod store;
  pub mod error;
  
  // udr_core/src/chunk_store/error.rs
  use thiserror::Error;
  
  #[derive(Error, Debug)]
  pub enum ChunkStoreError {
      #[error("IO error: {0}")]
      Io(#[from] std::io::Error),
      #[error("Chunk not found: {0}")]
      NotFound(String),
  }
  ```

- [ ] **1.2** Implement ChunkStore struct
  ```rust
  // udr_core/src/chunk_store/store.rs
  pub struct ChunkStore {
      base_path: PathBuf,
  }
  
  impl ChunkStore {
      pub fn new(base_path: impl AsRef<Path>) -> Result<Self, ChunkStoreError>;
      pub fn put(&self, data: &[u8]) -> Result<String, ChunkStoreError>;
      pub fn get(&self, hash: &str) -> Result<Vec<u8>, ChunkStoreError>;
      pub fn exists(&self, hash: &str) -> bool;
      pub fn delete(&self, hash: &str) -> Result<(), ChunkStoreError>;
  }
  ```

- [ ] **1.3** Implement hash-based path derivation
  ```rust
  fn hash_to_path(&self, hash: &str) -> PathBuf {
      // hash = "ab12cd34..."
      // path = base/ab/12/ab12cd34...
      self.base_path
          .join(&hash[0..2])
          .join(&hash[2..4])
          .join(hash)
  }
  ```

- [ ] **1.4** Implement put operation
  - Hash data with BLAKE3
  - Derive path from hash
  - Check if exists (dedup)
  - Write atomically (write to temp, rename)
  - Return hash

- [ ] **1.5** Implement get operation
  - Derive path from hash
  - Read file
  - Optionally verify hash matches content

- [ ] **1.6** Write unit tests
  ```rust
  #[cfg(test)]
  mod tests {
      #[test]
      fn test_put_get_roundtrip();
      
      #[test]
      fn test_deduplication();
      
      #[test]
      fn test_not_found();
  }
  ```

- [ ] **1.7** Add Python bindings
  ```rust
  // udr_python/src/lib.rs
  #[pyclass]
  struct PyChunkStore {
      inner: ChunkStore,
  }
  
  #[pymethods]
  impl PyChunkStore {
      #[new]
      fn new(path: &str) -> PyResult<Self>;
      
      fn put(&self, data: &[u8]) -> PyResult<String>;
      fn get(&self, hash: &str) -> PyResult<Vec<u8>>;
      fn exists(&self, hash: &str) -> bool;
  }
  ```

- [ ] **1.8** Write Python integration tests
  ```python
  # tests/test_chunk_store.py
  def test_roundtrip():
      store = udr.ChunkStore("./test_chunks")
      data = b"hello world"
      hash = store.put(data)
      assert store.get(hash) == data
  
  def test_deduplication():
      store = udr.ChunkStore("./test_chunks")
      h1 = store.put(b"same data")
      h2 = store.put(b"same data")
      assert h1 == h2
  ```

- [ ] **1.9** Benchmark put/get performance
  ```rust
  // benches/chunk_store.rs
  use criterion::{criterion_group, criterion_main, Criterion};
  
  fn bench_put(c: &mut Criterion) {
      // Benchmark with various sizes: 1KB, 1MB, 64MB
  }
  ```

### Phase 1 Milestone
**Checkpoint:** 
- `cargo test` passes all chunk store tests
- Python can store and retrieve 100MB of data
- Deduplication verified (store same data twice, only one copy on disk)

---

## Phase 2: Table Catalog & Versioning
**Duration:** Week 3
**Goal:** Tables as versioned collections of chunks

### Data Model

```
Table Version:
  - table_name: String
  - version: u64
  - chunk_hashes: Vec<String>
  - schema_hash: String
  - created_at: Timestamp
  - parent_version: Option<u64>
  - metadata: HashMap<String, String>
```

#### Action Items

- [ ] **2.1** Define table version struct
  ```rust
  // udr_core/src/catalog/version.rs
  #[derive(Debug, Clone, Serialize, Deserialize)]
  pub struct TableVersion {
      pub table_name: String,
      pub version: u64,
      pub chunk_hashes: Vec<String>,
      pub schema_hash: String,
      pub created_at: i64,
      pub parent_version: Option<u64>,
      pub metadata: HashMap<String, String>,
  }
  ```

- [ ] **2.2** Define catalog trait/interface
  ```rust
  // udr_core/src/catalog/mod.rs
  pub trait Catalog {
      fn commit(&self, version: TableVersion) -> Result<u64>;
      fn get_version(&self, table: &str, version: Option<u64>) -> Result<TableVersion>;
      fn list_versions(&self, table: &str) -> Result<Vec<u64>>;
      fn list_tables(&self) -> Result<Vec<String>>;
  }
  ```

- [ ] **2.3** Implement file-based catalog
  ```
  catalog/
  ├── customers/
  │   ├── 1.json
  │   ├── 2.json
  │   └── latest -> 2.json (symlink or file containing "2")
  └── orders/
      ├── 1.json
      └── latest
  ```

- [ ] **2.4** Implement commit operation
  - Validate version number (must be parent + 1 or 1 if first)
  - Write version metadata to JSON file
  - Update latest pointer atomically

- [ ] **2.5** Implement get_version operation
  - If version specified, load directly
  - If None, read latest pointer, then load

- [ ] **2.6** Implement list operations
  - Scan directory for version files
  - Parse and return

- [ ] **2.7** Add concurrency safety
  - File locking for commits
  - Or optimistic concurrency with version check

- [ ] **2.8** Write unit tests
  ```rust
  #[test]
  fn test_commit_and_retrieve();
  
  #[test]
  fn test_version_sequence();
  
  #[test]
  fn test_latest_pointer();
  ```

- [ ] **2.9** Create TableWriter helper
  ```rust
  pub struct TableWriter<'a> {
      store: &'a ChunkStore,
      catalog: &'a dyn Catalog,
      table_name: String,
      chunks: Vec<String>,
  }
  
  impl TableWriter {
      pub fn write_chunk(&mut self, data: &[u8]) -> Result<()>;
      pub fn commit(self) -> Result<u64>;
  }
  ```

- [ ] **2.10** Create TableReader helper
  ```rust
  pub struct TableReader<'a> {
      store: &'a ChunkStore,
      version: TableVersion,
  }
  
  impl TableReader {
      pub fn chunks(&self) -> impl Iterator<Item = Result<Vec<u8>>>;
  }
  ```

- [ ] **2.11** Add Python bindings for catalog

- [ ] **2.12** Integration test: store parquet file as chunks, retrieve and verify

### Phase 2 Milestone
**Checkpoint:**
- Store a parquet file, commit as table version
- Retrieve table at version N
- List all versions of a table
- Verify: modify table, commit v2, can still read v1

---

## Phase 3: Query Layer (DuckDB Integration)
**Duration:** Week 4
**Goal:** SQL queries over versioned tables

#### Action Items

- [ ] **3.1** Set up Python query module
  ```python
  # python/udr/query.py
  import duckdb
  from udr import ChunkStore, Catalog
  ```

- [ ] **3.2** Implement table assembly
  ```python
  def load_table(store, catalog, table_name, version=None):
      """Load table chunks into DuckDB."""
      table_version = catalog.get_version(table_name, version)
      
      frames = []
      for chunk_hash in table_version.chunk_hashes:
          chunk_data = store.get(chunk_hash)
          # Assuming parquet chunks
          frame = pq.read_table(io.BytesIO(chunk_data))
          frames.append(frame)
      
      return pa.concat_tables(frames)
  ```

- [ ] **3.3** Implement query engine wrapper
  ```python
  class QueryEngine:
      def __init__(self, store, catalog):
          self.store = store
          self.catalog = catalog
          self.conn = duckdb.connect(":memory:")
      
      def query(self, sql, tables=None, as_of=None):
          """Execute query with optional time travel."""
          # Register required tables
          # Execute SQL
          # Return results as DataFrame or Arrow
  ```

- [ ] **3.4** Implement time travel syntax
  ```python
  # Option A: Explicit parameter
  engine.query("SELECT * FROM parcels", as_of={"parcels": 5})
  
  # Option B: Parse custom syntax (more complex)
  engine.query("SELECT * FROM parcels AS OF VERSION 5")
  ```

- [ ] **3.5** Add table registration caching
  - Don't reload unchanged tables
  - Track which version is loaded

- [ ] **3.6** Test with Lotitude sample data
  - Export subset of Lotitude data to parquet
  - Load into UDR
  - Run typical Lotitude queries

- [ ] **3.7** Benchmark query performance
  - Compare: direct DuckDB on parquet vs UDR
  - Identify overhead sources

- [ ] **3.8** Add query result formats
  ```python
  def query(self, sql, ..., format="arrow"):
      # format: "arrow", "pandas", "polars", "dict"
  ```

### Phase 3 Milestone
**Checkpoint:**
- Load Lotitude parcel data (sample)
- Run: `SELECT * FROM parcels WHERE score > 80`
- Run same query at two different versions, get different results
- Performance within 2x of direct DuckDB

---

## Phase 4: Branching
**Duration:** Week 5
**Goal:** Git-like branching for data development

### Data Model

```
Branch:
  - name: String
  - head: HashMap<String, u64>  # table_name -> version
  - created_at: Timestamp
  - parent_branch: Option<String>
```

#### Action Items

- [ ] **4.1** Define branch struct
  ```rust
  #[derive(Debug, Clone, Serialize, Deserialize)]
  pub struct Branch {
      pub name: String,
      pub head: HashMap<String, u64>,
      pub created_at: i64,
      pub parent_branch: Option<String>,
  }
  ```

- [ ] **4.2** Implement BranchManager
  ```rust
  pub struct BranchManager {
      catalog_path: PathBuf,
  }
  
  impl BranchManager {
      pub fn create(&self, name: &str, from: Option<&str>) -> Result<Branch>;
      pub fn get(&self, name: &str) -> Result<Branch>;
      pub fn update_head(&self, name: &str, table: &str, version: u64) -> Result<()>;
      pub fn list(&self) -> Result<Vec<String>>;
      pub fn delete(&self, name: &str) -> Result<()>;
  }
  ```

- [ ] **4.3** Implement branch-aware catalog
  ```rust
  pub struct BranchCatalog {
      base_catalog: FileCatalog,
      branch_manager: BranchManager,
      current_branch: String,
  }
  
  impl Catalog for BranchCatalog {
      // get_version uses branch head to resolve "latest"
      // commit updates branch head
  }
  ```

- [ ] **4.4** Implement branch creation (zero-copy)
  - Copy head pointers only
  - No data duplication
  - Verify storage doesn't increase

- [ ] **4.5** Implement diff between branches
  ```rust
  pub fn diff(branch_a: &str, branch_b: &str) -> BranchDiff {
      // For each table:
      // - Same version: no diff
      // - Different version: list changed
      // - Only in A or B: added/removed
  }
  ```

- [ ] **4.6** Implement merge (fast-forward)
  ```rust
  pub fn merge(&self, source: &str, target: &str) -> Result<MergeResult> {
      // If target is ancestor of source: fast-forward
      // If diverged: return conflict
  }
  ```

- [ ] **4.7** Add Python bindings

- [ ] **4.8** Add CLI commands
  ```
  udr branch create feature/new-scoring
  udr branch list
  udr checkout feature/new-scoring
  udr branch diff main..feature/new-scoring
  udr merge feature/new-scoring --into main
  ```

- [ ] **4.9** Test workflow
  - Create branch
  - Modify data on branch
  - Query both branches, see different results
  - Merge back
  - Verify main updated

### Phase 4 Milestone
**Checkpoint:**
- Create branch from main
- Modify parcel scores on branch
- `diff` shows changed parcels
- Merge back to main
- Storage increase ≈ 0 until actual changes made

---

## Phase 5: Cross-Table Transactions
**Duration:** Weeks 6-7 (most complex phase)
**Goal:** ACID transactions across multiple tables

### Design Decisions

**Concurrency Control:** Optimistic (MVCC)
- Readers never block
- Writers check for conflicts at commit
- Simpler than locking for POC

**Isolation Level:** Snapshot Isolation
- Each transaction sees consistent snapshot
- Write-write conflicts detected

#### Action Items

- [ ] **5.1** Define transaction structures
  ```rust
  pub struct Transaction {
      id: Uuid,
      start_time: i64,
      read_set: HashMap<String, u64>,     // table -> version read
      write_buffer: HashMap<String, Vec<u8>>,  // table -> pending data
      status: TransactionStatus,
  }
  
  pub enum TransactionStatus {
      Active,
      Committed,
      Aborted,
  }
  ```

- [ ] **5.2** Implement TransactionManager
  ```rust
  pub struct TransactionManager {
      store: Arc<ChunkStore>,
      catalog: Arc<dyn Catalog>,
      active_transactions: Mutex<HashMap<Uuid, Transaction>>,
      commit_lock: Mutex<()>,  // Serialize commits for POC
  }
  
  impl TransactionManager {
      pub fn begin(&self) -> Result<TransactionHandle>;
      pub fn commit(&self, tx: TransactionHandle) -> Result<()>;
      pub fn abort(&self, tx: TransactionHandle) -> Result<()>;
  }
  ```

- [ ] **5.3** Implement TransactionHandle (user-facing)
  ```rust
  pub struct TransactionHandle<'a> {
      manager: &'a TransactionManager,
      tx_id: Uuid,
  }
  
  impl TransactionHandle {
      pub fn read_table(&mut self, name: &str) -> Result<TableReader>;
      pub fn write_table(&mut self, name: &str, data: &[u8]) -> Result<()>;
  }
  ```

- [ ] **5.4** Implement snapshot reads
  - Record version read in read_set
  - Always read from snapshot at transaction start

- [ ] **5.5** Implement buffered writes
  - Don't write to storage immediately
  - Buffer in transaction
  - Apply all at commit

- [ ] **5.6** Implement conflict detection
  ```rust
  fn check_conflicts(&self, tx: &Transaction) -> Result<(), ConflictError> {
      for (table, read_version) in &tx.read_set {
          let current_version = self.catalog.get_latest_version(table)?;
          if current_version > read_version {
              // Someone committed after we read
              return Err(ConflictError::WriteConflict(table.clone()));
          }
      }
      Ok(())
  }
  ```

- [ ] **5.7** Implement atomic commit
  ```rust
  fn commit_internal(&self, tx: Transaction) -> Result<()> {
      let _lock = self.commit_lock.lock();  // Serialize commits
      
      // Check conflicts
      self.check_conflicts(&tx)?;
      
      // Write all chunks
      let mut new_versions = Vec::new();
      for (table, data) in tx.write_buffer {
          let hash = self.store.put(&data)?;
          let version = self.catalog.commit(/* ... */)?;
          new_versions.push((table, version));
      }
      
      // Update transaction log (for recovery)
      self.log_commit(tx.id, &new_versions)?;
      
      Ok(())
  }
  ```

- [ ] **5.8** Add write-ahead logging (WAL)
  - Log transaction intent before commit
  - Enable recovery after crash

- [ ] **5.9** Implement rollback
  - Simply discard write buffer
  - No changes to storage

- [ ] **5.10** Add Python bindings with context manager
  ```python
  with udr.transaction(store, catalog) as tx:
      parcels = tx.read_table("parcels")
      scores = tx.read_table("scores")
      
      # Modify
      tx.write_table("parcels", new_parcels)
      tx.write_table("scores", new_scores)
      
      # Commits atomically on exit
  # Or rolls back on exception
  ```

- [ ] **5.11** Test atomicity
  ```python
  def test_atomic_commit():
      with udr.transaction() as tx:
          tx.write_table("a", data_a)
          tx.write_table("b", data_b)
      
      # Both committed
      assert catalog.get_version("a").version == expected
      assert catalog.get_version("b").version == expected
  
  def test_rollback_on_error():
      try:
          with udr.transaction() as tx:
              tx.write_table("a", data_a)
              raise Exception("simulated failure")
      except:
          pass
      
      # Neither committed
      assert catalog.get_version("a").version == original
  ```

- [ ] **5.12** Test conflict detection
  ```python
  def test_write_conflict():
      tx1 = manager.begin()
      tx2 = manager.begin()
      
      tx1.read_table("parcels")
      tx2.read_table("parcels")
      
      tx1.write_table("parcels", data1)
      tx1.commit()  # Succeeds
      
      tx2.write_table("parcels", data2)
      with pytest.raises(ConflictError):
          tx2.commit()  # Fails - parcels changed
  ```

### Phase 5 Milestone
**Checkpoint:**
- Atomic update of 3 Lotitude tables
- Rollback works on simulated failure
- Conflict detection prevents lost updates
- Basic WAL enables crash recovery

---

## Phase 6: Changelog & Subscriptions
**Duration:** Week 8
**Goal:** Unified batch/stream via changelog

### Design

Every commit creates a changelog entry:
```
ChangelogEntry:
  - commit_id: u64
  - timestamp: i64
  - tables: Vec<TableChange>
  - transaction_id: Option<Uuid>

TableChange:
  - table_name: String
  - old_version: Option<u64>
  - new_version: u64
  - change_type: Insert | Update | Delete
```

#### Action Items

- [ ] **6.1** Define changelog structures
  ```rust
  #[derive(Debug, Clone, Serialize, Deserialize)]
  pub struct ChangelogEntry {
      pub commit_id: u64,
      pub timestamp: i64,
      pub changes: Vec<TableChange>,
  }
  
  #[derive(Debug, Clone, Serialize, Deserialize)]
  pub struct TableChange {
      pub table_name: String,
      pub old_version: Option<u64>,
      pub new_version: u64,
  }
  ```

- [ ] **6.2** Implement changelog writer
  - Append-only log file
  - Written atomically on each commit

- [ ] **6.3** Implement changelog reader
  ```rust
  pub fn read_from(&self, commit_id: u64) -> impl Iterator<Item = ChangelogEntry>;
  ```

- [ ] **6.4** Implement subscription API
  ```rust
  pub struct Subscriber {
      changelog: Arc<Changelog>,
      position: u64,
  }
  
  impl Subscriber {
      pub fn poll(&mut self) -> Vec<ChangelogEntry>;
      pub fn subscribe(&mut self, callback: impl Fn(ChangelogEntry));
  }
  ```

- [ ] **6.5** Add Python bindings
  ```python
  def on_change(entry):
      for change in entry.changes:
          if change.table_name == "parcels":
              print(f"Parcels updated: v{change.old_version} -> v{change.new_version}")
  
  subscriber = udr.Subscriber(changelog, from_commit=100)
  subscriber.subscribe(on_change)
  ```

- [ ] **6.6** Implement diff between versions
  ```python
  def diff_versions(store, catalog, table, v1, v2):
      """Return rows added, removed, modified between versions."""
      old = load_table(store, catalog, table, version=v1)
      new = load_table(store, catalog, table, version=v2)
      # Compare and return diff
  ```

- [ ] **6.7** Test: subscribe to parcel changes
  - Set up subscriber
  - Commit new parcel data
  - Verify callback fires
  - Verify diff shows correct changes

### Phase 6 Milestone
**Checkpoint:**
- Changelog records all commits
- Subscriber receives notifications
- Can compute diff between any two versions
- Lotitude could use this for "what changed" alerts

---

## Phase 7: Lotitude Migration
**Duration:** Weeks 9-10
**Goal:** Lotitude running on UDR

#### Action Items

- [ ] **7.1** Audit current Lotitude data storage
  - Document current schema
  - Document current data flow
  - Identify all data sources

- [ ] **7.2** Design UDR table schema for Lotitude
  ```
  Tables:
    - parcels (core parcel data)
    - zoning (zoning information)
    - ownership (owner data)
    - transactions (sale history)
    - scores (computed scores)
    - signals (individual scoring signals)
  ```

- [ ] **7.3** Write migration script
  ```python
  def migrate_to_udr():
      # Export current data
      # Chunk and store in UDR
      # Verify integrity
  ```

- [ ] **7.4** Adapt Lotitude queries to UDR
  - Replace direct database queries with UDR query engine
  - Add time travel where beneficial
  - Add branching for score algorithm development

- [ ] **7.5** Implement score recalculation with transactions
  ```python
  with udr.transaction() as tx:
      parcels = tx.read_table("parcels")
      signals = tx.read_table("signals")
      
      new_scores = compute_scores(parcels, signals)
      
      tx.write_table("scores", new_scores)
  ```

- [ ] **7.6** Add "what changed" feature
  ```python
  def get_score_changes(since_version):
      current = catalog.get_version("scores")
      return diff_versions(store, catalog, "scores", since_version, current.version)
  ```

- [ ] **7.7** Update PDF report generation
  - Reports now reference specific data version
  - Reproducible: same version = same report

- [ ] **7.8** Performance testing
  - Full refresh timing
  - Query latency
  - Memory usage

- [ ] **7.9** Parallel operation period
  - Run both old and new systems
  - Compare results
  - Build confidence

- [ ] **7.10** Cutover
  - Switch Lotitude to UDR
  - Monitor for issues
  - Keep rollback path open

### Phase 7 Milestone
**Checkpoint:**
- Lotitude fully operational on UDR
- All existing functionality preserved
- New capabilities working (time travel, branching)
- Performance acceptable

---

## Phase 8: Documentation & Release
**Duration:** Week 11+
**Goal:** Publishable, usable by others

#### Action Items

- [ ] **8.1** Write README.md
  - Project overview
  - Installation instructions
  - Quick start example
  - Architecture diagram

- [ ] **8.2** Write API documentation
  - Rust docs (cargo doc)
  - Python docstrings
  - Examples for each major operation

- [ ] **8.3** Create example notebooks
  - Basic operations tutorial
  - Time travel demo
  - Branching workflow
  - Transaction example

- [ ] **8.4** Write architecture document
  - Design decisions
  - Trade-offs made
  - Future directions

- [ ] **8.5** Create demo video or GIF
  - Show key capabilities
  - Shareable on social media

- [ ] **8.6** Write blog post
  - Why this exists
  - What it enables
  - How to get started

- [ ] **8.7** Set up GitHub repository
  - Public or private (your choice)
  - CI/CD with GitHub Actions
  - Issue templates
  - Contributing guidelines

- [ ] **8.8** Publish to crates.io / PyPI (optional)
  - If ready for public use

---

## Key Checkpoints Summary

| Week | Phase | Must Have | Nice to Have |
|------|-------|-----------|--------------|
| 1 | Environment | Rust builds, PyO3 works | Rustlings complete |
| 2 | Chunk Store | Put/get/dedup working | Benchmarks |
| 3 | Catalog | Versioned tables | Schema validation |
| 4 | Query | DuckDB + time travel | Query caching |
| 5 | Branching | Create/merge works | Conflict resolution |
| 6-7 | Transactions | Atomic multi-table | Full MVCC |
| 8 | Changelog | Subscriptions work | Real-time notifications |
| 9-10 | Migration | Lotitude on UDR | Performance parity |
| 11+ | Release | Documentation | Public release |

---

## Risk Mitigation

### Risk: Rust learning curve
**Mitigation:** 
- First week dedicated to learning
- Start with simpler components
- Python fallback for complex logic initially

### Risk: Transaction complexity
**Mitigation:**
- Start with single-process locking (simple but correct)
- Upgrade to full MVCC only if needed
- Phase 5 has extra week buffer

### Risk: Performance issues
**Mitigation:**
- Benchmark early and often
- Profile before optimizing
- DuckDB does heavy lifting for queries

### Risk: Scope creep
**Mitigation:**
- Each phase has clear milestone
- "Nice to have" explicitly separated
- Lotitude migration is the forcing function

---

## Resources

### Rust Learning
- [The Rust Book](https://doc.rust-lang.org/book/)
- [Rust by Example](https://doc.rust-lang.org/rust-by-example/)
- [Rustlings](https://github.com/rust-lang/rustlings)
- [Too Many Linked Lists](https://rust-unofficial.github.io/too-many-lists/) (intermediate)

### PyO3
- [PyO3 User Guide](https://pyo3.rs/)
- [Maturin Docs](https://www.maturin.rs/)

### Relevant Crates
- [blake3](https://docs.rs/blake3)
- [serde](https://serde.rs/)
- [parking_lot](https://docs.rs/parking_lot)
- [thiserror](https://docs.rs/thiserror)

### Prior Art (for reference)
- [delta-rs](https://github.com/delta-io/delta-rs) - Delta Lake in Rust
- [lance](https://github.com/lancedb/lance) - Modern columnar format
- [oxigraph](https://github.com/oxigraph/oxigraph) - Database in Rust

---

## Notes & Decisions Log

Use this section to record key decisions as you build:

```
Date: 
Decision:
Rationale:
Alternatives considered:
```

---

*Last updated: January 2026*
