use std::collections::HashMap;
use std::str::FromStr;
use std::sync::Arc;

use pyo3::prelude::*;
use pyo3::exceptions::{PyIOError, PyValueError, PyRuntimeError};

// =============================================================================
// Error Message Sanitization
// =============================================================================

/// Sanitize error messages to prevent information leakage.
///
/// This function removes or masks:
/// - Absolute filesystem paths (Windows and Unix)
/// - Internal implementation details
///
/// User-facing errors retain enough context to be actionable while
/// preventing leakage of system structure.
fn sanitize_error_message(msg: &str) -> String {
    // Regex-free path detection and sanitization for performance
    let mut result = String::with_capacity(msg.len());
    let chars: Vec<char> = msg.chars().collect();
    let len = chars.len();
    let mut i = 0;

    while i < len {
        // Detect Windows absolute paths: C:\ or similar drive letters
        if i + 2 < len
            && chars[i].is_ascii_alphabetic()
            && chars[i + 1] == ':'
            && (chars[i + 2] == '\\' || chars[i + 2] == '/')
        {
            // Found a Windows absolute path, extract just the filename
            let path_start = i;
            // Find the end of the path (space, quote, or end of string)
            while i < len && !chars[i].is_whitespace() && chars[i] != '"' && chars[i] != '\'' {
                i += 1;
            }
            let path: String = chars[path_start..i].iter().collect();
            if let Some(filename) = extract_filename(&path) {
                result.push_str("<path>/");
                result.push_str(filename);
            } else {
                result.push_str("<path>");
            }
            continue;
        }

        // Detect Unix absolute paths: starts with /
        if chars[i] == '/' && (i == 0 || chars[i - 1].is_whitespace() || chars[i - 1] == '"' || chars[i - 1] == '\'') {
            // Check if this looks like a path (contains more slashes or typical path chars)
            let mut j = i + 1;
            let mut has_path_chars = false;
            while j < len && !chars[j].is_whitespace() && chars[j] != '"' && chars[j] != '\'' {
                if chars[j] == '/' || chars[j].is_alphanumeric() || chars[j] == '_' || chars[j] == '-' || chars[j] == '.' {
                    has_path_chars = true;
                }
                j += 1;
            }
            // Only treat as path if it has typical path characteristics
            if has_path_chars && j > i + 1 {
                let path: String = chars[i..j].iter().collect();
                // Heuristic: if it looks like a real path (has multiple segments or common extensions)
                if path.contains('/') && path.len() > 2 {
                    if let Some(filename) = extract_filename(&path) {
                        result.push_str("<path>/");
                        result.push_str(filename);
                    } else {
                        result.push_str("<path>");
                    }
                    i = j;
                    continue;
                }
            }
        }

        result.push(chars[i]);
        i += 1;
    }

    result
}

/// Extract filename from a path string.
fn extract_filename(path: &str) -> Option<&str> {
    // Handle both Windows and Unix separators
    let path = path.trim_end_matches(['/', '\\']);
    if let Some(pos) = path.rfind(['/', '\\']) {
        let filename = &path[pos + 1..];
        if !filename.is_empty() {
            return Some(filename);
        }
    }
    None
}

/// Sanitize IO error messages, providing a generic message for internal errors.
fn sanitize_io_error(e: &std::io::Error) -> String {
    // Provide user-friendly error messages based on error kind
    let kind_msg = match e.kind() {
        std::io::ErrorKind::NotFound => "File or directory not found",
        std::io::ErrorKind::PermissionDenied => "Permission denied",
        std::io::ErrorKind::AlreadyExists => "File or directory already exists",
        std::io::ErrorKind::InvalidInput => "Invalid input",
        std::io::ErrorKind::InvalidData => "Invalid data",
        std::io::ErrorKind::WriteZero => "Write operation failed",
        std::io::ErrorKind::Interrupted => "Operation interrupted",
        std::io::ErrorKind::UnexpectedEof => "Unexpected end of file",
        std::io::ErrorKind::OutOfMemory => "Out of memory",
        _ => "I/O operation failed",
    };

    // Sanitize any path information from the raw error message
    let raw_msg = e.to_string();
    let sanitized = sanitize_error_message(&raw_msg);

    // If the sanitized message differs significantly, use the kind message
    // to avoid leaking partial path information
    if sanitized.contains("<path>") {
        format!("{}: {}", kind_msg, sanitized)
    } else {
        sanitized
    }
}
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
    // Distributed types
    VectorClock, NodeId, CausalOrder,
    AlgebraicOperation, AlgebraicTransaction, VersionedUpdate,
    LocalCommitProtocol,
    // Simulation types
    SimulatedCluster, SimulationConfig, SimulationStats, NetworkCondition,
};

// Phase 4: Arrow pyarrow for zero-copy FFI
use arrow_pyarrow::{ToPyArrow, FromPyArrow};
use arrow::record_batch::RecordBatch;

