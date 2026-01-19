use std::collections::HashMap;
use std::str::FromStr;
use std::sync::Arc;

use pyo3::prelude::*;
use pyo3::exceptions::{PyIOError, PyValueError, PyRuntimeError};
use rhizo_core::{
    ChunkStore, ChunkStoreError,
    FileCatalog, CatalogError, TableVersion,
    Branch, BranchDiff, BranchError, BranchManager,
    MergeAnalysis, MergeAnalyzer, MergeOutcome,
    TransactionManager, TransactionRecord, TransactionError,
    TableWrite, RecoveryReport,
    ChangelogEntry, TableChange, ChangelogQuery,
    MerkleTree, MerkleNode, DataChunk, MerkleDiff, MerkleConfig, MerkleError,
    build_tree, diff_trees, verify_tree,
    ParquetEncoder, ParquetDecoder, ParquetCompression, ParquetError,
    FilterOp, ScalarValue, PredicateFilter,
    // Algebraic types
    OpType, AlgebraicValue, AlgebraicMerger, MergeResult,
    TableAlgebraicSchema, AlgebraicSchemaRegistry,
};

// Phase 4: pyo3-arrow for zero-copy Arrow FFI
use pyo3_arrow::PyRecordBatch;

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
        BranchError::AlgebraicConflict(tables) => {
            PyValueError::new_err(format!("Algebraic merge conflict on tables: {:?}", tables))
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

/// Convert ParquetError to appropriate Python exception
fn parquet_err_to_py(e: ParquetError) -> PyErr {
    match e {
        ParquetError::Arrow(e) => PyValueError::new_err(format!("Arrow error: {}", e)),
        ParquetError::Parquet(e) => PyValueError::new_err(format!("Parquet error: {}", e)),
        ParquetError::EmptyData => PyValueError::new_err("Cannot process empty data"),
        ParquetError::InvalidCompression(c) => {
            PyValueError::new_err(format!("Invalid compression: {}", c))
        }
        ParquetError::InvalidColumn(msg) => {
            PyValueError::new_err(format!("Invalid column: {}", msg))
        }
    }
}

// =============================================================================
// Phase 4: Native Parquet Encoder/Decoder with Zero-Copy Arrow FFI
// =============================================================================

/// High-performance Parquet encoder using Rust's parquet crate.
///
/// Provides zero-copy Arrow data transfer from Python via pyo3-arrow,
/// and parallel encoding of multiple batches using Rayon.
#[pyclass]
struct PyParquetEncoder {
    inner: ParquetEncoder,
}

#[pymethods]
impl PyParquetEncoder {
    /// Create a new encoder.
    ///
    /// Args:
    ///     compression: Compression type ("zstd", "snappy", "gzip", "lz4", "none")
    ///                  Defaults to "zstd" for best compression/speed balance.
    #[new]
    #[pyo3(signature = (compression = None))]
    fn new(compression: Option<&str>) -> PyResult<Self> {
        let compression = match compression {
            Some(c) => ParquetCompression::from_str(c).map_err(parquet_err_to_py)?,
            None => ParquetCompression::Zstd,
        };
        Ok(Self {
            inner: ParquetEncoder::with_compression(compression),
        })
    }

    /// Encode an Arrow RecordBatch to Parquet bytes.
    ///
    /// Args:
    ///     batch: PyArrow RecordBatch to encode (zero-copy transfer)
    ///
    /// Returns:
    ///     bytes: Parquet-encoded data
    fn encode(&self, batch: PyRecordBatch) -> PyResult<Vec<u8>> {
        let rust_batch = batch.into_inner();
        self.inner.encode(&rust_batch).map_err(parquet_err_to_py)
    }

    /// Encode multiple Arrow RecordBatches in parallel.
    ///
    /// Uses Rayon for parallel encoding, significantly faster than
    /// encoding batches sequentially.
    ///
    /// Args:
    ///     batches: List of PyArrow RecordBatches to encode
    ///
    /// Returns:
    ///     List[bytes]: Parquet-encoded data for each batch
    fn encode_batch(&self, batches: Vec<PyRecordBatch>) -> PyResult<Vec<Vec<u8>>> {
        let rust_batches: Vec<_> = batches.into_iter().map(|b| b.into_inner()).collect();
        self.inner
            .encode_batch(&rust_batches)
            .map_err(parquet_err_to_py)
    }
}

/// High-performance Parquet decoder using Rust's parquet crate.
///
/// Provides zero-copy Arrow data transfer to Python via pyo3-arrow,
/// and parallel decoding of multiple chunks using Rayon.
#[pyclass]
struct PyParquetDecoder {
    inner: ParquetDecoder,
}

#[pymethods]
impl PyParquetDecoder {
    /// Create a new decoder.
    #[new]
    fn new() -> Self {
        Self {
            inner: ParquetDecoder::new(),
        }
    }

    /// Decode Parquet bytes to an Arrow RecordBatch.
    ///
    /// Args:
    ///     data: Parquet file bytes
    ///
    /// Returns:
    ///     PyArrow RecordBatch (zero-copy transfer)
    fn decode<'py>(&self, py: Python<'py>, data: &[u8]) -> PyResult<Bound<'py, PyAny>> {
        let batch = self.inner.decode(data).map_err(parquet_err_to_py)?;
        PyRecordBatch::new(batch).into_pyarrow(py)
    }

    /// Decode multiple Parquet chunks in parallel.
    ///
    /// Uses Rayon for parallel decoding, significantly faster than
    /// decoding chunks sequentially.
    ///
    /// Args:
    ///     chunks: List of Parquet byte arrays to decode
    ///
    /// Returns:
    ///     List[RecordBatch]: PyArrow RecordBatches for each chunk
    fn decode_batch<'py>(
        &self,
        py: Python<'py>,
        chunks: Vec<Vec<u8>>,
    ) -> PyResult<Vec<Bound<'py, PyAny>>> {
        let batches = self
            .inner
            .decode_batch_owned(&chunks)
            .map_err(parquet_err_to_py)?;

        batches
            .into_iter()
            .map(|batch| PyRecordBatch::new(batch).into_pyarrow(py))
            .collect()
    }

    /// Decode only specific columns by index (projection pushdown).
    ///
    /// This is significantly faster when you only need a subset of columns.
    /// Column indices are 0-based and refer to the schema order.
    ///
    /// Mathematical Model:
    ///     Speedup ≈ n/k where n=total columns, k=requested columns
    ///     Example: 10 columns, query 2 → ~5x speedup on decode phase
    ///
    /// Args:
    ///     data: Parquet file bytes
    ///     column_indices: List of 0-based column indices to decode
    ///
    /// Returns:
    ///     PyArrow RecordBatch with only requested columns
    fn decode_columns<'py>(
        &self,
        py: Python<'py>,
        data: &[u8],
        column_indices: Vec<usize>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let batch = self
            .inner
            .decode_columns(data, &column_indices)
            .map_err(parquet_err_to_py)?;
        PyRecordBatch::new(batch).into_pyarrow(py)
    }

    /// Decode only specific columns by name (projection pushdown).
    ///
    /// Convenience method that resolves column names to indices and applies
    /// projection pushdown.
    ///
    /// Args:
    ///     data: Parquet file bytes
    ///     column_names: List of column names to decode
    ///
    /// Returns:
    ///     PyArrow RecordBatch with only requested columns
    ///
    /// Raises:
    ///     ValueError: If a column name is not found in schema
    fn decode_columns_by_name<'py>(
        &self,
        py: Python<'py>,
        data: &[u8],
        column_names: Vec<String>,
    ) -> PyResult<Bound<'py, PyAny>> {
        // Convert Vec<String> to Vec<&str> for the Rust API
        let names: Vec<&str> = column_names.iter().map(|s| s.as_str()).collect();
        let batch = self
            .inner
            .decode_columns_by_name(data, &names)
            .map_err(parquet_err_to_py)?;
        PyRecordBatch::new(batch).into_pyarrow(py)
    }

    /// Decode with predicate pushdown (row-level filtering).
    ///
    /// This method applies filter predicates during decoding, reducing the
    /// amount of data that needs to be processed for selective queries.
    ///
    /// Mathematical Model:
    ///     For selectivity `s` (fraction of rows matching):
    ///       - Row-level filtering reduces output by factor of `s`
    ///       - Combined with projection: Speedup ≈ (n/k) × (1/s)
    ///     Example: 10 columns, query 2, 1% selectivity → up to 500x speedup
    ///
    /// Args:
    ///     data: Parquet file bytes
    ///     filters: List of PyPredicateFilter objects
    ///     column_indices: Optional list of column indices to project
    ///
    /// Returns:
    ///     PyArrow RecordBatch with filters applied
    ///
    /// Example:
    ///     >>> decoder = PyParquetDecoder()
    ///     >>> filter = PyPredicateFilter("age", "gt", 50)
    ///     >>> result = decoder.decode_with_filter(data, [filter])
    #[pyo3(signature = (data, filters, column_indices=None))]
    fn decode_with_filter<'py>(
        &self,
        py: Python<'py>,
        data: &[u8],
        filters: Vec<PyPredicateFilter>,
        column_indices: Option<Vec<usize>>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let rust_filters: Vec<PredicateFilter> = filters
            .into_iter()
            .map(|f| f.into_inner())
            .collect();

        let batch = self
            .inner
            .decode_with_filter(
                data,
                &rust_filters,
                column_indices.as_deref(),
            )
            .map_err(parquet_err_to_py)?;
        PyRecordBatch::new(batch).into_pyarrow(py)
    }

    /// Get row-group pruning statistics for a filtered decode.
    ///
    /// This is useful for debugging and understanding pruning effectiveness.
    /// Row-group pruning uses min/max statistics to skip entire row groups
    /// that cannot contain matching rows, before doing any decoding.
    ///
    /// Args:
    ///     data: Parquet file bytes
    ///     filters: List of PyPredicateFilter objects
    ///
    /// Returns:
    ///     Tuple of (total_row_groups, pruned_row_groups, kept_row_groups)
    ///
    /// Example:
    ///     >>> decoder = PyParquetDecoder()
    ///     >>> filter = PyPredicateFilter("id", "gt", 9000)
    ///     >>> total, pruned, kept = decoder.get_pruning_stats(data, [filter])
    ///     >>> print(f"Pruned {pruned}/{total} row groups ({100*pruned/total:.1f}%)")
    fn get_pruning_stats(
        &self,
        data: &[u8],
        filters: Vec<PyPredicateFilter>,
    ) -> PyResult<(usize, usize, usize)> {
        let rust_filters: Vec<PredicateFilter> = filters
            .into_iter()
            .map(|f| f.into_inner())
            .collect();

        self.inner
            .get_pruning_stats(data, &rust_filters)
            .map_err(parquet_err_to_py)
    }
}

