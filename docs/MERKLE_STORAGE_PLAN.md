# Rhizo v2: Mathematical Storage Architecture

## Vision

Transform Rhizo from a working prototype into a **mathematically optimal** data system where every claim is provable and every operation approaches theoretical limits.

---

## Implementation Status

| Phase | Status | Verified Claim |
|-------|--------|----------------|
| **A: Merkle Storage** | **COMPLETE** | 5% change = 95% chunk reuse (measured) |
| B: CRDT Coordination | Planned | Coordination-free distributed |
| C: Interval Tracking | Planned | Row-level conflict detection |

### Phase A Benchmark Results (January 2026)

```
| Change % | Chunk Reuse | Storage Savings (2 versions) |
|----------|-------------|------------------------------|
| 1%       | 98.8%       | 49.4%                        |
| 5%       | 95.0%       | 47.5%                        |
| 10%      | 90.0%       | 45.0%                        |
| 25%      | 75.0%       | 37.5%                        |
```

**Implementation:**
- Rust: `rhizo_core/src/merkle/` (error.rs, types.rs, tree.rs, mod.rs)
- Python bindings: `PyMerkleTree`, `PyMerkleDiff`, `PyMerkleConfig`
- 15 Rust tests, all passing
- Benchmark: `examples/merkle_benchmark.py`

---

## Three Phases

| Phase | Name | What It Solves | Key Innovation |
|-------|------|----------------|----------------|
| **A** | Merkle Storage | Deduplication broken for incremental changes | O(log n) per change |
| **B** | CRDT Coordination | Single-node limitation | Coordination-free distributed |
| **C** | Interval Tracking | Table-level conflicts too coarse | Row-level conflict detection |

## Efficiency Comparison

| Metric | Current | Phase A | Phase B | Phase C |
|--------|---------|---------|---------|---------|
| Storage (10 versions, 5% change) | 10x | 1.5x | 1.5x | 1.5x |
| Deduplication | 0% incremental | 85-95% | 85-95% | 85-95% |
| Distributed | No | No | Yes | Yes |
| Conflict granularity | Table | Chunk | Chunk | Row |
| Coordination needed | Single node | Single node | None | None |

---

# PHASE A: Merkle Tree Storage

## Goal

Make the deduplication claims in the paper **true and provable**:
- 60-85% storage reduction for incremental changes
- O(log n) storage overhead per version
- Mathematical proof of correctness

---

## Current State vs Target State

### Current (Whole-File Hashing)

```
Table (1M rows)
      ↓
Serialize to Parquet (50 MB)
      ↓
BLAKE3(entire file) → hash_v1
      ↓
Store at chunks/ha/sh/hash_v1

Change 1 row:
      ↓
Serialize to Parquet (50 MB) ← ENTIRE TABLE AGAIN
      ↓
BLAKE3(entire file) → hash_v2 ← COMPLETELY DIFFERENT
      ↓
Store at chunks/ha/sh/hash_v2 ← NO DEDUPLICATION
```

**Storage for 10 versions with 5% changes: 10 × 50MB = 500MB**

### Target (Merkle Tree)

```
Table (1M rows)
      ↓
Split into chunks of ~1000 rows each (1000 chunks)
      ↓
Build Merkle tree:
                        Root
                       /    \
                   L1_0      L1_1
                  /    \    /    \
                ...    ...  ...   ...
               /  \
           Chunk_0  Chunk_1 ... Chunk_999
              ↓        ↓           ↓
           hash_0   hash_1  ... hash_999

Change 1 row (in Chunk_42):
      ↓
Only recompute:
  - Chunk_42 content → new hash
  - Path to root (~10 internal nodes for 1000 chunks)
      ↓
999 chunks REUSED (99.9% deduplication)
```

**Storage for 10 versions with 5% changes:**
- Version 1: 1000 chunks + tree overhead ≈ 51MB
- Versions 2-10: Only changed chunks (~50 per version) + tree paths
- Total: 51MB + 9 × (50 × 50KB + tree) ≈ 75MB

**Reduction: 500MB → 75MB = 85% savings**

---

## Mathematical Foundation

### Theorem 1: Storage Efficiency

For a table with n rows, chunk size c, and change rate r per version:

**Chunks per version:**
$$\text{chunks}_\text{changed} = \lceil n \times r / c \rceil$$

**Tree nodes changed (binary tree):**
$$\text{nodes}_\text{changed} = \text{chunks}_\text{changed} \times \log_2(n/c)$$

**Storage per new version:**
$$S_\text{new} = \text{chunks}_\text{changed} \times \text{chunk\_size} + \text{nodes}_\text{changed} \times 32\text{ bytes}$$

