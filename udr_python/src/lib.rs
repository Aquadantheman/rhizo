use pyo3::prelude::*;
use pyo3::exceptions::{PyIOError, PyValueError, PyRuntimeError};
use udr_core::{
    ChunkStore, ChunkStoreError,
    FileCatalog, CatalogError, TableVersion,
    Branch, BranchDiff, BranchError, BranchManager,
    TransactionManager, TransactionRecord, TransactionError,
    TableWrite, RecoveryReport,
    ChangelogEntry, TableChange, ChangelogQuery,
    MerkleTree, MerkleNode, DataChunk, MerkleDiff, MerkleConfig, MerkleError,
    build_tree, diff_trees, verify_tree,
};
use std::collections::HashMap;
use std::sync::Arc;

/// Convert ChunkStoreError to appropriate Python exception
fn chunk_err_to_py(e: ChunkStoreError) -> PyErr {
    match e {
        ChunkStoreError::NotFound(h) => PyIOError::new_err(format!("Chunk not found: {}", h)),
        ChunkStoreError::InvalidHash(msg) => PyValueError::new_err(format!("Invalid hash: {}", msg)),
        ChunkStoreError::HashMismatch { expected, actual } => {
            PyValueError::new_err(format!("Hash mismatch: expected {}, got {}", expected, actual))
        }
        ChunkStoreError::Io(e) => PyIOError::new_err(e.to_string()),
    }
}

/// Convert CatalogError to appropriate Python exception
fn catalog_err_to_py(e: CatalogError) -> PyErr {
    match e {
        CatalogError::TableNotFound(t) => PyIOError::new_err(format!("Table not found: {}", t)),
        CatalogError::VersionNotFound(t, v) => {
            PyIOError::new_err(format!("Version not found: {} v{}", t, v))
        }
        CatalogError::InvalidVersion { expected, got } => {
            PyValueError::new_err(format!("Invalid version: expected {}, got {}", expected, got))
        }
        CatalogError::LatestPointerCorrupted(t) => {
            PyIOError::new_err(format!("Latest pointer corrupted for table: {}", t))
        }
        CatalogError::Io(e) => PyIOError::new_err(e.to_string()),
        CatalogError::Json(e) => PyValueError::new_err(format!("JSON error: {}", e)),
    }
}

/// Convert BranchError to appropriate Python exception
fn branch_err_to_py(e: BranchError) -> PyErr {
    match e {
        BranchError::BranchNotFound(name) => {
            PyIOError::new_err(format!("Branch not found: {}", name))
        }
        BranchError::BranchAlreadyExists(name) => {
            PyValueError::new_err(format!("Branch already exists: {}", name))
        }
        BranchError::CannotDeleteDefault(name) => {
            PyValueError::new_err(format!("Cannot delete default branch: {}", name))
        }
        BranchError::InvalidBranchName(msg) => {
            PyValueError::new_err(format!("Invalid branch name: {}", msg))
        }
        BranchError::MergeConflict(tables) => {
            PyValueError::new_err(format!("Merge conflict on tables: {:?}", tables))
        }
        BranchError::CannotFastForward { source_branch, target_branch } => {
            PyValueError::new_err(format!(
                "Cannot fast-forward: {} is not ahead of {}",
                source_branch, target_branch
            ))
        }
        BranchError::Io(e) => PyIOError::new_err(e.to_string()),
        BranchError::Json(e) => PyValueError::new_err(format!("JSON error: {}", e)),
    }
}

/// Convert MerkleError to appropriate Python exception
fn merkle_err_to_py(e: MerkleError) -> PyErr {
    match e {
        MerkleError::Io(e) => PyIOError::new_err(e.to_string()),
        MerkleError::ChunkNotFound(hash) => {
            PyValueError::new_err(format!("Chunk not found: {}", hash))
        }
        MerkleError::InvalidChunkSize(size) => {
            PyValueError::new_err(format!("Invalid chunk size: must be > 0, got {}", size))
        }
        MerkleError::EmptyData => {
            PyValueError::new_err("Cannot build Merkle tree from empty data")
        }
        MerkleError::IntegrityError { expected, actual } => {
            PyValueError::new_err(format!("Integrity error: expected {}, got {}", expected, actual))
        }
        MerkleError::TreeCorruption(msg) => {
            PyValueError::new_err(format!("Tree corruption: {}", msg))
        }
        MerkleError::Serialization(msg) => {
            PyValueError::new_err(format!("Serialization error: {}", msg))
        }
        MerkleError::ChunkStore(msg) => {
            PyIOError::new_err(format!("Chunk store error: {}", msg))
        }
    }
}

#[pyclass]
struct PyChunkStore {
    inner: ChunkStore,
}

#[pymethods]
impl PyChunkStore {
    #[new]
    fn new(path: &str) -> PyResult<Self> {
        let inner = ChunkStore::new(path).map_err(chunk_err_to_py)?;
        Ok(Self { inner })
    }

    fn put(&self, data: &[u8]) -> PyResult<String> {
        self.inner.put(data).map_err(chunk_err_to_py)
    }

    fn get(&self, hash: &str) -> PyResult<Vec<u8>> {
        self.inner.get(hash).map_err(chunk_err_to_py)
    }