// =============================================================================
// Phase R.2: Predicate Pushdown Types
// =============================================================================

/// Comparison operations for filter predicates.
///
/// These map to SQL-style comparisons:
///   - "eq"  → column = value
///   - "ne"  → column != value
///   - "lt"  → column < value
///   - "le"  → column <= value
///   - "gt"  → column > value
///   - "ge"  → column >= value
#[pyclass]
#[derive(Clone)]
struct PyFilterOp {
    inner: FilterOp,
}

#[pymethods]
impl PyFilterOp {
    /// Create a filter operation from a string.
    ///
    /// Args:
    ///     op: One of "eq", "ne", "lt", "le", "gt", "ge"
    #[new]
    fn new(op: &str) -> PyResult<Self> {
        let inner = match op.to_lowercase().as_str() {
            "eq" | "=" | "==" => FilterOp::Eq,
            "ne" | "!=" | "<>" => FilterOp::Ne,
            "lt" | "<" => FilterOp::Lt,
            "le" | "<=" => FilterOp::Le,
            "gt" | ">" => FilterOp::Gt,
            "ge" | ">=" => FilterOp::Ge,
            _ => return Err(PyValueError::new_err(format!(
                "Invalid filter operation: '{}'. Use eq, ne, lt, le, gt, or ge",
                op
            ))),
        };
        Ok(Self { inner })
    }