**Example:** n=1M rows, c=1000 rows/chunk, r=5%
- chunks_changed = 1M × 0.05 / 1000 = 50 chunks
- Tree depth = log₂(1000) ≈ 10
- nodes_changed = 50 × 10 = 500 nodes
- S_new = 50 × 50KB + 500 × 32B = 2.5MB + 16KB ≈ 2.5MB

**Vs naive:** 50MB per version
**Savings:** 1 - (2.5/50) = **95%**

### Theorem 2: Deduplication Guarantee

If chunk_hash(A) == chunk_hash(B), then content(A) == content(B) with probability:

$$P(\text{collision}) < \frac{n^2}{2^{257}} < 10^{-47}$$

Therefore: **Same hash = Same content (for all practical purposes)**

### Theorem 3: Integrity Verification

For any table version V with root hash H:

1. Recompute all leaf hashes from chunks
2. Recompute internal nodes bottom-up
3. Compare computed root with stored H

If match: **100% of data is verified uncorrupted**

This is O(n) verification but provides complete integrity guarantee.

---

## Data Structures

### Rust Types

```rust
/// A content-addressed chunk of row data
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DataChunk {
    /// BLAKE3 hash of the serialized rows
    pub hash: String,
    /// Row indices covered by this chunk [start, end)
    pub row_range: (u64, u64),
    /// Number of rows in this chunk
    pub row_count: u64,
}

/// Internal node in the Merkle tree
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct MerkleNode {
    /// BLAKE3 hash of concatenated child hashes
    pub hash: String,
    /// Child node hashes (2 for binary tree)
    pub children: Vec<String>,
    /// Level in tree (0 = root)
    pub level: u32,
}

/// Complete Merkle tree for a table version
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct MerkleTable {
    /// Root hash (identity of this table version)
    pub root_hash: String,
    /// Schema information
    pub schema: TableSchema,
    /// Leaf chunks (data)
    pub chunks: Vec<DataChunk>,
    /// Internal nodes (by level, then index)
    pub nodes: Vec<Vec<MerkleNode>>,
    /// Total row count
    pub row_count: u64,
    /// Chunk size used
    pub chunk_size: u64,
}

/// Reference to a table version (lightweight)
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TableRef {
    pub table_name: String,
    pub version: u64,
    pub root_hash: String,
}
```

### Storage Layout

```
{store_path}/
├── chunks/                    # Content-addressed data chunks
│   ├── ab/
│   │   └── cd/
│   │       └── abcd1234...   # Parquet file with ~1000 rows
│   └── ...
├── nodes/                     # Merkle tree internal nodes
│   ├── ef/
│   │   └── 12/
│   │       └── ef123456...   # JSON: {hash, children, level}
│   └── ...
└── tables/                    # Table version metadata
    └── users/
        ├── v1.json           # MerkleTable metadata
        ├── v2.json
        └── ...
```

---

## Implementation Plan

### Phase 1: Core Merkle Operations (Rust)

**File:** `rhizo_core/src/merkle/mod.rs`

```rust
pub mod chunk;      // Chunk creation and hashing
pub mod tree;       // Merkle tree construction
pub mod diff;       // Tree diffing for incremental updates
pub mod verify;     // Integrity verification
pub mod store;      // Storage integration
```

**Key Functions:**

```rust
// Create chunks from a DataFrame
pub fn chunk_dataframe(
    df: &DataFrame,
    chunk_size: usize,
) -> Result<Vec<DataChunk>, MerkleError>;

// Build Merkle tree from chunks
pub fn build_tree(
    chunks: &[DataChunk],
) -> Result<MerkleTable, MerkleError>;

// Compute incremental update (only changed chunks)
pub fn diff_and_update(
    old_tree: &MerkleTable,
    new_df: &DataFrame,
    chunk_store: &ChunkStore,
) -> Result<MerkleTable, MerkleError>;

// Verify integrity of entire table
pub fn verify_integrity(
    table: &MerkleTable,
    chunk_store: &ChunkStore,
) -> Result<bool, MerkleError>;

// Reconstruct DataFrame from Merkle table
pub fn reconstruct(
    table: &MerkleTable,
    chunk_store: &ChunkStore,
) -> Result<DataFrame, MerkleError>;
```

### Phase 2: Chunk Diffing Algorithm

The key to efficiency is detecting which chunks changed:

```rust
pub fn diff_chunks(
    old_df: &DataFrame,
    new_df: &DataFrame,
    chunk_size: usize,
) -> ChunkDiff {
    let old_chunks = chunk_dataframe(old_df, chunk_size);
    let new_chunks = chunk_dataframe(new_df, chunk_size);

    let mut unchanged = Vec::new();
    let mut changed = Vec::new();

    for (i, (old, new)) in old_chunks.iter().zip(new_chunks.iter()).enumerate() {
        if old.hash == new.hash {
            unchanged.push(i);
        } else {
            changed.push((i, new.clone()));
        }
    }

    ChunkDiff { unchanged, changed }
}
```