/// Convert ChunkStoreError to appropriate Python exception
fn chunk_err_to_py(e: ChunkStoreError) -> PyErr {
    match e {
        ChunkStoreError::NotFound(h) => PyIOError::new_err(format!("Chunk not found: {}", h)),
        ChunkStoreError::InvalidHash(msg) => PyValueError::new_err(format!("Invalid hash: {}", msg)),
        ChunkStoreError::HashMismatch { expected, actual } => {
            PyValueError::new_err(format!("Hash mismatch: expected {}, got {}", expected, actual))
        }
        ChunkStoreError::Io(e) => PyIOError::new_err(sanitize_io_error(&e)),
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
        CatalogError::Io(e) => PyIOError::new_err(sanitize_io_error(&e)),
        CatalogError::Json(e) => PyValueError::new_err(format!("JSON error: {}", sanitize_error_message(&e.to_string()))),
        CatalogError::LockError(msg) => PyIOError::new_err(format!("Lock error: {}", sanitize_error_message(&msg))),
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
        BranchError::Io(e) => PyIOError::new_err(sanitize_io_error(&e)),
        BranchError::Json(e) => PyValueError::new_err(format!("JSON error: {}", sanitize_error_message(&e.to_string()))),
    }
}

/// Convert MerkleError to appropriate Python exception
fn merkle_err_to_py(e: MerkleError) -> PyErr {
    match e {
        MerkleError::Io(e) => PyIOError::new_err(sanitize_io_error(&e)),
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
            PyValueError::new_err(format!("Tree corruption: {}", sanitize_error_message(&msg)))
        }
        MerkleError::Serialization(msg) => {
            PyValueError::new_err(format!("Serialization error: {}", sanitize_error_message(&msg)))
        }
        MerkleError::ChunkStore(msg) => {
            PyIOError::new_err(format!("Chunk store error: {}", sanitize_error_message(&msg)))
        }
    }
}

/// Convert ParquetError to appropriate Python exception
fn parquet_err_to_py(e: ParquetError) -> PyErr {
    match e {
        ParquetError::Arrow(e) => PyValueError::new_err(format!("Arrow error: {}", sanitize_error_message(&e.to_string()))),
        ParquetError::Parquet(e) => PyValueError::new_err(format!("Parquet error: {}", sanitize_error_message(&e.to_string()))),
        ParquetError::EmptyData => PyValueError::new_err("Cannot process empty data"),
        ParquetError::InvalidCompression(c) => {
            PyValueError::new_err(format!("Invalid compression: {}", c))
        }
        ParquetError::InvalidColumn(msg) => {
            PyValueError::new_err(format!("Invalid column: {}", msg))
        }
        ParquetError::FileTooLarge { size, max } => {
            PyValueError::new_err(format!(
                "File size {} bytes exceeds maximum {} bytes",
                size, max
            ))
        }
        ParquetError::InvalidRowCount(count) => {
            PyValueError::new_err(format!("Invalid row count in Parquet metadata: {}", count))
        }
        ParquetError::RowCountOverflow => {
            PyValueError::new_err("Row count overflow: total rows exceed maximum")
        }
    }
}

// =============================================================================
// Phase 4: Native Parquet Encoder/Decoder with Zero-Copy Arrow FFI
// =============================================================================

/// High-performance Parquet encoder using Rust's parquet crate.
///
/// Provides zero-copy Arrow data transfer from Python via Arrow's pyarrow FFI,
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
    fn encode(&self, batch: Bound<'_, PyAny>) -> PyResult<Vec<u8>> {
        let rust_batch = RecordBatch::from_pyarrow_bound(&batch)
            .map_err(|e| PyValueError::new_err(format!("Invalid RecordBatch: {}", sanitize_error_message(&e.to_string()))))?;
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
    fn encode_batch(&self, batches: Vec<Bound<'_, PyAny>>) -> PyResult<Vec<Vec<u8>>> {
        let rust_batches: Vec<_> = batches
            .iter()
            .map(|b| RecordBatch::from_pyarrow_bound(b))
            .collect::<Result<Vec<_>, _>>()
            .map_err(|e| PyValueError::new_err(format!("Invalid RecordBatch: {}", sanitize_error_message(&e.to_string()))))?;
        self.inner
            .encode_batch(&rust_batches)
            .map_err(parquet_err_to_py)
    }
}