    fn __repr__(&self) -> String {
        format!("PyFilterOp({})", self.inner)
    }

    fn __str__(&self) -> String {
        format!("{}", self.inner)
    }
}

/// Scalar values for filter predicates.
///
/// Supports common types used in analytical queries:
///   - int: Integer values (64-bit)
///   - float: Floating-point values (64-bit)
///   - str: UTF-8 strings
///   - bool: Boolean values
#[pyclass]
#[derive(Clone)]
struct PyScalarValue {
    inner: ScalarValue,
}

#[pymethods]
impl PyScalarValue {
    /// Create a scalar value.
    ///
    /// The type is inferred from the Python value:
    ///   - int → Int64
    ///   - float → Float64
    ///   - str → Utf8
    ///   - bool → Boolean
    ///   - None → Null
    #[new]
    fn new(value: &Bound<'_, PyAny>) -> PyResult<Self> {
        let inner = if value.is_none() {
            ScalarValue::Null
        } else if let Ok(v) = value.extract::<bool>() {
            ScalarValue::Boolean(v)
        } else if let Ok(v) = value.extract::<i64>() {
            ScalarValue::Int64(v)
        } else if let Ok(v) = value.extract::<f64>() {
            ScalarValue::Float64(v)
        } else if let Ok(v) = value.extract::<String>() {
            ScalarValue::Utf8(v)
        } else {
            return Err(PyValueError::new_err(
                "Unsupported scalar type. Use int, float, str, bool, or None"
            ));
        };
        Ok(Self { inner })
    }