**Optimization:** Use row-level hashing to detect changes before re-serializing:

```rust
// Quick check: hash each row, compare
fn rows_changed(old_df: &DataFrame, new_df: &DataFrame) -> Vec<usize> {
    old_df.iter_rows()
        .zip(new_df.iter_rows())
        .enumerate()
        .filter(|(_, (old, new))| row_hash(old) != row_hash(new))
        .map(|(i, _)| i)
        .collect()
}
```

### Phase 3: Python Bindings

**File:** `rhizo_python/src/merkle.rs`

```rust
#[pyclass]
pub struct PyMerkleTable {
    inner: MerkleTable,
}

#[pymethods]
impl PyMerkleTable {
    #[getter]
    fn root_hash(&self) -> String { ... }

    #[getter]
    fn chunk_count(&self) -> usize { ... }

    #[getter]
    fn row_count(&self) -> u64 { ... }

    fn verify(&self, store: &PyChunkStore) -> PyResult<bool> { ... }
}

#[pyclass]
pub struct PyMerkleStore {
    inner: MerkleStore,
}

#[pymethods]
impl PyMerkleStore {
    #[new]
    fn new(path: &str, chunk_size: Option<usize>) -> PyResult<Self> { ... }

    fn write_table(&self, name: &str, df: PyArrowArray) -> PyResult<PyMerkleTable> { ... }

    fn read_table(&self, name: &str, version: Option<u64>) -> PyResult<PyArrowArray> { ... }

    fn diff_versions(&self, name: &str, v1: u64, v2: u64) -> PyResult<PyChunkDiff> { ... }
}
```

### Phase 4: Integration with Existing System

**Modify `QueryEngine` to use Merkle storage:**

```python
class QueryEngine:
    def __init__(
        self,
        store: PyChunkStore,
        catalog: PyCatalog,
        merkle_store: Optional[PyMerkleStore] = None,  # NEW
        ...
    ):
        self._merkle = merkle_store

    def write_table(self, name: str, df: pd.DataFrame, ...):
        if self._merkle:
            # Use Merkle storage for deduplication
            merkle_table = self._merkle.write_table(name, df)
            # Store reference in catalog
            self._catalog.commit(name, merkle_table.root_hash, ...)
        else:
            # Fall back to whole-file storage
            ...
```

---

## Verification Tests

### Test 1: Deduplication Ratio

```python
def test_merkle_deduplication():
    """Prove 85%+ deduplication for 5% changes."""
    store = MerkleStore("./test_merkle", chunk_size=1000)

    # Create 100K row table
    df = pd.DataFrame({
        "id": range(100_000),
        "value": np.random.random(100_000)
    })

    # Write version 1
    v1 = store.write_table("data", df)
    storage_v1 = store.total_bytes()

    # Modify 5% of rows
    changed_rows = np.random.choice(100_000, 5000, replace=False)
    df.loc[changed_rows, "value"] = np.random.random(5000)

    # Write version 2
    v2 = store.write_table("data", df)
    storage_v2 = store.total_bytes()

    # Calculate deduplication
    new_bytes = storage_v2 - storage_v1
    naive_bytes = storage_v1  # Would be 2x without dedup
    dedup_ratio = 1 - (new_bytes / naive_bytes)

    print(f"Deduplication ratio: {dedup_ratio:.1%}")
    assert dedup_ratio > 0.85, f"Expected >85%, got {dedup_ratio:.1%}"
```

### Test 2: Mathematical Proof

```python
def test_dedup_matches_theory():
    """Verify empirical results match theoretical formula."""
    n = 100_000  # rows
    c = 1000     # chunk size
    r = 0.05     # change rate

    # Theoretical prediction
    chunks_changed = math.ceil(n * r / c)  # 50
    theoretical_new_data_ratio = chunks_changed / (n / c)  # 50/100 = 0.5
    # But changes may cluster, so actual is often better

    # Empirical measurement
    empirical_ratio = measure_actual_dedup(n, c, r)

    # Should be within 20% of theory
    assert abs(empirical_ratio - theoretical_new_data_ratio) < 0.2
```

### Test 3: Integrity Verification