    /// Get chunk data with integrity verification.
    /// Raises ValueError if the data doesn't match the expected hash.
    fn get_verified(&self, hash: &str) -> PyResult<Vec<u8>> {
        self.inner.get_verified(hash).map_err(chunk_err_to_py)
    }

    fn exists(&self, hash: &str) -> PyResult<bool> {
        self.inner.exists(hash).map_err(chunk_err_to_py)
    }

    fn delete(&self, hash: &str) -> PyResult<()> {
        self.inner.delete(hash).map_err(chunk_err_to_py)
    }

    // =========================================================================
    // Batch Operations (Parallel)
    // =========================================================================

    /// Store multiple chunks in parallel, returning their hashes.
    ///
    /// This is significantly faster than calling `put()` in a loop because:
    /// 1. BLAKE3 hashing runs in parallel across CPU cores
    /// 2. Disk I/O is parallelized
    /// 3. Single FFI call overhead instead of N calls
    ///
    /// Args:
    ///     chunks: List of byte arrays to store
    ///
    /// Returns:
    ///     List of hashes in the same order as input chunks
    ///
    /// Example:
    ///     >>> store = PyChunkStore("./data")
    ///     >>> hashes = store.put_batch([b"chunk1", b"chunk2", b"chunk3"])
    ///     >>> len(hashes)
    ///     3
    fn put_batch(&self, chunks: Vec<Vec<u8>>) -> PyResult<Vec<String>> {
        let refs: Vec<&[u8]> = chunks.iter().map(|c| c.as_slice()).collect();
        self.inner.put_batch(&refs).map_err(chunk_err_to_py)
    }

    /// Retrieve multiple chunks in parallel by their hashes.
    ///
    /// Returns results in the same order as input hashes.
    /// If any chunk is not found, raises an IOError.
    ///
    /// Args:
    ///     hashes: List of hash strings to retrieve
    ///
    /// Returns:
    ///     List of chunk data (bytes) in the same order as input hashes
    ///
    /// Example:
    ///     >>> store = PyChunkStore("./data")
    ///     >>> h1 = store.put(b"data1")
    ///     >>> h2 = store.put(b"data2")
    ///     >>> results = store.get_batch([h1, h2])
    ///     >>> results[0]
    ///     b'data1'
    fn get_batch(&self, hashes: Vec<String>) -> PyResult<Vec<Vec<u8>>> {
        let refs: Vec<&str> = hashes.iter().map(|s| s.as_str()).collect();
        self.inner.get_batch(&refs).map_err(chunk_err_to_py)
    }

    /// Retrieve multiple chunks with integrity verification in parallel.
    ///
    /// Like `get_batch`, but verifies each chunk's integrity by comparing
    /// its content hash to the expected hash.
    ///
    /// Args:
    ///     hashes: List of hash strings to retrieve and verify
    ///
    /// Returns:
    ///     List of verified chunk data (bytes) in the same order as input
    ///
    /// Raises:
    ///     ValueError: If any chunk fails integrity verification
    fn get_batch_verified(&self, hashes: Vec<String>) -> PyResult<Vec<Vec<u8>>> {
        let refs: Vec<&str> = hashes.iter().map(|s| s.as_str()).collect();
        self.inner.get_batch_verified(&refs).map_err(chunk_err_to_py)
    }

    // =========================================================================
    // Memory-Mapped Operations
    // =========================================================================

    /// Get chunk data using memory-mapped I/O.
    ///
    /// This can be faster than `get()` for large chunks because:
    /// 1. Uses OS page caching efficiently
    /// 2. Data is demand-paged (only accessed pages are loaded)
    /// 3. No intermediate buffering in Rust layer
    ///
    /// Note: The returned bytes are copied to Python. For true zero-copy,
    /// future versions may expose a buffer protocol interface.
    ///
    /// Args:
    ///     hash: Hash of the chunk to retrieve
    ///
    /// Returns:
    ///     Chunk data as bytes
    fn get_mmap(&self, hash: &str) -> PyResult<Vec<u8>> {
        let mmap = self.inner.get_mmap(hash).map_err(chunk_err_to_py)?;
        Ok(mmap.to_vec())
    }

    /// Get multiple chunks using memory-mapped I/O in parallel.
    ///
    /// Combines the benefits of parallel I/O with memory-mapped access.
    /// Results are returned in the same order as input hashes.
    ///
    /// Args:
    ///     hashes: List of chunk hashes to retrieve
    ///
    /// Returns:
    ///     List of chunk data in the same order as input
    fn get_mmap_batch(&self, hashes: Vec<String>) -> PyResult<Vec<Vec<u8>>> {
        let refs: Vec<&str> = hashes.iter().map(|s| s.as_str()).collect();
        let mmaps = self.inner.get_mmap_batch(&refs).map_err(chunk_err_to_py)?;
        Ok(mmaps.iter().map(|m| m.to_vec()).collect())
    }
}

#[pyclass]
#[derive(Clone)]
struct PyTableVersion {
    #[pyo3(get)]
    table_name: String,
    #[pyo3(get)]
    version: u64,
    #[pyo3(get)]
    chunk_hashes: Vec<String>,
    #[pyo3(get)]
    schema_hash: Option<String>,
    #[pyo3(get)]
    created_at: i64,
    #[pyo3(get)]
    parent_version: Option<u64>,
    #[pyo3(get)]
    metadata: HashMap<String, String>,
}