    fn __repr__(&self) -> String {
        format!("PyScalarValue({})", self.inner)
    }

    fn __str__(&self) -> String {
        format!("{}", self.inner)
    }
}

/// A predicate filter for Parquet data.
///
/// Represents a simple comparison: column <op> value
///
/// Example:
///     >>> # age > 50
///     >>> filter = PyPredicateFilter("age", "gt", 50)
///     >>> # status = 'active'
///     >>> filter = PyPredicateFilter("status", "eq", "active")
#[pyclass]
#[derive(Clone)]
struct PyPredicateFilter {
    inner: PredicateFilter,
}

#[pymethods]
impl PyPredicateFilter {
    /// Create a predicate filter.
    ///
    /// Args:
    ///     column: Column name to filter on
    ///     op: Comparison operation (eq, ne, lt, le, gt, ge)
    ///     value: Value to compare against (int, float, str, bool, or None)
    #[new]
    fn new(column: String, op: &str, value: &Bound<'_, PyAny>) -> PyResult<Self> {
        let filter_op = PyFilterOp::new(op)?;
        let scalar_value = PyScalarValue::new(value)?;

        Ok(Self {
            inner: PredicateFilter::new(column, filter_op.inner, scalar_value.inner),
        })
    }

    #[getter]
    fn column(&self) -> String {
        self.inner.column.clone()
    }

    #[getter]
    fn op(&self) -> String {
        format!("{}", self.inner.op)
    }

    #[getter]
    fn value(&self) -> String {
        format!("{}", self.inner.value)
    }

    fn __repr__(&self) -> String {
        format!("PyPredicateFilter({})", self.inner)
    }

    fn __str__(&self) -> String {
        format!("{}", self.inner)
    }
}