```python
def test_merkle_integrity():
    """Prove corruption is always detected."""
    store = MerkleStore("./test_merkle")

    # Write data
    df = pd.DataFrame({"x": range(10000)})
    table = store.write_table("test", df)

    # Verify passes
    assert table.verify(store) == True

    # Corrupt a chunk (simulate bit flip)
    chunk_path = store.chunk_path(table.chunks[0].hash)
    with open(chunk_path, "r+b") as f:
        f.seek(100)
        f.write(b"CORRUPTED")

    # Verify now fails
    assert table.verify(store) == False
```

### Test 4: Branching with Dedup

```python
def test_branch_deduplication():
    """Prove branches share chunks."""
    store = MerkleStore("./test_merkle")

    # Create main branch with data
    df = large_dataframe(1_000_000)
    store.write_table("data", df, branch="main")
    storage_after_main = store.total_bytes()

    # Create feature branch (should be ~0 new bytes)
    store.create_branch("feature", from_branch="main")
    storage_after_branch = store.total_bytes()

    branch_overhead = storage_after_branch - storage_after_main
    assert branch_overhead < 1000, "Branch should add <1KB"

    # Modify 1% on feature branch
    df.loc[:10000, "value"] = 999
    store.write_table("data", df, branch="feature")
    storage_after_modify = store.total_bytes()

    # Should only add ~1% new storage
    new_data = storage_after_modify - storage_after_branch
    assert new_data < storage_after_main * 0.02
```

---

## Benchmark Suite Update

```python
def benchmark_merkle_deduplication():
    """Comprehensive deduplication benchmarks for paper."""
    results = []

    for change_rate in [0.01, 0.05, 0.10, 0.25]:
        for num_versions in [5, 10, 20, 50]:
            # Run test
            dedup_ratio = measure_dedup(
                rows=100_000,
                chunk_size=1000,
                change_rate=change_rate,
                versions=num_versions
            )

            # Compare to theory
            theoretical = calculate_theoretical_dedup(
                rows=100_000,
                chunk_size=1000,
                change_rate=change_rate,
                versions=num_versions
            )

            results.append({
                "change_rate": change_rate,
                "versions": num_versions,
                "empirical_dedup": dedup_ratio,
                "theoretical_dedup": theoretical,
                "matches_theory": abs(dedup_ratio - theoretical) < 0.1
            })

    return results
```

---

## Timeline

| Phase | Task | Complexity |
|-------|------|------------|
| 1 | Core Merkle types and tree building | Medium |
| 2 | Chunk diffing algorithm | Medium |
| 3 | Storage integration | Medium |
| 4 | Python bindings | Low |
| 5 | Verification tests | Low |
| 6 | Benchmark suite update | Low |
| 7 | Paper update with proven claims | Low |

---

## Success Criteria

The implementation is complete when:

1. `test_merkle_deduplication` passes with >85% dedup for 5% changes
2. `test_dedup_matches_theory` proves empirical matches mathematical model
3. `test_merkle_integrity` proves 100% corruption detection
4. `test_branch_deduplication` proves branches share storage
5. Benchmarks show consistent results across multiple runs
6. Paper claims are updated with measured (not theoretical) numbers

---

## Risk Mitigation

| Risk | Mitigation |
|------|------------|
| Performance overhead from tree operations | Benchmark and optimize; tree ops are O(log n) |
| Chunk boundary misalignment | Use content-defined chunking (FastCDC) as enhancement |
| Increased complexity | Keep whole-file mode as fallback for simple cases |
| Parquet internal structure changes | Hash serialized bytes, not logical content |

---

## Phase A Next Steps

1. Review and approve this plan
2. Create `rhizo_core/src/merkle/` module structure
3. Implement `chunk_dataframe()` and `build_tree()`
4. Write initial tests
5. Iterate until tests pass
6. Add Python bindings
7. Update benchmarks and paper

---
---

# PHASE B: CRDT Coordination (Distributed Without Consensus)

## Goal

Eliminate the single-node limitation **without requiring coordination services**:
- No ZooKeeper, etcd, or external databases
- Works on S3, GCS, Azure Blob (eventually consistent stores)
- Mathematically guaranteed convergence

---

## The Problem

Current atomic commit relies on filesystem rename:

```rust
fs::rename(temp_path, catalog_path)  // Atomic on local FS only
```

On S3:
- No atomic rename
- No atomic multi-object update
- Concurrent writes can conflict unpredictably

Traditional solutions:
- **Pessimistic locking:** Requires coordination service (complexity, latency, SPOF)
- **Optimistic locking:** Requires conditional writes (not all stores support)

---

## The Solution: CRDTs

**Conflict-free Replicated Data Types** use mathematical properties to guarantee convergence without coordination.

### Key Insight

Instead of trying to prevent conflicts, design data structures where **conflicts are impossible** or **automatically resolved**.

### Mathematical Foundation

A CRDT must satisfy:

1. **Associativity:** `merge(merge(a, b), c) == merge(a, merge(b, c))`
2. **Commutativity:** `merge(a, b) == merge(b, a)`
3. **Idempotency:** `merge(a, a) == a`

If these hold, **any order of merges produces the same result**.

---

## CRDT Types for Rhizo

### 1. LWW-Register (Last-Writer-Wins)

For individual values where "latest wins" is acceptable:

```rust
#[derive(Clone, Serialize, Deserialize)]
pub struct LWWRegister<T> {
    pub value: T,
    pub timestamp: u64,      // Lamport timestamp
    pub writer_id: String,   // Tie-breaker
}

impl<T: Clone> LWWRegister<T> {
    pub fn merge(&self, other: &Self) -> Self {
        if (self.timestamp, &self.writer_id) > (other.timestamp, &other.writer_id) {
            self.clone()
        } else {
            other.clone()
        }
    }
}
```

**Guarantee:** Given any set of writes, all nodes converge to the same value.

### 2. G-Counter (Grow-Only Counter)

For counting operations (versions, row counts):

```rust
#[derive(Clone, Serialize, Deserialize)]
pub struct GCounter {
    counts: HashMap<String, u64>,  // node_id -> count
}

impl GCounter {
    pub fn increment(&mut self, node_id: &str) {
        *self.counts.entry(node_id.to_string()).or_default() += 1;
    }

    pub fn value(&self) -> u64 {
        self.counts.values().sum()
    }

    pub fn merge(&self, other: &Self) -> Self {
        let mut result = self.counts.clone();
        for (k, v) in &other.counts {
            let entry = result.entry(k.clone()).or_default();
            *entry = (*entry).max(*v);
        }
        GCounter { counts: result }
    }
}
```

**Guarantee:** Counter only grows, never loses increments.

### 3. OR-Set (Observed-Remove Set)

For sets of chunks/hashes where items can be added and removed:

```rust
#[derive(Clone, Serialize, Deserialize)]
pub struct ORSet<T: Hash + Eq + Clone> {
    // Each element tagged with unique add-id
    elements: HashMap<T, HashSet<Uuid>>,
    tombstones: HashMap<T, HashSet<Uuid>>,
}

impl<T: Hash + Eq + Clone> ORSet<T> {
    pub fn add(&mut self, value: T) {
        let tag = Uuid::new_v4();
        self.elements.entry(value).or_default().insert(tag);
    }

    pub fn remove(&mut self, value: &T) {
        if let Some(tags) = self.elements.get(value) {
            self.tombstones.entry(value.clone()).or_default()
                .extend(tags.iter().cloned());
        }
    }

    pub fn contains(&self, value: &T) -> bool {
        match (self.elements.get(value), self.tombstones.get(value)) {
            (Some(added), Some(removed)) => !added.is_subset(removed),
            (Some(_), None) => true,
            _ => false,
        }
    }

    pub fn merge(&self, other: &Self) -> Self {
        // Union of all adds, union of all removes
        // An element exists if any add is not tombstoned
        ...
    }
}
```

**Guarantee:** Add and remove commute; concurrent add+remove = element exists.

---

## CRDT Table Version

```rust
#[derive(Clone, Serialize, Deserialize)]
pub struct CRDTTableVersion {
    pub table_name: String,

    // Merkle root is LWW - latest write wins
    pub root_hash: LWWRegister<String>,

    // Version counter grows monotonically
    pub version: GCounter,

    // Set of chunk hashes (can add new, garbage collect old)
    pub chunks: ORSet<String>,

    // Metadata
    pub schema: LWWRegister<TableSchema>,
    pub row_count: LWWRegister<u64>,
}

impl CRDTTableVersion {
    pub fn merge(&self, other: &Self) -> Self {
        CRDTTableVersion {
            table_name: self.table_name.clone(),
            root_hash: self.root_hash.merge(&other.root_hash),
            version: self.version.merge(&other.version),
            chunks: self.chunks.merge(&other.chunks),
            schema: self.schema.merge(&other.schema),
            row_count: self.row_count.merge(&other.row_count),
        }
    }
}
```

---

## Distributed Protocol

### Write Path (No Coordination)

```
Node A wants to write table "users":

1. Read current state from S3 (may be stale, that's OK)
2. Create new Merkle tree with changes
3. Write new chunks to S3 (content-addressed, idempotent)
4. Create new CRDTTableVersion with updated fields
5. Write version file to S3: s3://bucket/tables/users/{uuid}.json
6. Done (no locks, no coordination)
```

### Read Path (Crdt Merge)