#[pymethods]
impl PyTableVersion {
    #[new]
    fn new(table_name: String, version: u64, chunk_hashes: Vec<String>) -> Self {
        Self {
            table_name,
            version,
            chunk_hashes,
            schema_hash: None,
            created_at: std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_secs() as i64,
            parent_version: if version > 1 { Some(version - 1) } else { None },
            metadata: HashMap::new(),
        }
    }
}

impl From<TableVersion> for PyTableVersion {
    fn from(tv: TableVersion) -> Self {
        Self {
            table_name: tv.table_name,
            version: tv.version,
            chunk_hashes: tv.chunk_hashes,
            schema_hash: tv.schema_hash,
            created_at: tv.created_at,
            parent_version: tv.parent_version,
            metadata: tv.metadata,
        }
    }
}

impl From<PyTableVersion> for TableVersion {
    fn from(ptv: PyTableVersion) -> Self {
        Self {
            table_name: ptv.table_name,
            version: ptv.version,
            chunk_hashes: ptv.chunk_hashes,
            schema_hash: ptv.schema_hash,
            created_at: ptv.created_at,
            parent_version: ptv.parent_version,
            metadata: ptv.metadata,
        }
    }
}

#[pyclass]
struct PyCatalog {
    inner: FileCatalog,
}

#[pymethods]
impl PyCatalog {
    #[new]
    fn new(path: &str) -> PyResult<Self> {
        let inner = FileCatalog::new(path).map_err(catalog_err_to_py)?;
        Ok(Self { inner })
    }

    fn commit(&self, version: PyTableVersion) -> PyResult<u64> {
        self.inner.commit(version.into()).map_err(catalog_err_to_py)
    }

    #[pyo3(signature = (table_name, version=None))]
    fn get_version(&self, table_name: &str, version: Option<u64>) -> PyResult<PyTableVersion> {
        self.inner
            .get_version(table_name, version)
            .map(|tv| tv.into())
            .map_err(catalog_err_to_py)
    }

    fn list_versions(&self, table_name: &str) -> PyResult<Vec<u64>> {
        self.inner.list_versions(table_name).map_err(catalog_err_to_py)
    }

    fn list_tables(&self) -> PyResult<Vec<String>> {
        self.inner.list_tables().map_err(catalog_err_to_py)
    }
}

// ============================================================================
// Branch Classes
// ============================================================================

#[pyclass]
#[derive(Clone)]
struct PyBranch {
    #[pyo3(get)]
    name: String,
    #[pyo3(get)]
    head: HashMap<String, u64>,
    #[pyo3(get)]
    created_at: i64,
    #[pyo3(get)]
    parent_branch: Option<String>,
    #[pyo3(get)]
    description: Option<String>,
}

impl From<Branch> for PyBranch {
    fn from(b: Branch) -> Self {
        Self {
            name: b.name,
            head: b.head,
            created_at: b.created_at,
            parent_branch: b.parent_branch,
            description: b.description,
        }
    }
}

#[pyclass]
#[derive(Clone)]
struct PyBranchDiff {
    #[pyo3(get)]
    source_branch: String,
    #[pyo3(get)]
    target_branch: String,
    #[pyo3(get)]
    unchanged: Vec<String>,
    #[pyo3(get)]
    modified: Vec<(String, u64, u64)>,
    #[pyo3(get)]
    added_in_source: Vec<(String, u64)>,
    #[pyo3(get)]
    added_in_target: Vec<(String, u64)>,
    #[pyo3(get)]
    has_conflicts: bool,
}

impl From<BranchDiff> for PyBranchDiff {
    fn from(d: BranchDiff) -> Self {
        Self {
            source_branch: d.source_branch,
            target_branch: d.target_branch,
            unchanged: d.unchanged,
            modified: d.modified,
            added_in_source: d.added_in_source,
            added_in_target: d.added_in_target,
            has_conflicts: d.has_conflicts,
        }
    }
}

#[pyclass]
struct PyBranchManager {
    inner: BranchManager,
}

#[pymethods]
impl PyBranchManager {
    #[new]
    fn new(path: &str) -> PyResult<Self> {
        let inner = BranchManager::new(path).map_err(branch_err_to_py)?;
        Ok(Self { inner })
    }

    /// Create a new branch from an existing branch.
    /// If from_branch is None, creates from the default branch (main).
    #[pyo3(signature = (name, from_branch=None, description=None))]
    fn create(
        &self,
        name: &str,
        from_branch: Option<&str>,
        description: Option<&str>,
    ) -> PyResult<PyBranch> {
        self.inner
            .create(name, from_branch, description)
            .map(|b| b.into())
            .map_err(branch_err_to_py)
    }

    /// Get a branch by name.
    fn get(&self, name: &str) -> PyResult<PyBranch> {
        self.inner.get(name).map(|b| b.into()).map_err(branch_err_to_py)
    }

    /// List all branch names.
    fn list(&self) -> PyResult<Vec<String>> {
        self.inner.list().map_err(branch_err_to_py)
    }