impl PyPredicateFilter {
    fn into_inner(self) -> PredicateFilter {
        self.inner
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

// =============================================================================
// Algebraic Classification Types
// =============================================================================

/// Algebraic operation classification.
///
/// Operations classified as conflict-free can be automatically merged
/// without requiring manual conflict resolution.
///
/// Conflict-free types:
///   - SemilatticeMax: max(a, b) - last-writer-wins for timestamps
///   - SemilatticeMin: min(a, b) - first-writer-wins
///   - SemilatticeUnion: set union - add-only sets
///   - SemilatticeIntersect: set intersection
///   - AbelianAdd: a + b - counters, deltas
///   - AbelianMultiply: a * b - scaling factors
///
/// Conflicting types:
///   - GenericOverwrite: may conflict
///   - GenericConditional: always conflicts
///   - Unknown: conservative fallback
#[pyclass]
#[derive(Clone)]
struct PyOpType {
    inner: OpType,
}

#[pymethods]
impl PyOpType {
    /// Create an operation type from a string.
    ///
    /// Valid values: "max", "min", "union", "intersect", "add", "multiply",
    ///               "overwrite", "conditional", "unknown"
    #[new]
    fn new(op_type: &str) -> PyResult<Self> {
        let inner = match op_type.to_lowercase().as_str() {
            "max" | "semilattice_max" => OpType::SemilatticeMax,
            "min" | "semilattice_min" => OpType::SemilatticeMin,
            "union" | "semilattice_union" => OpType::SemilatticeUnion,
            "intersect" | "semilattice_intersect" => OpType::SemilatticeIntersect,
            "add" | "abelian_add" => OpType::AbelianAdd,
            "multiply" | "abelian_multiply" => OpType::AbelianMultiply,
            "overwrite" | "generic_overwrite" => OpType::GenericOverwrite,
            "conditional" | "generic_conditional" => OpType::GenericConditional,
            "unknown" => OpType::Unknown,
            _ => return Err(PyValueError::new_err(format!(
                "Invalid operation type: '{}'. Valid: max, min, union, intersect, add, multiply, overwrite, conditional, unknown",
                op_type
            ))),
        };
        Ok(Self { inner })
    }

    /// Check if this operation type is conflict-free.
    fn is_conflict_free(&self) -> bool {
        self.inner.is_conflict_free()
    }

    /// Check if this is a semilattice operation.
    fn is_semilattice(&self) -> bool {
        self.inner.is_semilattice()
    }

    /// Check if this is an Abelian (group) operation.
    fn is_abelian(&self) -> bool {
        self.inner.is_abelian()
    }

    /// Check if this can be merged with another operation type.
    fn can_merge_with(&self, other: &PyOpType) -> bool {
        self.inner.can_merge_with(&other.inner)
    }

    /// Get a description of this operation type.
    fn description(&self) -> &'static str {
        self.inner.description()
    }

    fn __repr__(&self) -> String {
        format!("PyOpType({})", self.inner)
    }

    fn __str__(&self) -> String {
        format!("{}", self.inner)
    }
}

/// A value that can be algebraically merged.
#[pyclass]
#[derive(Clone)]
struct PyAlgebraicValue {
    inner: AlgebraicValue,
}

#[pymethods]
impl PyAlgebraicValue {
    /// Create an algebraic value from a Python value.
    ///
    /// Type inference:
    ///   - int → Integer
    ///   - float → Float
    ///   - bool → Boolean
    ///   - list/set of str → StringSet
    ///   - list/set of int → IntSet
    ///   - None → Null
    #[new]
    fn new(value: &Bound<'_, PyAny>) -> PyResult<Self> {
        let inner = if value.is_none() {
            AlgebraicValue::Null
        } else if let Ok(v) = value.extract::<bool>() {
            AlgebraicValue::Boolean(v)
        } else if let Ok(v) = value.extract::<i64>() {
            AlgebraicValue::Integer(v)
        } else if let Ok(v) = value.extract::<f64>() {
            AlgebraicValue::Float(v)
        } else if let Ok(v) = value.extract::<std::collections::HashSet<String>>() {
            AlgebraicValue::StringSet(v)
        } else if let Ok(v) = value.extract::<std::collections::HashSet<i64>>() {
            AlgebraicValue::IntSet(v)
        } else if let Ok(v) = value.extract::<Vec<String>>() {
            AlgebraicValue::StringSet(v.into_iter().collect())
        } else if let Ok(v) = value.extract::<Vec<i64>>() {
            AlgebraicValue::IntSet(v.into_iter().collect())
        } else {
            return Err(PyValueError::new_err(
                "Unsupported value type. Use int, float, bool, set/list of str, set/list of int, or None"
            ));
        };
        Ok(Self { inner })
    }