```
Node B wants to read table "users":

1. List all version files: s3://bucket/tables/users/*.json
2. Load and merge all versions: final = v1.merge(v2).merge(v3)...
3. Use final.root_hash to read Merkle tree
4. Done (always consistent due to CRDT properties)
```

### Consistency Guarantee

**Theorem:** If all writers use CRDTs correctly, all readers see a consistent state that includes all acknowledged writes.

**Proof sketch:**
1. CRDTs are associative/commutative/idempotent
2. Order of merge doesn't matter
3. All writes eventually reach all readers (eventual consistency of S3)
4. All readers merge the same set of writes
5. Same set + order-independent merge = same result

---

## Cross-Table Transactions with CRDTs

The challenge: atomic multi-table commits without coordination.

### Solution: Transaction as CRDT

```rust
#[derive(Clone, Serialize, Deserialize)]
pub struct CRDTTransaction {
    pub tx_id: Uuid,
    pub timestamp: u64,
    pub writer_id: String,

    // Tables modified in this transaction
    pub table_versions: HashMap<String, CRDTTableVersion>,

    // Transaction status (prepared -> committed | aborted)
    pub status: LWWRegister<TxStatus>,
}
```

### Two-Phase Protocol (Coordination-Free)

```
Phase 1: Prepare
  - Write all chunks to content-addressed storage
  - Write transaction record with status=Prepared
  - Write is durable once acknowledged by storage

Phase 2: Commit
  - Update transaction status to Committed
  - Readers only see committed transactions

Conflict Resolution:
  - If two transactions modify same table concurrently:
  - LWW: Later timestamp wins
  - Reader merges both, sees latest state
```

### Consistency Level

This provides **eventual consistency** with **causal ordering** (via Lamport timestamps).

For **strong consistency**, would need:
- Conditional writes (S3 `If-None-Match`)
- Or external coordination
- Or accept that concurrent writes to same table → last-write-wins

---

## Verification Tests (Phase B)

```python
def test_crdt_convergence():
    """Prove all nodes converge to same state."""
    # Simulate 3 nodes writing concurrently
    node_a = CRDTTableVersion.new("users")
    node_b = CRDTTableVersion.new("users")
    node_c = CRDTTableVersion.new("users")

    # Each node makes different updates
    node_a.update(root_hash="aaa", timestamp=1)
    node_b.update(root_hash="bbb", timestamp=2)
    node_c.update(root_hash="ccc", timestamp=3)

    # Merge in different orders
    result_1 = node_a.merge(node_b).merge(node_c)
    result_2 = node_c.merge(node_a).merge(node_b)
    result_3 = node_b.merge(node_c).merge(node_a)

    # All must be equal
    assert result_1 == result_2 == result_3
    assert result_1.root_hash.value == "ccc"  # Latest timestamp wins

def test_crdt_distributed_write():
    """Prove writes work without coordination."""
    s3 = MockS3()

    # Two nodes write simultaneously
    node_a = CRDTStore(s3, node_id="a")
    node_b = CRDTStore(s3, node_id="b")

    # Concurrent writes
    node_a.write_table("users", df_a)
    node_b.write_table("users", df_b)

    # Both reads see consistent state
    result_a = node_a.read_table("users")
    result_b = node_b.read_table("users")

    assert result_a.root_hash == result_b.root_hash
```

---

## Phase B Success Criteria

1. CRDT merge is associative, commutative, idempotent (property tests)
2. Concurrent writes from multiple nodes converge
3. Works on S3/GCS without coordination service
4. Performance: merge overhead < 10% vs single-node
5. Paper can claim "distributed operation without coordination"

---
---

# PHASE C: Interval Tracking (Row-Level Conflicts)

## Goal

Reduce conflict granularity from table-level to row-level:
- Two transactions modifying different rows should NOT conflict
- Mathematical tracking of which rows each transaction touches
- O(log n) conflict detection, not O(n)

---

## The Problem

Current conflict detection:

```rust
fn conflicts(tx_a: &Transaction, tx_b: &Transaction) -> bool {
    // Table-level: any overlap = conflict
    tx_a.tables_written.intersection(&tx_b.tables_written).count() > 0
}
```

Two transactions both writing to "users" conflict, even if:
- Tx A writes row 1
- Tx B writes row 1,000,000

---

## The Solution: Interval Trees

Track which row ranges each transaction modifies:

```rust
#[derive(Clone, Debug)]
pub struct WriteSet {
    // Table -> intervals of rows written
    intervals: HashMap<String, IntervalTree<u64>>,
}

impl WriteSet {
    pub fn add_write(&mut self, table: &str, row_start: u64, row_end: u64) {
        self.intervals
            .entry(table.to_string())
            .or_default()
            .insert(row_start..row_end);
    }

    pub fn conflicts_with(&self, other: &WriteSet) -> bool {
        for (table, my_intervals) in &self.intervals {
            if let Some(their_intervals) = other.intervals.get(table) {
                if my_intervals.overlaps(their_intervals) {
                    return true;
                }
            }
        }
        false
    }
}
```