    /// Delete a branch. Cannot delete the default branch.
    fn delete(&self, name: &str) -> PyResult<()> {
        self.inner.delete(name).map_err(branch_err_to_py)
    }

    /// Update the head pointer for a table on a branch.
    fn update_head(&self, branch_name: &str, table_name: &str, version: u64) -> PyResult<()> {
        self.inner
            .update_head(branch_name, table_name, version)
            .map_err(branch_err_to_py)
    }

    /// Get the version of a table on a branch.
    fn get_table_version(&self, branch_name: &str, table_name: &str) -> PyResult<Option<u64>> {
        self.inner
            .get_table_version(branch_name, table_name)
            .map_err(branch_err_to_py)
    }

    /// Compare two branches.
    fn diff(&self, source: &str, target: &str) -> PyResult<PyBranchDiff> {
        self.inner
            .diff(source, target)
            .map(|d| d.into())
            .map_err(branch_err_to_py)
    }

    /// Check if a fast-forward merge is possible.
    fn can_fast_forward(&self, source: &str, target: &str) -> PyResult<bool> {
        self.inner
            .can_fast_forward(source, target)
            .map_err(branch_err_to_py)
    }

    /// Merge source branch into target branch (fast-forward only).
    fn merge(&self, source: &str, into: &str) -> PyResult<()> {
        self.inner
            .merge_fast_forward(source, into)
            .map_err(branch_err_to_py)
    }

    /// Get the default branch name.
    fn get_default(&self) -> PyResult<Option<String>> {
        self.inner.get_default().map_err(branch_err_to_py)
    }

    /// Set the default branch.
    fn set_default(&self, name: &str) -> PyResult<()> {
        self.inner.set_default(name).map_err(branch_err_to_py)
    }
}

// ============================================================================
// Transaction Classes
// ============================================================================

/// Convert TransactionError to appropriate Python exception
fn tx_err_to_py(e: TransactionError) -> PyErr {
    match e {
        TransactionError::TransactionNotFound(id) => {
            PyValueError::new_err(format!("Transaction not found: {}", id))
        }
        TransactionError::TransactionNotActive(id) => {
            PyValueError::new_err(format!("Transaction {} is not active", id))
        }
        TransactionError::AlreadyCommitted(id) => {
            PyValueError::new_err(format!("Transaction {} already committed", id))
        }
        TransactionError::AlreadyAborted(id) => {
            PyValueError::new_err(format!("Transaction {} already aborted", id))
        }
        TransactionError::WriteConflict(tables) => {
            PyValueError::new_err(format!("Write conflict on tables: {:?}", tables))
        }
        TransactionError::SnapshotConflict { table, read_version, current_version } => {
            PyValueError::new_err(format!(
                "Snapshot conflict: {} was v{}, now v{}",
                table, read_version, current_version
            ))
        }
        TransactionError::Io(e) => PyIOError::new_err(e.to_string()),
        TransactionError::Json(e) => PyValueError::new_err(format!("JSON error: {}", e)),
        TransactionError::CatalogError(msg) => PyIOError::new_err(format!("Catalog error: {}", msg)),
        TransactionError::BranchError(msg) => PyIOError::new_err(format!("Branch error: {}", msg)),
        _ => PyRuntimeError::new_err(e.to_string()),
    }
}

#[pyclass]
#[derive(Clone)]
struct PyTransactionInfo {
    #[pyo3(get)]
    tx_id: u64,
    #[pyo3(get)]
    epoch_id: u64,
    #[pyo3(get)]
    status: String,
    #[pyo3(get)]
    branch: String,
    #[pyo3(get)]
    started_at: i64,
    #[pyo3(get)]
    committed_at: Option<i64>,
    #[pyo3(get)]
    read_snapshot: HashMap<String, u64>,
    #[pyo3(get)]
    written_tables: Vec<String>,
}

impl From<TransactionRecord> for PyTransactionInfo {
    fn from(tx: TransactionRecord) -> Self {
        let written = tx.written_tables().into_iter().map(|s| s.to_string()).collect();
        Self {
            tx_id: tx.tx_id,
            epoch_id: tx.epoch_id,
            status: format!("{}", tx.status),
            branch: tx.branch,
            started_at: tx.started_at,
            committed_at: tx.committed_at,
            read_snapshot: tx.read_snapshot,
            written_tables: written,
        }
    }
}

#[pyclass]
#[derive(Clone)]
struct PyRecoveryReport {
    #[pyo3(get)]
    last_committed_epoch: Option<u64>,
    #[pyo3(get)]
    replayed: Vec<u64>,
    #[pyo3(get)]
    rolled_back: Vec<u64>,
    #[pyo3(get)]
    already_aborted: Vec<u64>,
    #[pyo3(get)]
    already_committed: Vec<u64>,
    #[pyo3(get)]
    warnings: Vec<String>,
    #[pyo3(get)]
    errors: Vec<String>,
    #[pyo3(get)]
    is_clean: bool,
}

impl From<RecoveryReport> for PyRecoveryReport {
    fn from(r: RecoveryReport) -> Self {
        let is_clean = r.is_clean();
        Self {
            last_committed_epoch: r.last_committed_epoch,
            replayed: r.replayed,
            rolled_back: r.rolled_back,
            already_aborted: r.already_aborted,
            already_committed: r.already_committed,
            warnings: r.warnings,
            errors: r.errors,
            is_clean,
        }
    }
}