    /// Create an integer value.
    #[staticmethod]
    fn integer(v: i64) -> Self {
        Self { inner: AlgebraicValue::Integer(v) }
    }

    /// Create a float value.
    #[staticmethod]
    fn float(v: f64) -> Self {
        Self { inner: AlgebraicValue::Float(v) }
    }

    /// Create a string set value.
    #[staticmethod]
    fn string_set(values: Vec<String>) -> Self {
        Self { inner: AlgebraicValue::string_set(values) }
    }

    /// Create an integer set value.
    #[staticmethod]
    fn int_set(values: Vec<i64>) -> Self {
        Self { inner: AlgebraicValue::int_set(values) }
    }

    /// Create a boolean value.
    #[staticmethod]
    fn boolean(v: bool) -> Self {
        Self { inner: AlgebraicValue::Boolean(v) }
    }

    /// Create a null value.
    #[staticmethod]
    fn null() -> Self {
        Self { inner: AlgebraicValue::Null }
    }

    /// Check if this is a numeric type.
    fn is_numeric(&self) -> bool {
        self.inner.is_numeric()
    }

    /// Check if this is a set type.
    fn is_set(&self) -> bool {
        self.inner.is_set()
    }

    /// Check if this is null.
    fn is_null(&self) -> bool {
        self.inner.is_null()
    }

    /// Get the type name.
    fn type_name(&self) -> &'static str {
        self.inner.type_name()
    }

    fn __repr__(&self) -> String {
        format!("PyAlgebraicValue({})", self.inner)
    }

    fn __str__(&self) -> String {
        format!("{}", self.inner)
    }
}

/// Merge two algebraic values.
///
/// Args:
///     op_type: The operation type to use for merging
///     value1: First value
///     value2: Second value
///
/// Returns:
///     Merged value if successful
///
/// Raises:
///     ValueError: If merge fails (conflict or type mismatch)
#[pyfunction]
fn algebraic_merge(
    op_type: &PyOpType,
    value1: &PyAlgebraicValue,
    value2: &PyAlgebraicValue,
) -> PyResult<PyAlgebraicValue> {
    match AlgebraicMerger::merge(op_type.inner, &value1.inner, &value2.inner) {
        MergeResult::Merged(v) => Ok(PyAlgebraicValue { inner: v }),
        MergeResult::Conflict { reason, .. } => {
            Err(PyValueError::new_err(format!("Merge conflict: {}", reason)))
        }
        MergeResult::TypeMismatch { type1, type2, operation } => {
            Err(PyValueError::new_err(format!(
                "Type mismatch: cannot merge {} with {} using {:?}",
                type1, type2, operation
            )))
        }
    }
}

/// Schema-level algebraic configuration for a table.
#[pyclass]
#[derive(Clone)]
struct PyTableAlgebraicSchema {
    inner: TableAlgebraicSchema,
}

#[pymethods]
impl PyTableAlgebraicSchema {
    /// Create a new schema for a table.
    ///
    /// Args:
    ///     table: Table name
    ///     default_op_type: Default operation type for unannotated columns
    #[new]
    #[pyo3(signature = (table, default_op_type=None))]
    fn new(table: &str, default_op_type: Option<&PyOpType>) -> Self {
        let mut inner = TableAlgebraicSchema::new(table);
        if let Some(op) = default_op_type {
            inner.set_default(op.inner);
        }
        Self { inner }
    }

    /// Create a schema where all columns use additive merge.
    #[staticmethod]
    fn all_additive(table: &str) -> Self {
        Self { inner: TableAlgebraicSchema::all_additive(table) }
    }

