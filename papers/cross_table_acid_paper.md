# Cross-Table ACID Transactions via Content-Addressable Storage

**A Practical Approach to Multi-Table Consistency in Data Lakehouses**

---

## Abstract

Modern data lakehouse formats (Delta Lake, Iceberg, Hudi) provide versioning and ACID guarantees for individual tables but cannot atomically commit changes across multiple tables. This limitation forces practitioners to choose between data consistency and architectural flexibility. We present Rhizo, a single-node storage system that achieves cross-table ACID transactions through content-addressable storage and a unified metadata catalog. Our approach requires no coordination service, operates at O(t) complexity for t-table transactions, and maintains backward compatibility with existing query engines. On commodity hardware, we measure 1,500+ MB/s write throughput, sub-2ms branch creation regardless of dataset size, and automatic deduplication of identical data. The system is implemented in Rust with Python bindings and passes 280 tests across both languages. We discuss limitations including single-node deployment and table-level conflict detection, and outline paths to distributed operation.

---

## 1. Introduction

The data lakehouse architecture promises to unify analytical and transactional workloads on commodity storage. Formats like Delta Lake, Apache Iceberg, and Apache Hudi have made significant progress toward this goal, providing table-level versioning, schema evolution, and time travel queries.

However, these formats share a fundamental limitation: **transactions are scoped to a single table**. Consider a common business requirement: updating a customer record, creating an order, and writing an audit log entry. In current lakehouse architectures, these operations cannot be atomic—a failure between any two writes leaves the system in an inconsistent state.

This paper makes the following contributions:

1. **Cross-table ACID transactions** on a single node without external coordination services
2. **Zero-copy branching** enabling safe experimentation on production data
3. **Automatic deduplication** of identical content across tables and versions
4. **O(1) version lookup** for time travel queries (data access remains O(n))
5. **A working implementation** with benchmarks on commodity hardware

**Scope and limitations:** Rhizo currently operates on a single node. The atomic commit mechanism relies on filesystem rename operations, which are atomic on local filesystems but not on cloud object stores. Distributed deployment requires additional coordination, discussed in Section 8.

---

## 2. Background and Motivation

### 2.1 The Multi-Table Consistency Problem

Real-world data systems rarely consist of isolated tables. A typical e-commerce system might include:

- `customers` — user profiles and preferences
- `orders` — purchase records referencing customers
- `inventory` — stock levels updated on each order
- `audit_log` — compliance records for all changes

Business logic often requires atomically updating multiple tables. When a customer places an order:

1. Decrement inventory
2. Create order record
3. Update customer loyalty points
4. Write audit entry

In current lakehouse formats, each table maintains its own transaction log. There is no mechanism to ensure these four operations succeed or fail together. Practitioners resort to:

- **Application-level coordination** — Complex, error-prone, doesn't survive process failures
- **External transaction managers** — Additional infrastructure, latency, operational burden
- **Eventual consistency** — Acceptable for some workloads, unacceptable for others

### 2.2 Why Current Formats Cannot Solve This

Delta Lake [5], Iceberg [6], and Hudi [7] use per-table transaction logs stored alongside data files. Each table's log is an independent sequence of commits:

```
delta_log/
  00000000000000000001.json
  00000000000000000002.json
  ...
```

Atomic cross-table commits would require either:

1. **Distributed consensus** between table logs (expensive, complex)
2. **A unified log** spanning all tables (architectural change)

Current formats chose per-table logs for good reasons: simplicity, independence, and compatibility with existing object stores. But this choice makes cross-table atomicity fundamentally impossible without external coordination.

### 2.3 Content-Addressable Storage: A Different Foundation

We observe that content-addressable storage (CAS) provides a natural foundation for multi-table transactions. In CAS, data is identified by its cryptographic hash rather than its location:

```
chunk_id = BLAKE3(content)
path = f"chunks/{chunk_id[:2]}/{chunk_id[2:4]}/{chunk_id}"
```

This seemingly simple change has profound implications:

1. **Deduplication is automatic** — Identical content produces identical hashes
2. **Integrity is built-in** — Any corruption is detected on read
3. **Versioning is natural** — Old versions are never overwritten
4. **Branching is zero-copy** — Branches share all common chunks