// =============================================================================
// Changelog Types
// =============================================================================

/// A single table change within a commit.
#[pyclass]
#[derive(Clone)]
struct PyTableChange {
    #[pyo3(get)]
    table_name: String,
    #[pyo3(get)]
    old_version: Option<u64>,
    #[pyo3(get)]
    new_version: u64,
    #[pyo3(get)]
    chunk_hashes: Vec<String>,
}

impl From<&TableChange> for PyTableChange {
    fn from(tc: &TableChange) -> Self {
        Self {
            table_name: tc.table_name.clone(),
            old_version: tc.old_version,
            new_version: tc.new_version,
            chunk_hashes: tc.chunk_hashes.clone(),
        }
    }
}

#[pymethods]
impl PyTableChange {
    /// Check if this is a new table (no previous version).
    fn is_new_table(&self) -> bool {
        self.old_version.is_none()
    }

    fn __repr__(&self) -> String {
        format!(
            "PyTableChange(table={}, old_version={:?}, new_version={})",
            self.table_name, self.old_version, self.new_version
        )
    }
}

/// Entry in the changelog representing a committed transaction.
#[pyclass]
#[derive(Clone)]
struct PyChangelogEntry {
    #[pyo3(get)]
    tx_id: u64,
    #[pyo3(get)]
    epoch_id: u64,
    #[pyo3(get)]
    committed_at: i64,
    #[pyo3(get)]
    branch: String,
    changes: Vec<PyTableChange>,
}

impl From<ChangelogEntry> for PyChangelogEntry {
    fn from(entry: ChangelogEntry) -> Self {
        Self {
            tx_id: entry.tx_id,
            epoch_id: entry.epoch_id,
            committed_at: entry.committed_at,
            branch: entry.branch,
            changes: entry.changes.iter().map(PyTableChange::from).collect(),
        }
    }
}

#[pymethods]
impl PyChangelogEntry {
    /// Get list of table changes in this entry.
    #[getter]
    fn changes(&self) -> Vec<PyTableChange> {
        self.changes.clone()
    }

    /// Get list of changed table names.
    fn changed_tables(&self) -> Vec<String> {
        self.changes.iter().map(|c| c.table_name.clone()).collect()
    }

    /// Check if a specific table was changed.
    fn contains_table(&self, table_name: &str) -> bool {
        self.changes.iter().any(|c| c.table_name == table_name)
    }

    /// Get the change for a specific table, if present.
    fn get_change(&self, table_name: &str) -> Option<PyTableChange> {
        self.changes.iter()
            .find(|c| c.table_name == table_name)
            .cloned()
    }

    /// Number of tables changed in this entry.
    fn change_count(&self) -> usize {
        self.changes.len()
    }

    fn __repr__(&self) -> String {
        format!(
            "PyChangelogEntry(tx_id={}, branch={}, changes={})",
            self.tx_id, self.branch, self.changes.len()
        )
    }
}

// =============================================================================
// Merkle Tree Types
// =============================================================================

/// A leaf node in the Merkle tree - contains actual data
#[pyclass]
#[derive(Clone)]
struct PyDataChunk {
    #[pyo3(get)]
    hash: String,
    #[pyo3(get)]
    byte_range: (u64, u64),
    #[pyo3(get)]
    size: u64,
    #[pyo3(get)]
    index: usize,
}

impl From<&DataChunk> for PyDataChunk {
    fn from(chunk: &DataChunk) -> Self {
        Self {
            hash: chunk.hash.clone(),
            byte_range: chunk.byte_range,
            size: chunk.size,
            index: chunk.index,
        }
    }
}

#[pymethods]
impl PyDataChunk {
    fn __repr__(&self) -> String {
        format!(
            "PyDataChunk(hash={}..., range={:?}, size={})",
            &self.hash[..8.min(self.hash.len())],
            self.byte_range,
            self.size
        )
    }
}

/// Internal node in the Merkle tree
#[pyclass]
#[derive(Clone)]
struct PyMerkleNode {
    #[pyo3(get)]
    hash: String,
    #[pyo3(get)]
    children: Vec<String>,
    #[pyo3(get)]
    level: u32,
    #[pyo3(get)]
    index: usize,
}

impl From<&MerkleNode> for PyMerkleNode {
    fn from(node: &MerkleNode) -> Self {
        Self {
            hash: node.hash.clone(),
            children: node.children.clone(),
            level: node.level,
            index: node.index,
        }
    }
}

#[pymethods]
impl PyMerkleNode {
    fn __repr__(&self) -> String {
        format!(
            "PyMerkleNode(hash={}..., level={}, children={})",
            &self.hash[..8.min(self.hash.len())],
            self.level,
            self.children.len()
        )
    }
}

/// Complete Merkle tree for a data blob
#[pyclass]
#[derive(Clone)]
struct PyMerkleTree {
    #[pyo3(get)]
    root_hash: String,
    #[pyo3(get)]
    total_size: u64,
    #[pyo3(get)]
    chunk_size: usize,
    #[pyo3(get)]
    height: u32,
    chunks: Vec<PyDataChunk>,
    inner: MerkleTree,
}