/// High-performance Parquet decoder using Rust's parquet crate.
///
/// Provides zero-copy Arrow data transfer to Python via Arrow's pyarrow FFI,
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
        batch.to_pyarrow(py).map_err(|e| PyValueError::new_err(sanitize_error_message(&e.to_string())))
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
            .map(|batch| batch.to_pyarrow(py).map_err(|e| PyValueError::new_err(sanitize_error_message(&e.to_string()))))
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
        batch.to_pyarrow(py).map_err(|e| PyValueError::new_err(sanitize_error_message(&e.to_string())))
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
        batch.to_pyarrow(py).map_err(|e| PyValueError::new_err(sanitize_error_message(&e.to_string())))
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
        batch.to_pyarrow(py).map_err(|e| PyValueError::new_err(sanitize_error_message(&e.to_string())))
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

    /// Commit the next version of a table with auto-assigned version number.
    ///
    /// Atomically reads the latest version and increments, preventing race
    /// conditions where concurrent writers compute the same version number.
    fn commit_next(&self, table_name: &str, chunk_hashes: Vec<String>) -> PyResult<u64> {
        self.inner.commit_next_version(table_name, chunk_hashes).map_err(catalog_err_to_py)
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
    #[pyo3(get)]
    source_only_changes: Vec<(String, u64, u64)>,
    #[pyo3(get)]
    target_only_changes: Vec<(String, u64, u64)>,
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
            source_only_changes: d.source_only_changes,
            target_only_changes: d.target_only_changes,
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
        TransactionError::Io(e) => PyIOError::new_err(sanitize_io_error(&e)),
        TransactionError::Json(e) => PyValueError::new_err(format!("JSON error: {}", sanitize_error_message(&e.to_string()))),
        TransactionError::CatalogError(msg) => PyIOError::new_err(format!("Catalog error: {}", sanitize_error_message(&msg))),
        TransactionError::BranchError(msg) => PyIOError::new_err(format!("Branch error: {}", sanitize_error_message(&msg))),
        _ => PyRuntimeError::new_err(sanitize_error_message(&e.to_string())),
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

// ============================================================================
// Distributed Types (Coordination-Free Transactions)
// ============================================================================

/// Node identifier for distributed systems.
#[pyclass]
#[derive(Clone)]
pub struct PyNodeId {
    inner: NodeId,
}

#[pymethods]
impl PyNodeId {
    #[new]
    fn new(id: &str) -> Self {
        Self {
            inner: NodeId::new(id),
        }
    }

    fn __str__(&self) -> String {
        self.inner.as_str().to_string()
    }

    fn __repr__(&self) -> String {
        format!("NodeId('{}')", self.inner.as_str())
    }

    fn __eq__(&self, other: &PyNodeId) -> bool {
        self.inner == other.inner
    }

    fn __hash__(&self) -> u64 {
        use std::hash::{Hash, Hasher};
        let mut hasher = std::collections::hash_map::DefaultHasher::new();
        self.inner.as_str().hash(&mut hasher);
        hasher.finish()
    }
}

/// Causal ordering relationship between events.
#[pyclass]
#[derive(Clone)]
pub struct PyCausalOrder {
    #[pyo3(get)]
    order: String,
}

impl From<CausalOrder> for PyCausalOrder {
    fn from(order: CausalOrder) -> Self {
        let order_str = match order {
            CausalOrder::Before => "before",
            CausalOrder::After => "after",
            CausalOrder::Concurrent => "concurrent",
            CausalOrder::Equal => "equal",
        };
        Self {
            order: order_str.to_string(),
        }
    }
}

#[pymethods]
impl PyCausalOrder {
    /// Check if merge is needed (concurrent events require merge).
    fn needs_merge(&self) -> bool {
        self.order == "concurrent"
    }

    /// Check if update should be applied (other is newer or concurrent).
    fn should_apply(&self) -> bool {
        self.order == "before" || self.order == "concurrent"
    }

    fn __str__(&self) -> String {
        self.order.clone()
    }

    fn __repr__(&self) -> String {
        format!("CausalOrder('{}')", self.order)
    }
}

/// Vector clock for causality tracking in distributed systems.
///
/// Vector clocks enable determining whether events happened-before
/// each other or are concurrent (requiring algebraic merge).
///
/// Example:
///     >>> node_a = PyNodeId("sf")
///     >>> node_b = PyNodeId("tokyo")
///     >>> clock_a = PyVectorClock()
///     >>> clock_a.tick(node_a)
///     >>> clock_b = PyVectorClock()
///     >>> clock_b.tick(node_b)
///     >>> clock_a.concurrent_with(clock_b)  # True - need to merge!
#[pyclass]
#[derive(Clone)]
pub struct PyVectorClock {
    inner: VectorClock,
}

#[pymethods]
impl PyVectorClock {
    #[new]
    fn new() -> Self {
        Self {
            inner: VectorClock::new(),
        }
    }

    /// Create a clock with a single node's time initialized.
    #[staticmethod]
    fn with_node(node_id: &PyNodeId, time: u64) -> Self {
        Self {
            inner: VectorClock::with_node(&node_id.inner, time),
        }
    }

    /// Increment this node's logical time.
    ///
    /// Call this before performing a local operation that should be tracked.
    fn tick(&mut self, node_id: &PyNodeId) {
        self.inner.tick(&node_id.inner);
    }

    /// Get the logical time for a specific node.
    fn get(&self, node_id: &PyNodeId) -> u64 {
        self.inner.get(&node_id.inner)
    }

    /// Set the logical time for a specific node.
    fn set(&mut self, node_id: &PyNodeId, time: u64) {
        self.inner.set(&node_id.inner, time);
    }

    /// Merge another vector clock into this one.
    ///
    /// After merging, this clock will have the component-wise maximum.
    fn merge(&mut self, other: &PyVectorClock) {
        self.inner.merge(&other.inner);
    }

    /// Check if this clock happened strictly before another clock.
    fn happened_before(&self, other: &PyVectorClock) -> bool {
        self.inner.happened_before(&other.inner)
    }

    /// Check if this clock happened strictly after another clock.
    fn happened_after(&self, other: &PyVectorClock) -> bool {
        self.inner.happened_after(&other.inner)
    }

    /// Check if two clocks are concurrent (neither happened before the other).
    ///
    /// Concurrent events may need algebraic merging.
    fn concurrent_with(&self, other: &PyVectorClock) -> bool {
        self.inner.concurrent_with(&other.inner)
    }

    /// Compare two clocks and return the causal relationship.
    fn compare(&self, other: &PyVectorClock) -> PyCausalOrder {
        PyCausalOrder::from(self.inner.compare(&other.inner))
    }

    /// Get the number of nodes with entries in this clock.
    fn node_count(&self) -> usize {
        self.inner.node_count()
    }

    /// Check if this clock is empty (no nodes have ticked).
    fn is_empty(&self) -> bool {
        self.inner.is_empty()
    }

    /// Get the sum of all logical times.
    fn sum(&self) -> u64 {
        self.inner.sum()
    }

    /// Create a merged copy of two clocks (max of each component).
    #[staticmethod]
    fn max(a: &PyVectorClock, b: &PyVectorClock) -> PyVectorClock {
        PyVectorClock {
            inner: VectorClock::max(&a.inner, &b.inner),
        }
    }

    /// Return a copy with incremented time for the given node.
    fn ticked(&self, node_id: &PyNodeId) -> PyVectorClock {
        PyVectorClock {
            inner: self.inner.ticked(&node_id.inner),
        }
    }

    /// Serialize to JSON string.
    fn to_json(&self) -> PyResult<String> {
        serde_json::to_string(&self.inner)
            .map_err(|e| PyValueError::new_err(format!("Serialization error: {}", sanitize_error_message(&e.to_string()))))
    }

    /// Deserialize from JSON string.
    #[staticmethod]
    fn from_json(json: &str) -> PyResult<PyVectorClock> {
        let inner: VectorClock = serde_json::from_str(json)
            .map_err(|e| PyValueError::new_err(format!("Deserialization error: {}", sanitize_error_message(&e.to_string()))))?;
        Ok(PyVectorClock { inner })
    }

    fn __str__(&self) -> String {
        self.inner.to_string()
    }

    fn __repr__(&self) -> String {
        format!("VectorClock({})", self.inner)
    }

    fn __eq__(&self, other: &PyVectorClock) -> bool {
        self.inner == other.inner
    }
}

// ============================================================================
// Local Commit Protocol Types (Coordination-Free Transactions)
// ============================================================================

/// A single algebraic operation on a key.
///
/// Operations are the building blocks of transactions. Each operation
/// specifies a key, an algebraic type, and a delta value.
#[pyclass]
#[derive(Clone)]
pub struct PyAlgebraicOperation {
    inner: AlgebraicOperation,
}

#[pymethods]
impl PyAlgebraicOperation {
    /// Create a new algebraic operation.
    ///
    /// Args:
    ///     key: The key to operate on
    ///     op_type: The algebraic operation type
    ///     value: The value or delta to apply
    #[new]
    fn new(key: &str, op_type: &PyOpType, value: &PyAlgebraicValue) -> Self {
        Self {
            inner: AlgebraicOperation::new(key, op_type.inner, value.inner.clone()),
        }
    }

    /// Get the key.
    #[getter]
    fn key(&self) -> String {
        self.inner.key().to_string()
    }

    /// Get the operation type.
    #[getter]
    fn op_type(&self) -> PyOpType {
        PyOpType { inner: self.inner.op_type() }
    }

    /// Get the value.
    #[getter]
    fn value(&self) -> PyAlgebraicValue {
        PyAlgebraicValue { inner: self.inner.value().clone() }
    }

    /// Check if this operation can be committed locally (is algebraic).
    fn is_algebraic(&self) -> bool {
        self.inner.is_algebraic()
    }

    fn __repr__(&self) -> String {
        format!(
            "AlgebraicOperation(key='{}', op={}, value={})",
            self.inner.key(),
            self.inner.op_type(),
            self.inner.value()
        )
    }
}

/// A transaction containing multiple algebraic operations.
///
/// Transactions group operations that should be applied atomically.
/// For coordination-free commits, all operations must be algebraic.
#[pyclass]
#[derive(Clone)]
pub struct PyAlgebraicTransaction {
    inner: AlgebraicTransaction,
}

#[pymethods]
impl PyAlgebraicTransaction {
    /// Create a new empty transaction.
    #[new]
    fn new() -> Self {
        Self {
            inner: AlgebraicTransaction::new(),
        }
    }

    /// Add an operation to the transaction.
    fn add_operation(&mut self, op: &PyAlgebraicOperation) {
        self.inner.add_operation(op.inner.clone());
    }

    /// Get the number of operations.
    fn len(&self) -> usize {
        self.inner.len()
    }

    /// Check if the transaction is empty.
    fn is_empty(&self) -> bool {
        self.inner.is_empty()
    }

    /// Check if all operations in this transaction are algebraic.
    fn is_fully_algebraic(&self) -> bool {
        self.inner.is_fully_algebraic()
    }

    /// Set metadata.
    fn set_metadata(&mut self, key: &str, value: &str) {
        self.inner.set_metadata(key, value);
    }

    /// Get metadata.
    fn get_metadata(&self, key: &str) -> Option<String> {
        self.inner.get_metadata(key).map(|s| s.to_string())
    }

    fn __repr__(&self) -> String {
        format!(
            "AlgebraicTransaction(ops={}, algebraic={})",
            self.inner.len(),
            self.inner.is_fully_algebraic()
        )
    }
}

/// The result of a local commit: operations with their causal context.
///
/// A VersionedUpdate represents a committed set of operations along with
/// the vector clock at the time of commit. This allows other nodes to:
/// 1. Determine the causal relationship with their own state
/// 2. Merge concurrent updates correctly
#[pyclass]
#[derive(Clone)]
pub struct PyVersionedUpdate {
    inner: VersionedUpdate,
}

#[pymethods]
impl PyVersionedUpdate {
    /// Get all operations.
    fn operations(&self) -> Vec<PyAlgebraicOperation> {
        self.inner
            .operations()
            .iter()
            .map(|op| PyAlgebraicOperation { inner: op.clone() })
            .collect()
    }

    /// Get the vector clock.
    #[getter]
    fn clock(&self) -> PyVectorClock {
        PyVectorClock {
            inner: self.inner.clock().clone(),
        }
    }

    /// Get the origin node.
    #[getter]
    fn origin_node(&self) -> PyNodeId {
        PyNodeId {
            inner: self.inner.origin_node().clone(),
        }
    }

    /// Get the update ID if set.
    #[getter]
    fn update_id(&self) -> Option<String> {
        self.inner.update_id().map(|s| s.to_string())
    }

    /// Compare this update's causality with another.
    fn compare(&self, other: &PyVersionedUpdate) -> PyCausalOrder {
        PyCausalOrder::from(self.inner.compare(&other.inner))
    }

    /// Check if this update is concurrent with another.
    fn is_concurrent_with(&self, other: &PyVersionedUpdate) -> bool {
        self.inner.is_concurrent_with(&other.inner)
    }

    fn __repr__(&self) -> String {
        format!(
            "VersionedUpdate(origin='{}', ops={}, clock={})",
            self.inner.origin_node(),
            self.inner.operations().len(),
            self.inner.clock()
        )
    }
}

/// The local commit protocol for coordination-free transactions.
///
/// This class provides the core logic for:
/// 1. Checking if a transaction can commit locally
/// 2. Committing a transaction locally (no coordination)
/// 3. Merging concurrent updates from different nodes
///
/// Example:
///     >>> node = PyNodeId("my-node")
///     >>> clock = PyVectorClock()
///     >>> tx = PyAlgebraicTransaction()
///     >>> tx.add_operation(PyAlgebraicOperation("counter", PyOpType("add"), PyAlgebraicValue(5)))
///     >>> update = PyLocalCommitProtocol.commit_local(tx, node, clock)
#[pyclass]
pub struct PyLocalCommitProtocol;

#[pymethods]
impl PyLocalCommitProtocol {
    /// Check if a transaction can be committed locally without coordination.
    ///
    /// Returns True if all operations in the transaction are algebraic
    /// (semilattice or Abelian), meaning they commute and can be applied
    /// in any order.
    #[staticmethod]
    fn can_commit_locally(tx: &PyAlgebraicTransaction) -> bool {
        LocalCommitProtocol::can_commit_locally(&tx.inner)
    }

    /// Commit a transaction locally, returning a versioned update.
    ///
    /// This operation:
    /// 1. Validates that all operations are algebraic
    /// 2. Increments the local vector clock
    /// 3. Returns a VersionedUpdate that can be sent to other nodes
    ///
    /// Args:
    ///     tx: The transaction to commit
    ///     node_id: This node's ID
    ///     clock: This node's vector clock (will be mutated)
    ///
    /// Returns:
    ///     The committed update with causal context
    ///
    /// Raises:
    ///     ValueError: If the transaction cannot be committed locally
    #[staticmethod]
    fn commit_local(
        tx: &PyAlgebraicTransaction,
        node_id: &PyNodeId,
        clock: &mut PyVectorClock,
    ) -> PyResult<PyVersionedUpdate> {
        LocalCommitProtocol::commit_local(&tx.inner, &node_id.inner, &mut clock.inner)
            .map(|update| PyVersionedUpdate { inner: update })
            .map_err(|e| PyValueError::new_err(sanitize_error_message(&format!("{}", e))))
    }

    /// Merge two versioned updates into one.
    ///
    /// This is the core of coordination-free merging. Given two updates
    /// (potentially concurrent), this function:
    /// 1. Combines operations by key
    /// 2. Merges values using algebraic operations
    /// 3. Computes the merged vector clock
    ///
    /// For algebraic operations, merge(A, B) = merge(B, A) (commutative).
    #[staticmethod]
    fn merge_updates(
        update1: &PyVersionedUpdate,
        update2: &PyVersionedUpdate,
    ) -> PyResult<PyVersionedUpdate> {
        LocalCommitProtocol::merge_updates(&update1.inner, &update2.inner)
            .map(|update| PyVersionedUpdate { inner: update })
            .map_err(|e| PyValueError::new_err(sanitize_error_message(&format!("{}", e))))
    }

    /// Merge multiple updates at once (more efficient than pairwise).
    #[staticmethod]
    fn merge_all(updates: Vec<PyVersionedUpdate>) -> PyResult<PyVersionedUpdate> {
        let inner_updates: Vec<VersionedUpdate> = updates.iter().map(|u| u.inner.clone()).collect();
        LocalCommitProtocol::merge_all(&inner_updates)
            .map(|update| PyVersionedUpdate { inner: update })
            .map_err(|e| PyValueError::new_err(sanitize_error_message(&format!("{}", e))))
    }
}

// ============================================================================
// Phase 4: Simulation Bindings (Multi-Node Convergence Testing)
// ============================================================================

/// Network condition for message delivery in simulation.
///
/// Example:
///     >>> condition = PyNetworkCondition.perfect()  # Messages delivered immediately
///     >>> condition = PyNetworkCondition.reordered()  # Messages may be reordered
///     >>> condition = PyNetworkCondition.delayed(3)  # Messages delayed 3 rounds
///     >>> condition = PyNetworkCondition.partitioned()  # No delivery (network partition)
#[pyclass]
#[derive(Clone)]
pub struct PyNetworkCondition {
    inner: NetworkCondition,
}

#[pymethods]
impl PyNetworkCondition {
    /// Create a perfect network condition (immediate ordered delivery).
    #[staticmethod]
    fn perfect() -> Self {
        Self { inner: NetworkCondition::Perfect }
    }

    /// Create a reordered network condition (messages may arrive out of order).
    #[staticmethod]
    fn reordered() -> Self {
        Self { inner: NetworkCondition::Reordered }
    }

    /// Create a delayed network condition (messages delayed by N rounds).
    #[staticmethod]
    fn delayed(rounds: usize) -> Self {
        Self { inner: NetworkCondition::Delayed(rounds) }
    }

    /// Create a partitioned network condition (no message delivery).
    #[staticmethod]
    fn partitioned() -> Self {
        Self { inner: NetworkCondition::Partitioned }
    }

    fn __repr__(&self) -> String {
        match self.inner {
            NetworkCondition::Perfect => "NetworkCondition.Perfect".to_string(),
            NetworkCondition::Reordered => "NetworkCondition.Reordered".to_string(),
            NetworkCondition::Delayed(n) => format!("NetworkCondition.Delayed({})", n),
            NetworkCondition::Partitioned => "NetworkCondition.Partitioned".to_string(),
        }
    }
}

/// Configuration for the distributed simulation.
///
/// Example:
///     >>> config = PySimulationConfig()
///     >>> config.max_rounds = 200
///     >>> config.randomize_order = True
#[pyclass]
#[derive(Clone)]
pub struct PySimulationConfig {
    inner: SimulationConfig,
}

#[pymethods]
impl PySimulationConfig {
    #[new]
    fn new() -> Self {
        Self { inner: SimulationConfig::default() }
    }

    /// Maximum number of propagation rounds.
    #[getter]
    fn max_rounds(&self) -> usize {
        self.inner.max_rounds
    }

    #[setter]
    fn set_max_rounds(&mut self, rounds: usize) {
        self.inner.max_rounds = rounds;
    }

    /// Whether to randomize message order.
    #[getter]
    fn randomize_order(&self) -> bool {
        self.inner.randomize_order
    }

    #[setter]
    fn set_randomize_order(&mut self, randomize: bool) {
        self.inner.randomize_order = randomize;
    }

    fn __repr__(&self) -> String {
        format!(
            "SimulationConfig(max_rounds={}, randomize_order={})",
            self.inner.max_rounds, self.inner.randomize_order
        )
    }
}

/// Statistics from a simulation run.
///
/// Attributes:
///     messages_sent: Total messages sent between nodes
///     messages_delivered: Total messages successfully delivered
///     messages_dropped: Total messages dropped (due to partitions)
///     rounds_to_converge: Number of rounds until convergence (None if not converged)
///     operations_committed: Total operations committed across all nodes
#[pyclass]
#[derive(Clone)]
pub struct PySimulationStats {
    inner: SimulationStats,
}

#[pymethods]
impl PySimulationStats {
    #[getter]
    fn messages_sent(&self) -> usize {
        self.inner.messages_sent
    }

    #[getter]
    fn messages_delivered(&self) -> usize {
        self.inner.messages_delivered
    }

    #[getter]
    fn messages_dropped(&self) -> usize {
        self.inner.messages_dropped
    }

    #[getter]
    fn rounds_to_converge(&self) -> Option<usize> {
        self.inner.rounds_to_converge
    }

    #[getter]
    fn operations_committed(&self) -> usize {
        self.inner.operations_committed
    }

    fn __repr__(&self) -> String {
        format!(
            "SimulationStats(sent={}, delivered={}, dropped={}, converged={:?}, ops={})",
            self.inner.messages_sent,
            self.inner.messages_delivered,
            self.inner.messages_dropped,
            self.inner.rounds_to_converge,
            self.inner.operations_committed
        )
    }
}

/// A simulated node in a distributed cluster.
///
/// Each node has:
/// - A unique node ID
/// - A vector clock for causality tracking
/// - Local state (key-value pairs with algebraic operations)
/// - An outbox of pending updates to propagate
///
/// Example:
///     >>> cluster = PySimulatedCluster(5)
///     >>> node = cluster.get_node(0)
///     >>> print(node.node_id)
#[pyclass]
pub struct PySimulatedNode {
    // We'll provide read-only access to node state through the cluster
    node_id: String,
    index: usize,
}

#[pymethods]
impl PySimulatedNode {
    /// The node's unique identifier.
    #[getter]
    fn node_id(&self) -> &str {
        &self.node_id
    }

    /// The node's index in the cluster.
    #[getter]
    fn index(&self) -> usize {
        self.index
    }

    fn __repr__(&self) -> String {
        format!("SimulatedNode(id='{}', index={})", self.node_id, self.index)
    }
}

/// A simulated cluster of nodes for testing coordination-free convergence.
///
/// This class provides a simulation framework to prove that nodes using
/// coordination-free transactions converge to the same state regardless
/// of message ordering, network delays, or partitions.
///
/// Example:
///     >>> # Create a 5-node cluster
///     >>> cluster = PySimulatedCluster(5)
///     >>>
///     >>> # Each node performs local operations
///     >>> for i in range(5):
///     ...     tx = PyAlgebraicTransaction()
///     ...     tx.add_operation(PyAlgebraicOperation("counter", PyOpType.add(), PyAlgebraicValue((i + 1) * 10)))
///     ...     cluster.commit_on_node(i, tx)
///     >>>
///     >>> # Propagate all updates (simulated gossip)
///     >>> cluster.propagate_all()
///     >>>
///     >>> # Verify convergence
///     >>> assert cluster.verify_convergence()
///     >>> print(cluster.get_node_state(0, "counter"))  # 150
#[pyclass]
pub struct PySimulatedCluster {
    inner: SimulatedCluster,
}

#[pymethods]
impl PySimulatedCluster {
    /// Create a new simulated cluster with N nodes.
    #[new]
    fn new(num_nodes: usize) -> Self {
        Self { inner: SimulatedCluster::new(num_nodes) }
    }

    /// Create a cluster with custom configuration.
    #[staticmethod]
    fn with_config(num_nodes: usize, config: &PySimulationConfig) -> Self {
        Self { inner: SimulatedCluster::with_config(num_nodes, config.inner.clone()) }
    }

    /// Get the number of nodes in the cluster.
    #[getter]
    fn num_nodes(&self) -> usize {
        self.inner.num_nodes()
    }

    /// Get the current simulation round.
    #[getter]
    fn round(&self) -> usize {
        self.inner.round
    }

    /// Commit a transaction on a specific node.
    ///
    /// Args:
    ///     node_index: The index of the node to commit on
    ///     tx: The algebraic transaction to commit
    ///
    /// Returns:
    ///     The versioned update that was committed
    fn commit_on_node(&mut self, node_index: usize, tx: &PyAlgebraicTransaction) -> PyResult<PyVersionedUpdate> {
        self.inner.commit_on_node(node_index, tx.inner.clone())
            .map(|update| PyVersionedUpdate { inner: update })
            .map_err(|e| PyValueError::new_err(sanitize_error_message(&format!("{}", e))))
    }

    /// Run one round of propagation (broadcast + deliver).
    fn propagate_round(&mut self) {
        self.inner.propagate_round();
    }

    /// Propagate until convergence or max rounds.
    fn propagate_all(&mut self) {
        self.inner.propagate_all();
    }

    /// Add a network partition between two nodes.
    ///
    /// After calling this, messages between node_a and node_b will be dropped.
    fn partition(&mut self, node_a: usize, node_b: usize) {
        self.inner.partition(node_a, node_b);
    }

    /// Remove all network partitions (heal the network).
    fn heal_partitions(&mut self) {
        self.inner.heal_partitions();
    }

    /// Re-queue all local updates for propagation.
    ///
    /// Call this after healing partitions to ensure updates are re-gossiped.
    fn requeue_all_updates(&mut self) {
        self.inner.requeue_all_updates();
    }

    /// Verify that all nodes have converged to the same state.
    ///
    /// Returns True if all nodes have identical values for all keys.
    fn verify_convergence(&self) -> bool {
        self.inner.verify_convergence()
    }

    /// Get the value of a key on a specific node.
    ///
    /// Returns None if the key doesn't exist on that node.
    fn get_node_state(&self, node_index: usize, key: &str) -> Option<PyAlgebraicValue> {
        self.inner.get_node_state(node_index, key).map(|v: &AlgebraicValue| PyAlgebraicValue { inner: v.clone() })
    }

    /// Get all keys that exist across any node.
    fn all_keys(&self) -> Vec<String> {
        self.inner.all_keys()
    }

    /// Get simulation statistics.
    fn get_stats(&self) -> PySimulationStats {
        PySimulationStats { inner: self.inner.get_stats().clone() }
    }

    /// Get a debug string showing state of all nodes.
    fn debug_state(&self) -> String {
        self.inner.debug_state()
    }

    fn __repr__(&self) -> String {
        format!(
            "SimulatedCluster(nodes={}, round={}, converged={})",
            self.inner.num_nodes(),
            self.inner.round,
            self.inner.verify_convergence()
        )
    }
}

/// Builder for complex simulation scenarios.
///
/// Example:
///     >>> tx1 = PyAlgebraicTransaction()
///     >>> tx1.add_operation(PyAlgebraicOperation("count", PyOpType.add(), PyAlgebraicValue(100)))
///     >>> tx2 = PyAlgebraicTransaction()
///     >>> tx2.add_operation(PyAlgebraicOperation("count", PyOpType.add(), PyAlgebraicValue(200)))
///     >>>
///     >>> builder = PySimulationBuilder(2)
///     >>> builder.set_max_rounds(50)
///     >>> builder.add_operation(0, tx1)
///     >>> builder.add_operation(1, tx2)
///     >>> cluster = builder.run()
///     >>>
///     >>> assert cluster.verify_convergence()
///     >>> print(cluster.get_node_state(0, "count"))  # 300
#[pyclass]
pub struct PySimulationBuilder {
    num_nodes: usize,
    config: SimulationConfig,
    initial_operations: Vec<(usize, AlgebraicTransaction)>,
}

#[pymethods]
impl PySimulationBuilder {
    /// Create a new simulation builder with N nodes.
    #[new]
    fn new(num_nodes: usize) -> Self {
        Self {
            num_nodes,
            config: SimulationConfig::default(),
            initial_operations: Vec::new(),
        }
    }

    /// Set maximum propagation rounds.
    fn set_max_rounds(&mut self, rounds: usize) {
        self.config.max_rounds = rounds;
    }

    /// Enable message reordering.
    fn set_reordering(&mut self, enabled: bool) {
        self.config.randomize_order = enabled;
    }

    /// Add a network partition between two nodes.
    fn add_partition(&mut self, node_a: usize, node_b: usize) {
        self.config.partitions.push((node_a, node_b));
    }

    /// Add an initial operation for a node.
    fn add_operation(&mut self, node: usize, tx: &PyAlgebraicTransaction) {
        self.initial_operations.push((node, tx.inner.clone()));
    }

    /// Build and run the simulation.
    ///
    /// Returns the cluster after propagation.
    fn run(&self) -> PyResult<PySimulatedCluster> {
        let mut cluster = SimulatedCluster::with_config(self.num_nodes, self.config.clone());

        for (node_index, tx) in &self.initial_operations {
            cluster.commit_on_node(*node_index, tx.clone())
                .map_err(|e| PyValueError::new_err(sanitize_error_message(&format!("{}", e))))?;
        }

        cluster.propagate_all();

        Ok(PySimulatedCluster { inner: cluster })
    }

    fn __repr__(&self) -> String {
        format!(
            "SimulationBuilder(nodes={}, ops={}, max_rounds={})",
            self.num_nodes,
            self.initial_operations.len(),
            self.config.max_rounds
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
        source_only_changes: Vec::new(),
        target_only_changes: Vec::new(),
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

    // Distributed (Coordination-Free Transactions)
    m.add_class::<PyNodeId>()?;
    m.add_class::<PyCausalOrder>()?;
    m.add_class::<PyVectorClock>()?;

    // Local Commit Protocol (Coordination-Free Transactions)
    m.add_class::<PyAlgebraicOperation>()?;
    m.add_class::<PyAlgebraicTransaction>()?;
    m.add_class::<PyVersionedUpdate>()?;
    m.add_class::<PyLocalCommitProtocol>()?;

    // Simulation (Multi-Node Convergence Testing)
    m.add_class::<PyNetworkCondition>()?;
    m.add_class::<PySimulationConfig>()?;
    m.add_class::<PySimulationStats>()?;
    m.add_class::<PySimulatedNode>()?;
    m.add_class::<PySimulatedCluster>()?;
    m.add_class::<PySimulationBuilder>()?;

    Ok(())
}