Most importantly for our purposes: **if all tables store chunks in the same CAS, a single metadata commit can atomically reference new versions of multiple tables**.

---

## 3. System Design

### 3.1 Architecture Overview

Rhizo consists of three layers:

```
                    Query Layer
        (DuckDB integration, SQL, time travel)
                        |
                        v
                   FileCatalog
        (Versioned metadata, branch management)
                        |
                        v
                    ChunkStore
        (Content-addressed storage, BLAKE3)
                        |
                        v
                   Local File System
```

**ChunkStore** provides content-addressed storage with atomic writes. Data is serialized (typically as Parquet), hashed with BLAKE3, and stored at a path derived from the hash. The store guarantees:

- Atomic write (write-to-temp-rename pattern)
- Integrity verification on every read
- Automatic deduplication (identical content → same hash → same file)

**FileCatalog** maintains versioned metadata for tables and branches. Each table version records:

- Schema information
- List of chunk references (hashes)
- Row count and statistics
- Parent version (for lineage)

**Query Layer** integrates with DuckDB for SQL execution, resolving table references through the catalog and reconstructing data from chunks.

### 3.2 Cross-Table Transaction Protocol

A cross-table transaction proceeds as follows:

1. **Begin**: Acquire transaction ID, record snapshot of current table versions
2. **Buffer writes**: Accumulate changes in memory, write chunks to CAS
3. **Validate**: Check for conflicts with concurrent transactions (table-level)
4. **Commit**: Atomically write new catalog state with all table versions

The critical insight is step 4. Because all table metadata lives in a single catalog file, and we use atomic file rename, **a single rename commits changes to all tables simultaneously**.

```rust
// Simplified commit logic
fn commit(&self, changes: HashMap<String, TableVersion>) -> Result<()> {
    let commit_record = CommitRecord {
        transaction_id: self.id,
        timestamp: now(),
        table_versions: changes,
    };

    // Write to temp file, then atomic rename
    let temp_path = self.catalog_path.with_extension("tmp");
    fs::write(&temp_path, serialize(&commit_record))?;
    fs::rename(&temp_path, &self.catalog_path)?;  // Atomic on local FS
    Ok(())
}
```

**Important caveat:** The `fs::rename` operation is atomic on POSIX filesystems and NTFS, but not on cloud object stores like S3 or GCS. Extending this to distributed storage requires either conditional writes (where supported) or an external coordination service.

### 3.3 Conflict Detection

We implement snapshot isolation, the same model used by PostgreSQL and Oracle [4]. Transactions see a consistent snapshot and are checked for conflicts at commit time.

Two transactions T_i and T_j conflict if and only if:

$$\text{Conflict}(T_i, T_j) = (W_i \cap W_j \neq \emptyset) \land (\text{concurrent})$$

Where W_i is the write set of transaction i. **Currently, conflict detection operates at table granularity**—if two transactions both write to the same table, one must abort. Row-level conflict detection is planned for future work.

### 3.4 Zero-Copy Branching

A branch is simply a named pointer to a set of table versions:

```rust
struct Branch {
    name: String,
    head: HashMap<String, u64>,  // table -> version
    created_at: Timestamp,
    parent_branch: Option<String>,
}
```

Creating a branch copies only this metadata structure (~200 bytes), not the underlying data. All branches share the same ChunkStore, so common data is stored exactly once.

This enables workflows difficult in current lakehouse formats:

- **Safe experimentation**: Branch production, experiment, discard or merge
- **What-if analysis**: Create branches for different scenarios
- **Code review for data**: Branch, transform, diff, review, merge

---

## 4. Theoretical Foundations

### 4.1 Complexity Analysis

| Operation | Time | Space | Notes |
|-----------|------|-------|-------|
| Write chunk | O(n) | O(n) | n = data size, includes hashing |
| Read chunk | O(n) | O(n) | Includes integrity verification |
| Check exists | O(1) | O(1) | Hash to path conversion |
| Version lookup | O(1) | O(1) | Direct catalog access |
| Query at version | O(n) | O(n) | n = result size; lookup is O(1) |
| Commit | O(k) | O(k) | k = chunks in transaction |
| Create branch | O(t) | O(t) | t = table count (metadata only) |
| Cross-table commit | O(t) | O(t) | t = tables in transaction |