impl From<MerkleTree> for PyMerkleTree {
    fn from(tree: MerkleTree) -> Self {
        let chunks: Vec<PyDataChunk> = tree.chunks.iter().map(PyDataChunk::from).collect();
        Self {
            root_hash: tree.root_hash.clone(),
            total_size: tree.total_size,
            chunk_size: tree.chunk_size,
            height: tree.height,
            chunks,
            inner: tree,
        }
    }
}

#[pymethods]
impl PyMerkleTree {
    /// Get all leaf chunks
    #[getter]
    fn chunks(&self) -> Vec<PyDataChunk> {
        self.chunks.clone()
    }

    /// Get number of chunks
    fn chunk_count(&self) -> usize {
        self.chunks.len()
    }

    /// Get all chunk hashes (for storage)
    fn chunk_hashes(&self) -> Vec<String> {
        self.inner.chunk_hashes()
    }

    /// Get the chunk containing a specific byte offset
    fn chunk_for_offset(&self, offset: u64) -> Option<PyDataChunk> {
        self.inner.chunk_for_offset(offset).map(PyDataChunk::from)
    }

    /// Get chunks that overlap with a byte range
    fn chunks_in_range(&self, start: u64, end: u64) -> Vec<PyDataChunk> {
        self.inner
            .chunks_in_range(start, end)
            .into_iter()
            .map(PyDataChunk::from)
            .collect()
    }

    fn __repr__(&self) -> String {
        format!(
            "PyMerkleTree(root={}..., chunks={}, size={}, height={})",
            &self.root_hash[..8.min(self.root_hash.len())],
            self.chunks.len(),
            self.total_size,
            self.height
        )
    }
}

/// Result of comparing two Merkle trees
#[pyclass]
#[derive(Clone)]
struct PyMerkleDiff {
    #[pyo3(get)]
    unchanged_chunks: Vec<String>,
    #[pyo3(get)]
    removed_chunks: Vec<String>,
    #[pyo3(get)]
    added_chunks: Vec<String>,
    #[pyo3(get)]
    reuse_ratio: f64,
}

impl From<MerkleDiff> for PyMerkleDiff {
    fn from(diff: MerkleDiff) -> Self {
        Self {
            unchanged_chunks: diff.unchanged_chunks,
            removed_chunks: diff.removed_chunks,
            added_chunks: diff.added_chunks,
            reuse_ratio: diff.reuse_ratio,
        }
    }
}

#[pymethods]
impl PyMerkleDiff {
    /// Number of chunks that didn't change
    fn unchanged_count(&self) -> usize {
        self.unchanged_chunks.len()
    }

    /// Number of new chunks added
    fn added_count(&self) -> usize {
        self.added_chunks.len()
    }

    /// Number of chunks removed
    fn removed_count(&self) -> usize {
        self.removed_chunks.len()
    }

    /// Reuse percentage (0-100)
    fn reuse_percentage(&self) -> f64 {
        self.reuse_ratio * 100.0
    }

    fn __repr__(&self) -> String {
        format!(
            "PyMerkleDiff(unchanged={}, added={}, removed={}, reuse={:.1}%)",
            self.unchanged_chunks.len(),
            self.added_chunks.len(),
            self.removed_chunks.len(),
            self.reuse_ratio * 100.0
        )
    }
}

/// Configuration for Merkle tree building
#[pyclass]
#[derive(Clone)]
struct PyMerkleConfig {
    inner: MerkleConfig,
}

#[pymethods]
impl PyMerkleConfig {
    /// Create a new Merkle config.
    ///
    /// Args:
    ///     chunk_size: Target chunk size in bytes (default: 64KB)
    ///     branching_factor: Tree branching factor (default: 2 for binary)
    #[new]
    #[pyo3(signature = (chunk_size=65536, branching_factor=2))]
    fn new(chunk_size: usize, branching_factor: usize) -> Self {
        Self {
            inner: MerkleConfig::new(chunk_size).with_branching_factor(branching_factor),
        }
    }

    #[getter]
    fn chunk_size(&self) -> usize {
        self.inner.chunk_size
    }

    #[getter]
    fn branching_factor(&self) -> usize {
        self.inner.branching_factor
    }

    fn __repr__(&self) -> String {
        format!(
            "PyMerkleConfig(chunk_size={}, branching_factor={})",
            self.inner.chunk_size, self.inner.branching_factor
        )
    }
}

/// Build a Merkle tree from data.
///
/// Args:
///     data: Raw bytes to build tree from
///     config: Optional MerkleConfig (uses defaults if not provided)
///
/// Returns:
///     PyMerkleTree with content-addressable structure
///
/// Example:
///     >>> config = PyMerkleConfig(chunk_size=1024)
///     >>> tree = merkle_build_tree(data, config)
///     >>> print(f"Root: {tree.root_hash}")
#[pyfunction]
#[pyo3(signature = (data, config=None))]
fn merkle_build_tree(data: &[u8], config: Option<PyMerkleConfig>) -> PyResult<PyMerkleTree> {
    let cfg = config.map(|c| c.inner).unwrap_or_default();
    build_tree(data, &cfg)
        .map(PyMerkleTree::from)
        .map_err(merkle_err_to_py)
}