    /// Create a schema where all columns use max merge.
    #[staticmethod]
    fn all_max(table: &str) -> Self {
        Self { inner: TableAlgebraicSchema::all_max(table) }
    }

    /// Add a column with the specified operation type.
    fn add_column(&mut self, column: &str, op_type: &PyOpType) {
        self.inner.add_column(column, op_type.inner);
    }

    /// Get the operation type for a column.
    fn get_op_type(&self, column: &str) -> PyOpType {
        PyOpType { inner: self.inner.get_op_type(column) }
    }

    /// Check if all columns are conflict-free.
    fn is_fully_conflict_free(&self) -> bool {
        self.inner.is_fully_conflict_free()
    }

    /// Get list of conflict-free columns.
    fn conflict_free_columns(&self) -> Vec<String> {
        self.inner.conflict_free_columns().into_iter().map(|s| s.to_string()).collect()
    }

    /// Get list of conflicting columns.
    fn conflicting_columns(&self) -> Vec<String> {
        self.inner.conflicting_columns().into_iter().map(|s| s.to_string()).collect()
    }

    /// Check if writes to these columns can be auto-merged.
    fn can_auto_merge(&self, columns: Vec<String>) -> bool {
        let refs: Vec<&str> = columns.iter().map(|s| s.as_str()).collect();
        self.inner.can_auto_merge(&refs)
    }

    #[getter]
    fn table(&self) -> String {
        self.inner.table.clone()
    }

    fn __repr__(&self) -> String {
        format!(
            "PyTableAlgebraicSchema(table={}, columns={})",
            self.inner.table,
            self.inner.columns.len()
        )
    }
}

/// Registry for table algebraic schemas.
#[pyclass]
struct PyAlgebraicSchemaRegistry {
    inner: AlgebraicSchemaRegistry,
}

#[pymethods]
impl PyAlgebraicSchemaRegistry {
    /// Create an empty registry.
    #[new]
    fn new() -> Self {
        Self { inner: AlgebraicSchemaRegistry::new() }
    }

    /// Register a schema for a table.
    fn register(&mut self, schema: PyTableAlgebraicSchema) {
        self.inner.register(schema.inner);
    }

    /// Get the schema for a table.
    fn get(&self, table: &str) -> Option<PyTableAlgebraicSchema> {
        self.inner.get(table).map(|s| PyTableAlgebraicSchema { inner: s.clone() })
    }

    /// Get the operation type for a table/column.
    fn get_op_type(&self, table: &str, column: &str) -> PyOpType {
        PyOpType { inner: self.inner.get_op_type(table, column) }
    }

    /// Check if a table is registered.
    fn has_table(&self, table: &str) -> bool {
        self.inner.has_table(table)
    }

    /// Get all registered table names.
    fn tables(&self) -> Vec<String> {
        self.inner.tables().into_iter().map(|s| s.to_string()).collect()
    }

    /// Remove a schema from the registry.
    fn unregister(&mut self, table: &str) -> Option<PyTableAlgebraicSchema> {
        self.inner.unregister(table).map(|s| PyTableAlgebraicSchema { inner: s })
    }

    fn __repr__(&self) -> String {
        format!("PyAlgebraicSchemaRegistry(tables={})", self.inner.tables().len())
    }
}

/// Result of analyzing merge compatibility.
#[pyclass]
#[derive(Clone)]
struct PyMergeAnalysis {
    #[pyo3(get)]
    auto_mergeable: Vec<String>,
    #[pyo3(get)]
    conflicting: Vec<String>,
    #[pyo3(get)]
    source_only: Vec<String>,
    #[pyo3(get)]
    target_only: Vec<String>,
    #[pyo3(get)]
    unchanged: Vec<String>,
}