**Clarification on "O(1) time travel":** Version lookup is O(1)—we directly access the version's metadata without replaying a log. However, reading the data at that version is still O(n) where n is the data size. The key advantage is avoiding log replay, which in Delta Lake and Iceberg requires reconstructing state from a sequence of commits.

### 4.2 Storage Deduplication Analysis

For a dataset with original size S, V versions, and change rate r per version:

**Naive storage (no deduplication):**
$$S_{naive} = S \times V$$

**Content-addressed storage:**
$$S_{dedup} = S \times (1 + (V-1) \times r)$$

**Storage savings:**
$$\text{Savings} = 1 - \frac{1 + (V-1) \times r}{V}$$

For 30 versions with 5% daily changes:
$$S_{dedup} = S \times (1 + 29 \times 0.05) = 2.45S$$
$$\text{Savings} = 1 - \frac{2.45}{30} = 91.8\%$$

**Real-world considerations:** This formula assumes:
1. Changes distribute evenly across chunks (often untrue—updates cluster)
2. Chunk boundaries align with changes (content-defined chunking helps but isn't perfect)
3. No metadata overhead (small but nonzero)

Empirical studies of content-defined chunking report 70-90% alignment efficiency [1]. With this factor, practical savings for our scenario would be 64-83%.

### 4.3 Collision Probability

BLAKE3 produces 256-bit hashes. The collision probability follows the birthday bound [2]:

$$P(\text{collision}) \approx \frac{n^2}{2^{b+1}}$$

For n = 10^15 chunks (exabyte-scale deployment):

$$P(\text{collision}) = \frac{10^{30}}{2^{257}} \approx 4.3 \times 10^{-48}$$

For comparison, undetected RAM bit flips occur at approximately 10^-13 per year [3]. Hash collision is not a practical concern for any realistic deployment.

---

## 5. Implementation

Rhizo is implemented in Rust for the core storage and catalog layers, with Python bindings via PyO3. The query layer is pure Python integrating with DuckDB.

### 5.1 Code Statistics

| Component | Language | Tests |
|-----------|----------|-------|
| Core (ChunkStore, Catalog, Branch, Transaction, Merkle, Parquet) | Rust | 173 |
| Query layer (time travel, branching, transactions, changelog, OLAP) | Python | 247 |
| **Total** | | **420** |

**Note:** The OLAP engine (Phase DF) added DataFusion integration with 26x faster reads than DuckDB, TIME TRAVEL SQL syntax (`VERSION` keyword), branch queries (`@branch` notation), and changelog SQL (`__changelog` virtual table).

### 5.2 Key Implementation Details

**Atomic Writes**: All mutations use the write-to-temp-rename pattern:

```rust
fn atomic_write(path: &Path, data: &[u8]) -> io::Result<()> {
    let temp = path.with_extension("tmp");
    fs::write(&temp, data)?;
    fs::rename(&temp, path)?;
    Ok(())
}
```

**Chunk Addressing**: Two-level directory structure prevents filesystem limitations on files per directory:

```
chunks/
  ab/
    cd/
      abcd1234...  (full hash as filename)
```

**Integrity Verification**: Every read verifies content against its hash:

```rust
fn read_chunk(&self, hash: &str) -> Result<Vec<u8>> {
    let data = fs::read(self.chunk_path(hash))?;
    let actual_hash = blake3::hash(&data).to_hex();
    if actual_hash != hash {
        return Err(ChunkStoreError::IntegrityError);
    }
    Ok(data)
}
```

---

## 6. Evaluation

All benchmarks run on commodity hardware (AMD Ryzen 5, NVMe SSD, 32GB RAM, Windows 11).

### 6.1 Throughput

| Operation | Throughput | Notes |
|-----------|------------|-------|
| Write (with BLAKE3) | 1,542 MB/s | Includes hashing and file write |
| Read (with verify) | 446 MB/s | Includes integrity verification |

Write throughput is dominated by BLAKE3 hashing (which itself achieves ~3 GB/s on this hardware). The gap between write and read is due to the verification step on read.

### 6.2 Branching Performance

| Dataset Size | Branch Creation Time | Storage Overhead |
|--------------|---------------------|------------------|
| 1,000 rows | 2.1 ms | ~200 bytes |
| 10,000 rows | 0.8 ms | ~200 bytes |
| 50,000 rows | 0.8 ms | ~200 bytes |
| 100,000 rows | 1.0 ms | ~200 bytes |

Branch creation time is effectively constant regardless of data size because only metadata pointers are copied. The slight variations are measurement noise.

### 6.3 Deduplication

**Merkle Tree Storage (Implemented):**

With our Merkle tree implementation, incremental changes achieve chunk-level deduplication:

| Change Percentage | Chunk Reuse | Storage Savings (2 versions) |
|-------------------|-------------|------------------------------|
| 1% | 98.8% | 49.4% |
| 5% | 95.0% | 47.5% |
| 10% | 90.0% | 45.0% |
| 25% | 75.0% | 37.5% |
| 50% | 50.0% | 25.0% |

These results demonstrate **O(change) storage** instead of O(n) per version. For a 10MB dataset with 5% changes:
- **Without Merkle**: 2 versions = 20 MB (full copy each time)
- **With Merkle**: 2 versions = 10.5 MB (original + 0.5 MB changed chunks)

**Additional verified results:**
- Identical content deduplication: 5x reduction (as before)
- Merkle tree build time: ~4ms for 10MB data
- Diff computation: <1ms regardless of data size
- Change location independent: Beginning, middle, and end changes achieve identical reuse ratios
- Scattered changes (every Nth byte): 0% reuse, demonstrating that granularity matters

**Note:** Scattered modifications that touch every chunk cannot benefit from deduplication. Real-world workloads with localized changes (append, update range) achieve near-theoretical savings.

### 6.4 Time Travel Queries

| Versions in History | Query Time | Notes |
|---------------------|------------|-------|
| 20 | 8.7 ms | Constant regardless of which version queried |

Time travel query time is dominated by data access, not version lookup. Querying version 1 takes the same time as version 20.

### 6.5 Comparison with Existing Formats

| Feature | Rhizo | Delta Lake | Iceberg | Hudi |
|---------|------------|------------|---------|------|
| Cross-table ACID | Yes (single-node) | No | No | No |
| Zero-copy branching | Yes | No | No | No |
| Content deduplication | Yes | No | No | No |
| Built-in integrity check | Yes | External | External | External |
| Version lookup | O(1) | O(log n)* | O(log n)* | O(log n)* |
| Distributed | No | Yes | Yes | Yes |

*Delta Lake, Iceberg, and Hudi require reconstructing state from transaction logs.

---

## 7. Related Work

**Delta Lake** [5] introduced ACID transactions to data lakes with a transaction log stored alongside Parquet files. It provides excellent single-table semantics and operates at scale on cloud object stores. Cross-table transactions require external coordination.

**Apache Iceberg** [6] offers similar capabilities with a focus on schema evolution and hidden partitioning. Like Delta Lake, transactions are table-scoped. Iceberg's metadata structure is more complex but enables efficient partition pruning.

**Apache Hudi** [7] emphasizes incremental processing and record-level updates. It provides upsert capabilities and change streams but not cross-table atomicity.

**LakeFS** [8] provides Git-like branching for data lakes, layered on top of object storage. It operates at the object level rather than the table level, and does not provide cross-table transactions. Rhizo's branching operates at the table version level with tighter integration.

**Nessie** [9] offers Git-like semantics specifically for Iceberg catalogs. It provides branching and tagging but inherits Iceberg's single-table transaction model. Nessie could potentially coordinate cross-table commits through its catalog, but this is not its current design.

**DVC** (Data Version Control) [10] applies Git principles to ML datasets and models. It tracks large files via content hashing but focuses on artifact versioning rather than queryable tables with SQL access.

**Git** pioneered content-addressable storage for version control. Rhizo applies similar principles to structured data, with the key additions of query engine integration and table-level semantics.

**IPFS** [11] uses content addressing for distributed file storage. Our work adapts these concepts for the specific requirements of analytical data systems, trading distribution for transactional guarantees.

---

## 8. Limitations and Future Work

### Current Limitations

**Single-node only:** The atomic rename mechanism requires a local filesystem. Cloud object stores (S3, GCS, Azure Blob) do not provide atomic rename across objects. Distributed deployment would require either:
- Conditional writes (S3 supports `If-None-Match`, but not atomic multi-object updates)
- An external coordination service (ZooKeeper, etcd)
- A distributed consensus protocol

**Table-level conflict detection:** Two concurrent transactions writing to the same table will conflict, even if they modify different rows. Row-level conflict detection requires tracking write sets at finer granularity.

**Fixed-size chunking:** The current Merkle tree implementation uses fixed-size chunks (default 64KB). Content-defined chunking (e.g., FastCDC [1]) would improve deduplication when data shifts (insertions/deletions) rather than just modifications.

**Merge is fast-forward only:** Diverged branches require manual reconciliation. Three-way merge with conflict resolution is planned.

### Planned Enhancements

1. **Row-level conflict detection** using Merkle tree chunk-level write tracking
2. **Content-defined chunking** (FastCDC) for improved deduplication with insertions/deletions
3. **Three-way merge** with configurable conflict resolution
4. **Distributed coordination** via pluggable backend (etcd, S3 conditional writes)
5. **Integration of Merkle storage with query layer** for transparent versioned data access

---

## 9. Conclusion

We have presented Rhizo, a single-node data storage system that achieves cross-table ACID transactions through content-addressable storage. By storing all data in a shared ChunkStore and maintaining table metadata in a unified catalog, atomic multi-table commits become possible without external coordination.

Our implementation demonstrates that this approach is practical on a single node: 1,500+ MB/s write throughput, sub-2ms branching, and automatic deduplication of identical content. The system passes 420 tests (173 Rust + 247 Python) and integrates with standard query engines via DuckDB and DataFusion.

The key insight is architectural: per-table transaction logs make cross-table atomicity fundamentally impossible, while a unified content-addressed foundation makes it straightforward—at least on a single node. Extending these guarantees to distributed deployments remains future work.

Rhizo is open source under the MIT license at: https://github.com/aquadantheman/unifieddataruntime

---

## References

[1] Xia, W., Jiang, H., Feng, D., Tian, L., Fu, M., & Zhou, Y. (2016). FastCDC: A Fast and Efficient Content-Defined Chunking Approach for Data Deduplication. USENIX ATC.

[2] Bellare, M., & Rogaway, P. (2005). Introduction to Modern Cryptography. Lecture Notes.

[3] Schroeder, B., Pinheiro, E., & Weber, W. D. (2009). DRAM Errors in the Wild: A Large-Scale Field Study. SIGMETRICS. doi:10.1145/1555349.1555372

[4] Berenson, H., Bernstein, P., Gray, J., Melton, J., O'Neil, E., & O'Neil, P. (1995). A Critique of ANSI SQL Isolation Levels. SIGMOD. doi:10.1145/223784.223785

[5] Armbrust, M., et al. (2020). Delta Lake: High-Performance ACID Table Storage over Cloud Object Stores. VLDB.

[6] Apache Iceberg. https://iceberg.apache.org/

[7] Apache Hudi. https://hudi.apache.org/

[8] LakeFS. https://lakefs.io/

[9] Project Nessie. https://projectnessie.org/

[10] DVC (Data Version Control). https://dvc.org/

[11] Benet, J. (2014). IPFS - Content Addressed, Versioned, P2P File System. arXiv:1407.3561.

---

## Appendix A: Verification Code

```python
# Collision probability calculation
n = 10**15  # chunks (exabyte scale)
b = 256     # BLAKE3 output bits
p = (n ** 2) / (2 ** (b + 1))
print(f"Collision probability: {p:.2e}")  # 4.31e-48

# Deduplication savings (theoretical)
S = 1  # normalized original size
V = 30  # versions
r = 0.05  # 5% change rate
S_dedup = S * (1 + (V - 1) * r)
savings = 1 - (S_dedup / (S * V))
print(f"Theoretical storage savings: {savings:.1%}")  # 91.8%

# With chunking efficiency factor
efficiency = 0.75  # 70-90% typical
practical_savings = savings * efficiency
print(f"Practical savings estimate: {practical_savings:.1%}")  # ~69%
```