/// Compare two Merkle trees and find differences.
///
/// Args:
///     old_tree: Previous version tree
///     new_tree: New version tree
///
/// Returns:
///     PyMerkleDiff with unchanged, added, and removed chunks
///
/// Example:
///     >>> diff = merkle_diff_trees(old_tree, new_tree)
///     >>> print(f"Reuse: {diff.reuse_percentage():.1f}%")
#[pyfunction]
fn merkle_diff_trees(old_tree: &PyMerkleTree, new_tree: &PyMerkleTree) -> PyMerkleDiff {
    PyMerkleDiff::from(diff_trees(&old_tree.inner, &new_tree.inner))
}

/// Verify integrity of a Merkle tree.
///
/// This function verifies that all chunk hashes match their actual data
/// and that the tree structure is consistent.
///
/// Args:
///     tree: The Merkle tree to verify
///     chunk_store: A PyChunkStore to retrieve chunk data
///
/// Returns:
///     True if verification passes
///
/// Raises:
///     ValueError: If integrity check fails
#[pyfunction]
fn merkle_verify_tree(tree: &PyMerkleTree, chunk_store: &PyChunkStore) -> PyResult<bool> {
    verify_tree(&tree.inner, |hash| {
        chunk_store.inner.get(hash).ok()
    }).map_err(merkle_err_to_py)
}

// =============================================================================
// Transaction Manager
// =============================================================================

#[pyclass]
struct PyTransactionManager {
    inner: Arc<TransactionManager>,
}

#[pymethods]
impl PyTransactionManager {
    /// Create a new TransactionManager.
    ///
    /// Args:
    ///     base_path: Path for transaction storage
    ///     catalog_path: Path to catalog directory
    ///     branch_path: Optional path to branch manager directory
    ///     auto_recover: If True, run recovery on startup (default: False)
    #[new]
    #[pyo3(signature = (base_path, catalog_path, branch_path=None, auto_recover=false))]
    fn new(
        base_path: &str,
        catalog_path: &str,
        branch_path: Option<&str>,
        auto_recover: bool,
    ) -> PyResult<Self> {
        let catalog = Arc::new(FileCatalog::new(catalog_path).map_err(catalog_err_to_py)?);
        let branch_manager = match branch_path {
            Some(p) => Some(Arc::new(BranchManager::new(p).map_err(branch_err_to_py)?)),
            None => None,
        };

        let inner = TransactionManager::new(base_path, catalog, branch_manager)
            .map_err(tx_err_to_py)?;

        // Optionally run recovery on startup
        if auto_recover {
            inner.recover_and_apply().map_err(tx_err_to_py)?;
        }

        Ok(Self { inner: Arc::new(inner) })
    }

    /// Begin a new transaction.
    ///
    /// Args:
    ///     branch: Optional branch name (default: current branch)
    ///
    /// Returns:
    ///     Transaction ID
    #[pyo3(signature = (branch=None))]
    fn begin(&self, branch: Option<&str>) -> PyResult<u64> {
        self.inner.begin(branch).map_err(tx_err_to_py)
    }

    /// Add a write to a transaction.
    ///
    /// Args:
    ///     tx_id: Transaction ID
    ///     table_name: Table being written
    ///     new_version: New version number
    ///     chunk_hashes: List of chunk hashes
    fn add_write(
        &self,
        tx_id: u64,
        table_name: &str,
        new_version: u64,
        chunk_hashes: Vec<String>,
    ) -> PyResult<()> {
        let write = TableWrite::new(table_name, new_version, chunk_hashes);
        self.inner.add_write(tx_id, write).map_err(tx_err_to_py)
    }

    /// Record a read for conflict detection.
    ///
    /// Args:
    ///     tx_id: Transaction ID
    ///     table_name: Table being read
    ///     version: Version being read
    fn record_read(
        &self,
        tx_id: u64,
        table_name: &str,
        version: u64,
    ) -> PyResult<()> {
        self.inner.record_read(tx_id, table_name, version).map_err(tx_err_to_py)
    }

    /// Commit a transaction.
    ///
    /// Args:
    ///     tx_id: Transaction ID
    ///
    /// Raises:
    ///     ValueError: If conflict detected or transaction not active
    fn commit(&self, tx_id: u64) -> PyResult<()> {
        self.inner.commit(tx_id).map_err(tx_err_to_py)
    }

    /// Abort a transaction.
    ///
    /// Args:
    ///     tx_id: Transaction ID
    ///     reason: Reason for abort
    #[pyo3(signature = (tx_id, reason="User requested"))]
    fn abort(&self, tx_id: u64, reason: &str) -> PyResult<()> {
        self.inner.abort(tx_id, reason).map_err(tx_err_to_py)
    }

    /// Get transaction information.
    ///
    /// Args:
    ///     tx_id: Transaction ID
    ///
    /// Returns:
    ///     PyTransactionInfo with transaction details
    fn get_transaction(&self, tx_id: u64) -> PyResult<PyTransactionInfo> {
        self.inner
            .get_transaction(tx_id)
            .map(|tx| tx.into())
            .map_err(tx_err_to_py)
    }