impl From<MergeAnalysis> for PyMergeAnalysis {
    fn from(a: MergeAnalysis) -> Self {
        Self {
            auto_mergeable: a.auto_mergeable,
            conflicting: a.conflicting,
            source_only: a.source_only,
            target_only: a.target_only,
            unchanged: a.unchanged,
        }
    }
}

#[pymethods]
impl PyMergeAnalysis {
    /// Check if merge can proceed without conflicts.
    fn can_merge(&self) -> bool {
        self.conflicting.is_empty()
    }

    fn __repr__(&self) -> String {
        format!(
            "PyMergeAnalysis(auto_mergeable={}, conflicting={}, source_only={}, target_only={})",
            self.auto_mergeable.len(),
            self.conflicting.len(),
            self.source_only.len(),
            self.target_only.len()
        )
    }
}

/// Outcome of an algebraic merge operation.
#[pyclass]
#[derive(Clone)]
struct PyMergeOutcome {
    #[pyo3(get)]
    source_branch: String,
    #[pyo3(get)]
    target_branch: String,
    #[pyo3(get)]
    fast_forwarded: Vec<String>,
    #[pyo3(get)]
    algebraically_merged: Vec<String>,
    #[pyo3(get)]
    conflicts: Vec<String>,
    #[pyo3(get)]
    success: bool,
    #[pyo3(get)]
    description: Option<String>,
}

impl From<MergeOutcome> for PyMergeOutcome {
    fn from(o: MergeOutcome) -> Self {
        Self {
            source_branch: o.source_branch,
            target_branch: o.target_branch,
            fast_forwarded: o.fast_forwarded,
            algebraically_merged: o.algebraically_merged,
            conflicts: o.conflicts,
            success: o.success,
            description: o.description,
        }
    }
}

#[pymethods]
impl PyMergeOutcome {
    fn __repr__(&self) -> String {
        format!(
            "PyMergeOutcome(success={}, fast_forwarded={}, algebraically_merged={}, conflicts={})",
            self.success,
            self.fast_forwarded.len(),
            self.algebraically_merged.len(),
            self.conflicts.len()
        )
    }
}

/// Analyze merge compatibility using algebraic schemas.
///
/// Args:
///     diff: Branch diff to analyze
///     registry: Schema registry with table schemas
///
/// Returns:
///     PyMergeAnalysis indicating which tables can be auto-merged
#[pyfunction]
fn analyze_merge(diff: &PyBranchDiff, registry: &PyAlgebraicSchemaRegistry) -> PyMergeAnalysis {
    let rust_diff = BranchDiff {
        source_branch: diff.source_branch.clone(),
        target_branch: diff.target_branch.clone(),
        unchanged: diff.unchanged.clone(),
        modified: diff.modified.clone(),
        added_in_source: diff.added_in_source.clone(),
        added_in_target: diff.added_in_target.clone(),
        has_conflicts: diff.has_conflicts,
    };
    let analyzer = MergeAnalyzer::new(&registry.inner);
    PyMergeAnalysis::from(analyzer.analyze(&rust_diff))
}

#[pymodule]
fn _rhizo(m: &Bound<'_, PyModule>) -> PyResult<()> {
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

    // Phase 4: Native Parquet (zero-copy Arrow FFI)
    m.add_class::<PyParquetEncoder>()?;
    m.add_class::<PyParquetDecoder>()?;

    // Phase R.2: Predicate Pushdown
    m.add_class::<PyFilterOp>()?;
    m.add_class::<PyScalarValue>()?;
    m.add_class::<PyPredicateFilter>()?;

    // Algebraic Classification
    m.add_class::<PyOpType>()?;
    m.add_class::<PyAlgebraicValue>()?;
    m.add_class::<PyTableAlgebraicSchema>()?;
    m.add_class::<PyAlgebraicSchemaRegistry>()?;
    m.add_class::<PyMergeAnalysis>()?;
    m.add_class::<PyMergeOutcome>()?;
    m.add_function(wrap_pyfunction!(algebraic_merge, m)?)?;
    m.add_function(wrap_pyfunction!(analyze_merge, m)?)?;

    Ok(())
}