---

## Mathematical Foundation

### Interval Tree

An interval tree stores intervals and supports:
- **Insert:** O(log n)
- **Query overlap:** O(log n + k) where k = number of overlapping intervals
- **Space:** O(n)

### Conflict Detection Complexity

For two transactions with w₁ and w₂ write intervals:

$$\text{Time} = O(w_1 \times \log w_2)$$

For typical transactions (few writes): **O(log n)** vs current **O(1)** but with much finer granularity.

### Chunk-Level Tracking (Practical Middle Ground)

Instead of row-level (expensive), track at chunk level from Phase A:

```rust
pub struct ChunkWriteSet {
    // Table -> set of chunk hashes modified
    chunks: HashMap<String, HashSet<String>>,
}

impl ChunkWriteSet {
    pub fn conflicts_with(&self, other: &Self) -> bool {
        for (table, my_chunks) in &self.chunks {
            if let Some(their_chunks) = other.chunks.get(table) {
                if !my_chunks.is_disjoint(their_chunks) {
                    return true;
                }
            }
        }
        false
    }
}
```

**Complexity:** O(c) where c = chunks written (typically small)

---

## Integration with Merkle Trees

Phase A gives us chunk boundaries. Use them for conflict detection:

```rust
pub fn detect_conflicts(
    tx_a: &Transaction,
    tx_b: &Transaction,
    merkle_store: &MerkleStore,
) -> ConflictResult {
    let chunks_a = tx_a.get_modified_chunks(merkle_store);
    let chunks_b = tx_b.get_modified_chunks(merkle_store);

    let overlapping: Vec<_> = chunks_a.intersection(&chunks_b).collect();

    if overlapping.is_empty() {
        ConflictResult::NoConflict
    } else {
        ConflictResult::Conflict {
            chunks: overlapping,
            // Could resolve automatically or report to user
        }
    }
}
```

---

## Conflict Resolution Strategies

### 1. First-Committer-Wins (Current)

```rust
if conflicts(&tx_a, &tx_b) {
    if tx_a.commit_time < tx_b.commit_time {
        abort(tx_b)
    } else {
        abort(tx_a)
    }
}
```

### 2. Last-Writer-Wins (CRDT-style)

```rust
// No abort, just merge
let result = tx_a.changes.merge(tx_b.changes);  // LWW per cell
```

### 3. Application-Defined

```rust
if conflicts(&tx_a, &tx_b) {
    let resolution = user_resolver(
        tx_a.conflicting_rows(),
        tx_b.conflicting_rows()
    );
    apply(resolution)
}
```

---

## Data Structures

### Vector Clocks (Causal Ordering)

Track causality between transactions:

```rust
#[derive(Clone, Debug)]
pub struct VectorClock {
    clocks: HashMap<String, u64>,  // node_id -> logical_time
}

impl VectorClock {
    pub fn increment(&mut self, node_id: &str) {
        *self.clocks.entry(node_id.to_string()).or_default() += 1;
    }

    pub fn merge(&self, other: &Self) -> Self {
        let mut result = self.clocks.clone();
        for (k, v) in &other.clocks {
            let entry = result.entry(k.clone()).or_default();
            *entry = (*entry).max(*v);
        }
        VectorClock { clocks: result }
    }

    pub fn happened_before(&self, other: &Self) -> bool {
        self.clocks.iter().all(|(k, v)| {
            other.clocks.get(k).map_or(false, |ov| v <= ov)
        }) && self.clocks != other.clocks
    }

    pub fn concurrent_with(&self, other: &Self) -> bool {
        !self.happened_before(other) && !other.happened_before(self)
    }
}
```

### Transaction with Fine-Grained Tracking

```rust
pub struct TransactionV2 {
    pub tx_id: Uuid,
    pub vector_clock: VectorClock,

    // Chunk-level write tracking
    pub write_set: ChunkWriteSet,

    // For row-level (optional, more expensive)
    pub row_intervals: Option<IntervalWriteSet>,

    // Changes to apply
    pub changes: HashMap<String, MerkleTable>,
}
```

---

## Verification Tests (Phase C)