    /// Get all active transactions.
    ///
    /// Returns:
    ///     List of PyTransactionInfo
    fn active_transactions(&self) -> PyResult<Vec<PyTransactionInfo>> {
        self.inner
            .active_transactions()
            .map(|txs| txs.into_iter().map(|tx| tx.into()).collect())
            .map_err(tx_err_to_py)
    }

    /// Get count of active transactions.
    fn active_count(&self) -> PyResult<usize> {
        self.inner.active_count().map_err(tx_err_to_py)
    }

    /// Perform recovery after crash/restart (read-only).
    ///
    /// Scans the transaction log to identify:
    /// - Committed transactions (preserved)
    /// - Pending transactions (will be marked for rollback)
    /// - Any inconsistencies
    ///
    /// This is read-only; use recover_and_apply() to mark pending
    /// transactions as aborted.
    ///
    /// Returns:
    ///     PyRecoveryReport with recovery details
    fn recover(&self) -> PyResult<PyRecoveryReport> {
        self.inner
            .recover()
            .map(|r| r.into())
            .map_err(tx_err_to_py)
    }

    /// Perform recovery and apply rollbacks.
    ///
    /// Like recover(), but also marks pending transactions as aborted.
    /// Call this on startup to ensure clean state.
    ///
    /// Returns:
    ///     PyRecoveryReport with recovery details including applied rollbacks
    fn recover_and_apply(&self) -> PyResult<PyRecoveryReport> {
        self.inner
            .recover_and_apply()
            .map(|r| r.into())
            .map_err(tx_err_to_py)
    }

    /// Verify consistency of the transaction system.
    ///
    /// Returns a list of any issues found. Empty list means consistent.
    ///
    /// Returns:
    ///     List of issue descriptions (empty if consistent)
    fn verify_consistency(&self) -> PyResult<Vec<String>> {
        self.inner
            .verify_consistency()
            .map_err(tx_err_to_py)
    }

    // =========================================================================
    // Changelog Methods
    // =========================================================================

    /// Get changelog entries matching query criteria.
    ///
    /// This is the API for querying committed changes, enabling the
    /// unified batch/stream model:
    /// - Batch: "What is the state?" (use QueryEngine.query())
    /// - Stream: "What changed?" (use this method)
    ///
    /// Args:
    ///     since_tx_id: Start from this transaction (exclusive)
    ///     since_timestamp: Start from this Unix timestamp
    ///     tables: Filter to specific tables
    ///     branch: Filter to specific branch
    ///     limit: Maximum entries to return
    ///
    /// Returns:
    ///     List of PyChangelogEntry objects
    #[pyo3(signature = (since_tx_id=None, since_timestamp=None, tables=None, branch=None, limit=None))]
    fn get_changelog(
        &self,
        since_tx_id: Option<u64>,
        since_timestamp: Option<i64>,
        tables: Option<Vec<String>>,
        branch: Option<String>,
        limit: Option<usize>,
    ) -> PyResult<Vec<PyChangelogEntry>> {
        // Build query
        let mut query = ChangelogQuery::new();
        if let Some(tx_id) = since_tx_id {
            query = query.since_tx(tx_id);
        }
        if let Some(ts) = since_timestamp {
            query = query.since_time(ts);
        }
        if let Some(t) = tables {
            query = query.for_tables(t);
        }
        if let Some(b) = branch {
            query = query.on_branch(&b);
        }
        if let Some(l) = limit {
            query = query.with_limit(l);
        }

        // Execute query
        let entries = self.inner.get_changelog(query).map_err(tx_err_to_py)?;

        Ok(entries.into_iter().map(PyChangelogEntry::from).collect())
    }

    /// Get the latest committed transaction ID.
    ///
    /// Returns None if no transactions have been committed yet.
    ///
    /// Returns:
    ///     Latest transaction ID or None
    fn latest_tx_id(&self) -> PyResult<Option<u64>> {
        self.inner.latest_tx_id().map_err(tx_err_to_py)
    }
}

#[pymodule]
fn armillaria(m: &Bound<'_, PyModule>) -> PyResult<()> {
    // Core storage
    m.add_class::<PyChunkStore>()?;
    m.add_class::<PyTableVersion>()?;
    m.add_class::<PyCatalog>()?;

    // Branching
    m.add_class::<PyBranch>()?;
    m.add_class::<PyBranchDiff>()?;
    m.add_class::<PyBranchManager>()?;

    // Transactions
    m.add_class::<PyTransactionManager>()?;
    m.add_class::<PyTransactionInfo>()?;
    m.add_class::<PyRecoveryReport>()?;

    // Changelog
    m.add_class::<PyTableChange>()?;
    m.add_class::<PyChangelogEntry>()?;

    // Merkle Tree
    m.add_class::<PyDataChunk>()?;
    m.add_class::<PyMerkleNode>()?;
    m.add_class::<PyMerkleTree>()?;
    m.add_class::<PyMerkleDiff>()?;
    m.add_class::<PyMerkleConfig>()?;
    m.add_function(wrap_pyfunction!(merkle_build_tree, m)?)?;
    m.add_function(wrap_pyfunction!(merkle_diff_trees, m)?)?;
    m.add_function(wrap_pyfunction!(merkle_verify_tree, m)?)?;

    Ok(())
}