```python
def test_chunk_level_no_conflict():
    """Prove non-overlapping chunks don't conflict."""
    store = MerkleStore("./test", chunk_size=1000)

    # Write initial data
    df = pd.DataFrame({"id": range(10000), "value": range(10000)})
    store.write_table("data", df)

    # Two transactions modifying different row ranges
    with store.transaction() as tx_a:
        # Modify rows 0-999 (chunk 0)
        tx_a.update_rows("data", 0, 1000, new_values_a)

    with store.transaction() as tx_b:
        # Modify rows 9000-9999 (chunk 9)
        tx_b.update_rows("data", 9000, 10000, new_values_b)

    # Both should commit (no conflict)
    assert tx_a.committed
    assert tx_b.committed

def test_chunk_level_conflict():
    """Prove overlapping chunks do conflict."""
    with store.transaction() as tx_a:
        tx_a.update_rows("data", 500, 600)  # Chunk 0

    with store.transaction() as tx_b:
        tx_b.update_rows("data", 550, 650)  # Also chunk 0

    # One should abort
    assert tx_a.committed != tx_b.committed

def test_vector_clock_causality():
    """Prove vector clocks track causality."""
    vc_a = VectorClock()
    vc_b = VectorClock()

    vc_a.increment("node_a")  # a: {a: 1}
    vc_b = vc_a.clone()        # b: {a: 1}
    vc_b.increment("node_b")  # b: {a: 1, b: 1}

    assert vc_a.happened_before(vc_b)
    assert not vc_b.happened_before(vc_a)
    assert not vc_a.concurrent_with(vc_b)

    # Now make concurrent
    vc_c = VectorClock()
    vc_c.increment("node_c")  # c: {c: 1}

    assert vc_a.concurrent_with(vc_c)
```

---

## Phase C Success Criteria

1. Non-overlapping row modifications don't conflict
2. Conflict detection is O(chunks) not O(tables)
3. Vector clocks correctly identify concurrent vs causal transactions
4. Can provide row-level diffs for conflict resolution
5. Paper can claim "row-level conflict detection"

---
---

# Complete Architecture (All Phases)

```
┌─────────────────────────────────────────────────────────────┐
│                    Application Layer                         │
│  Python API  │  SQL (DuckDB)  │  Streaming  │  REST API     │
└──────────────────────────┬──────────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────────┐
│                   Transaction Manager                        │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────┐  │
│  │   Vector    │  │   Chunk     │  │   Conflict          │  │
│  │   Clocks    │  │   Write     │  │   Resolution        │  │
│  │  (Phase C)  │  │   Sets      │  │   Strategies        │  │
│  └─────────────┘  └─────────────┘  └─────────────────────┘  │
└──────────────────────────┬──────────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────────┐
│                    CRDT Catalog                              │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────┐  │
│  │    LWW      │  │   OR-Set    │  │     G-Counter       │  │
│  │  Registers  │  │   (chunks)  │  │    (versions)       │  │
│  │  (Phase B)  │  │             │  │                     │  │
│  └─────────────┘  └─────────────┘  └─────────────────────┘  │
└──────────────────────────┬──────────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────────┐
│                   Merkle Storage                             │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────┐  │
│  │   Merkle    │  │   Chunk     │  │   Incremental       │  │
│  │   Trees     │  │   Diffing   │  │   Updates           │  │
│  │  (Phase A)  │  │             │  │   O(log n)          │  │
│  └─────────────┘  └─────────────┘  └─────────────────────┘  │
└──────────────────────────┬──────────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────────┐
│                Content-Addressed Store                       │
│  ┌─────────────────────────────────────────────────────────┐│
│  │  BLAKE3 Hashing  │  Deduplication  │  Integrity Check  ││
│  │                      (Current)                          ││
│  └─────────────────────────────────────────────────────────┘│
│                           │                                  │
│              ┌────────────┼────────────┐                    │
│              ▼            ▼            ▼                    │
│         Local FS        S3/GCS      Azure Blob              │
└─────────────────────────────────────────────────────────────┘
```

---

# Implementation Priority

| Priority | Phase | Impact | Effort | Dependencies |
|----------|-------|--------|--------|--------------|
| **1** | A (Merkle) | High - enables true dedup | Medium | None |
| **2** | C (Intervals) | Medium - better concurrency | Low | Phase A |
| **3** | B (CRDTs) | High - enables distributed | High | Phase A |

**Recommendation:** Implement Phase A first. It's the foundation and delivers the biggest improvement to provable claims.

---

# Final Success Metrics

| Metric | Current | Phase A | Phase B | Phase C | Target |
|--------|---------|---------|---------|---------|--------|
| Dedup (5% change) | 0% | 85%+ | 85%+ | 85%+ | 85%+ |
| Distributed | No | No | Yes | Yes | Yes |
| Conflict granularity | Table | Chunk | Chunk | Row | Row |
| Coordination required | Single node | Single node | None | None | None |
| All claims provable | No | Partial | Mostly | **Yes** | **Yes** |
