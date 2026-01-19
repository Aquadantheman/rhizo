"""Type stubs for the _rhizo Rust extension module (rhizo-core)."""

from typing import List, Dict, Optional, Tuple, Union
import pyarrow as pa

class PyChunkStore:
    def __init__(self, path: str) -> None: ...
    def put(self, data: bytes) -> str: ...
    def get(self, hash: str) -> bytes: ...
    def get_verified(self, hash: str) -> bytes: ...
    def exists(self, hash: str) -> bool: ...
    def delete(self, hash: str) -> None: ...
    def put_batch(self, chunks: List[bytes]) -> List[str]: ...
    def get_batch(self, hashes: List[str]) -> List[bytes]: ...
    def get_batch_verified(self, hashes: List[str]) -> List[bytes]: ...
    def get_mmap(self, hash: str) -> bytes: ...
    def get_mmap_batch(self, hashes: List[str]) -> List[bytes]: ...

class PyTableVersion:
    table_name: str
    version: int
    chunk_hashes: List[str]
    schema_hash: Optional[str]
    created_at: int
    parent_version: Optional[int]
    metadata: Dict[str, str]

    def __init__(
        self,
        table_name: str,
        version: int,
        chunk_hashes: List[str],
    ) -> None: ...

class PyCatalog:
    def __init__(self, path: str) -> None: ...
    def commit(self, version: PyTableVersion) -> int: ...
    def get_version(
        self,
        table_name: str,
        version: Optional[int] = None,
    ) -> PyTableVersion: ...
    def list_versions(self, table_name: str) -> List[int]: ...
    def list_tables(self) -> List[str]: ...

class PyBranch:
    """A branch represents a named pointer to table versions."""
    name: str
    head: Dict[str, int]
    created_at: int
    parent_branch: Optional[str]
    description: Optional[str]

class PyBranchDiff:
    """Result of comparing two branches."""
    source_branch: str
    target_branch: str
    unchanged: List[str]
    modified: List[Tuple[str, int, int]]  # (table_name, source_version, target_version)
    added_in_source: List[Tuple[str, int]]
    added_in_target: List[Tuple[str, int]]
    has_conflicts: bool

class PyBranchManager:
    """Manages branches for Rhizo tables."""
    def __init__(self, path: str) -> None: ...
    def create(
        self,
        name: str,
        from_branch: Optional[str] = None,
        description: Optional[str] = None,
    ) -> PyBranch: ...
    def get(self, name: str) -> PyBranch: ...
    def list(self) -> List[str]: ...
    def delete(self, name: str) -> None: ...
    def update_head(self, branch_name: str, table_name: str, version: int) -> None: ...
    def get_table_version(self, branch_name: str, table_name: str) -> Optional[int]: ...
    def diff(self, source: str, target: str) -> PyBranchDiff: ...
    def can_fast_forward(self, source: str, target: str) -> bool: ...
    def merge(self, source: str, into: str) -> None: ...
    def get_default(self) -> Optional[str]: ...
    def set_default(self, name: str) -> None: ...

class PyTransactionInfo:
    """Information about a transaction."""
    tx_id: int
    epoch_id: int
    status: str
    branch: str
    started_at: int
    committed_at: Optional[int]
    read_snapshot: Dict[str, int]
    written_tables: List[str]

class PyRecoveryReport:
    """Result of transaction recovery process."""
    last_committed_epoch: Optional[int]
    replayed: List[int]
    rolled_back: List[int]
    already_aborted: List[int]
    already_committed: List[int]
    warnings: List[str]
    errors: List[str]
    is_clean: bool

class PyTransactionManager:
    """Manages cross-table ACID transactions."""
    def __init__(
        self,
        base_path: str,
        catalog_path: str,
        branch_path: Optional[str] = None,
        auto_recover: bool = False,
    ) -> None: ...
    def begin(self, branch: Optional[str] = None) -> int: ...
    def add_write(
        self,
        tx_id: int,
        table_name: str,
        new_version: int,
        chunk_hashes: List[str],
    ) -> None: ...
    def record_read(self, tx_id: int, table_name: str, version: int) -> None: ...
    def commit(self, tx_id: int) -> None: ...
    def abort(self, tx_id: int, reason: str = "User requested") -> None: ...
    def get_transaction(self, tx_id: int) -> PyTransactionInfo: ...
    def active_transactions(self) -> List[PyTransactionInfo]: ...
    def active_count(self) -> int: ...
    def recover(self) -> PyRecoveryReport: ...
    def recover_and_apply(self) -> PyRecoveryReport: ...
    def verify_consistency(self) -> List[str]: ...
    def get_changelog(
        self,
        since_tx_id: Optional[int] = None,
        since_timestamp: Optional[int] = None,
        tables: Optional[List[str]] = None,
        branch: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> List["PyChangelogEntry"]: ...
    def latest_tx_id(self) -> Optional[int]: ...

class PyTableChange:
    """A single table change within a committed transaction."""
    table_name: str
    old_version: Optional[int]
    new_version: int
    chunk_hashes: List[str]

    def is_new_table(self) -> bool: ...

class PyChangelogEntry:
    """Entry in the changelog representing a committed transaction."""
    tx_id: int
    epoch_id: int
    committed_at: int
    branch: str
    changes: List[PyTableChange]

    def changed_tables(self) -> List[str]: ...
    def contains_table(self, table_name: str) -> bool: ...
    def get_change(self, table_name: str) -> Optional[PyTableChange]: ...
    def change_count(self) -> int: ...

# =============================================================================
# Merkle Tree Types
# =============================================================================

class PyDataChunk:
    """A leaf node in the Merkle tree - contains actual data."""
    hash: str
    byte_range: Tuple[int, int]
    size: int
    index: int

class PyMerkleNode:
    """Internal node in the Merkle tree."""
    hash: str
    children: List[str]
    level: int
    index: int

class PyMerkleTree:
    """Complete Merkle tree for a data blob."""
    root_hash: str
    total_size: int
    chunk_size: int
    height: int
    chunks: List[PyDataChunk]

    def chunk_count(self) -> int: ...
    def chunk_hashes(self) -> List[str]: ...
    def chunk_for_offset(self, offset: int) -> Optional[PyDataChunk]: ...
    def chunks_in_range(self, start: int, end: int) -> List[PyDataChunk]: ...

class PyMerkleDiff:
    """Result of comparing two Merkle trees."""
    unchanged_chunks: List[str]
    removed_chunks: List[str]
    added_chunks: List[str]
    reuse_ratio: float

    def unchanged_count(self) -> int: ...
    def added_count(self) -> int: ...
    def removed_count(self) -> int: ...
    def reuse_percentage(self) -> float: ...

class PyMerkleConfig:
    """Configuration for Merkle tree building."""
    chunk_size: int
    branching_factor: int

    def __init__(
        self,
        chunk_size: int = 65536,
        branching_factor: int = 2,
    ) -> None: ...

def merkle_build_tree(
    data: bytes,
    config: Optional[PyMerkleConfig] = None,
) -> PyMerkleTree:
    """Build a Merkle tree from data.

    Args:
        data: Raw bytes to build tree from
        config: Optional MerkleConfig (uses defaults if not provided)

    Returns:
        PyMerkleTree with content-addressable structure
    """
    ...

def merkle_diff_trees(
    old_tree: PyMerkleTree,
    new_tree: PyMerkleTree,
) -> PyMerkleDiff:
    """Compare two Merkle trees and find differences.

    Args:
        old_tree: Previous version tree
        new_tree: New version tree

    Returns:
        PyMerkleDiff with unchanged, added, and removed chunks
    """
    ...

def merkle_verify_tree(
    tree: PyMerkleTree,
    chunk_store: PyChunkStore,
) -> bool:
    """Verify integrity of a Merkle tree.

    Args:
        tree: The Merkle tree to verify
        chunk_store: A PyChunkStore to retrieve chunk data

    Returns:
        True if verification passes

    Raises:
        ValueError: If integrity check fails
    """
    ...

# =============================================================================
# Phase 4: Native Parquet Encoder/Decoder
# =============================================================================

class PyParquetEncoder:
    """High-performance Parquet encoder using Rust's parquet crate.

    Provides zero-copy Arrow data transfer from Python via pyo3-arrow,
    and parallel encoding of multiple batches using Rayon.
    """

    def __init__(self, compression: Optional[str] = None) -> None:
        """Create a new encoder.

        Args:
            compression: Compression type ("zstd", "snappy", "gzip", "lz4", "none")
                        Defaults to "zstd" for best compression/speed balance.
        """
        ...

    def encode(self, batch: pa.RecordBatch) -> bytes:
        """Encode an Arrow RecordBatch to Parquet bytes.

        Args:
            batch: PyArrow RecordBatch to encode (zero-copy transfer)

        Returns:
            Parquet-encoded bytes
        """
        ...

    def encode_batch(self, batches: List[pa.RecordBatch]) -> List[bytes]:
        """Encode multiple Arrow RecordBatches in parallel.

        Args:
            batches: List of PyArrow RecordBatches to encode

        Returns:
            List of Parquet-encoded bytes for each batch
        """
        ...


class PyParquetDecoder:
    """High-performance Parquet decoder using Rust's parquet crate.

    Provides zero-copy Arrow data transfer to Python via pyo3-arrow,
    and parallel decoding of multiple chunks using Rayon.
    """

    def __init__(self) -> None:
        """Create a new decoder."""
        ...

    def decode(self, data: bytes) -> pa.RecordBatch:
        """Decode Parquet bytes to an Arrow RecordBatch.

        Args:
            data: Parquet file bytes

        Returns:
            PyArrow RecordBatch (zero-copy transfer)
        """
        ...

    def decode_batch(self, chunks: List[bytes]) -> List[pa.RecordBatch]:
        """Decode multiple Parquet chunks in parallel.

        Args:
            chunks: List of Parquet byte arrays to decode

        Returns:
            List of PyArrow RecordBatches for each chunk
        """
        ...

    def decode_columns(self, data: bytes, column_indices: List[int]) -> pa.RecordBatch:
        """Decode only specific columns by index (projection pushdown).

        This is significantly faster when you only need a subset of columns.
        Column indices are 0-based and refer to the schema order.

        Mathematical Model:
            Speedup ≈ n/k where n=total columns, k=requested columns
            Example: 10 columns, query 2 → ~5x speedup on decode phase

        Args:
            data: Parquet file bytes
            column_indices: List of 0-based column indices to decode

        Returns:
            PyArrow RecordBatch with only requested columns
        """
        ...

    def decode_columns_by_name(self, data: bytes, column_names: List[str]) -> pa.RecordBatch:
        """Decode only specific columns by name (projection pushdown).

        Convenience method that resolves column names to indices and applies
        projection pushdown.

        Args:
            data: Parquet file bytes
            column_names: List of column names to decode

        Returns:
            PyArrow RecordBatch with only requested columns

        Raises:
            ValueError: If a column name is not found in schema
        """
        ...

    def decode_with_filter(
        self,
        data: bytes,
        filters: List["PyPredicateFilter"],
        column_indices: Optional[List[int]] = None,
    ) -> pa.RecordBatch:
        """Decode with predicate pushdown (row-level filtering and row-group pruning).

        This method applies filter predicates during decoding using a two-level
        optimization strategy:

        1. **Row Group Pruning**: Check min/max statistics to skip entire row groups
           that cannot contain matching rows.

        2. **Row-Level Filtering**: For row groups that might contain matches,
           apply filters during decoding to skip non-matching rows.

        Mathematical Model:
            For selectivity `s` (fraction of rows matching):
              - Row group pruning can skip G/g row groups based on statistics
              - Row-level filtering reduces output by factor of `s`
              - Combined with projection: Speedup ≈ (G/g) × (n/k) × (1/s)
            Example: 10 row groups, 1 contains data, 10 columns, query 2, 1% selectivity
                     → up to 5000x speedup

        Args:
            data: Parquet file bytes
            filters: List of PyPredicateFilter objects
            column_indices: Optional list of column indices to project

        Returns:
            PyArrow RecordBatch with filters applied

        Example:
            >>> decoder = PyParquetDecoder()
            >>> filter = PyPredicateFilter("age", "gt", 50)
            >>> result = decoder.decode_with_filter(data, [filter])
        """
        ...

    def get_pruning_stats(
        self,
        data: bytes,
        filters: List["PyPredicateFilter"],
    ) -> Tuple[int, int, int]:
        """Get row-group pruning statistics for a filtered decode.

        This is useful for debugging and understanding pruning effectiveness.
        Row-group pruning uses min/max statistics to skip entire row groups
        that cannot contain matching rows, before doing any decoding.

        Args:
            data: Parquet file bytes
            filters: List of PyPredicateFilter objects

        Returns:
            Tuple of (total_row_groups, pruned_row_groups, kept_row_groups)

        Example:
            >>> decoder = PyParquetDecoder()
            >>> filter = PyPredicateFilter("id", "gt", 9000)
            >>> total, pruned, kept = decoder.get_pruning_stats(data, [filter])
            >>> print(f"Pruned {pruned}/{total} row groups ({100*pruned/total:.1f}%)")
        """
        ...


# =============================================================================
# Phase R.2: Predicate Pushdown Types
# =============================================================================

ScalarValueType = Union[int, float, str, bool, None]

class PyFilterOp:
    """Comparison operations for filter predicates.

    These map to SQL-style comparisons:
      - "eq"  → column = value
      - "ne"  → column != value
      - "lt"  → column < value
      - "le"  → column <= value
      - "gt"  → column > value
      - "ge"  → column >= value
    """

    def __init__(self, op: str) -> None:
        """Create a filter operation from a string.

        Args:
            op: One of "eq", "ne", "lt", "le", "gt", "ge"
                Also accepts symbols: "=", "==", "!=", "<>", "<", "<=", ">", ">="
        """
        ...


class PyScalarValue:
    """Scalar values for filter predicates.

    Supports common types used in analytical queries:
      - int: Integer values (64-bit)
      - float: Floating-point values (64-bit)
      - str: UTF-8 strings
      - bool: Boolean values
      - None: NULL value
    """

    def __init__(self, value: ScalarValueType) -> None:
        """Create a scalar value.

        The type is inferred from the Python value:
          - int → Int64
          - float → Float64
          - str → Utf8
          - bool → Boolean
          - None → Null
        """
        ...


class PyPredicateFilter:
    """A predicate filter for Parquet data.

    Represents a simple comparison: column <op> value

    Example:
        >>> # age > 50
        >>> filter = PyPredicateFilter("age", "gt", 50)
        >>> # status = 'active'
        >>> filter = PyPredicateFilter("status", "eq", "active")
    """

    column: str
    op: str
    value: str

    def __init__(self, column: str, op: str, value: ScalarValueType) -> None:
        """Create a predicate filter.

        Args:
            column: Column name to filter on
            op: Comparison operation (eq, ne, lt, le, gt, ge)
            value: Value to compare against (int, float, str, bool, or None)
        """
        ...


# =============================================================================
# Phase AF: Algebraic Classification for Conflict-Free Merge
# =============================================================================

class PyOpType:
    """Algebraic operation type classification.

    Operations are classified by their algebraic properties:
    - Semilattice: Associative, commutative, idempotent (MAX, MIN, UNION, INTERSECT)
    - Abelian: Associative, commutative, has identity and inverse (ADD, MULTIPLY)
    - Generic: No special properties (OVERWRITE, CONDITIONAL, UNKNOWN)

    Conflict-free operations (semilattice and Abelian) can be automatically merged.
    """

    def __init__(self, op_type: str) -> None:
        """Create an operation type from string.

        Args:
            op_type: One of "MAX", "MIN", "UNION", "INTERSECT", "ADD",
                    "MULTIPLY", "OVERWRITE", "CONDITIONAL", "UNKNOWN"
        """
        ...

    def is_conflict_free(self) -> bool:
        """Check if this operation type can be auto-merged."""
        ...

    def is_semilattice(self) -> bool:
        """Check if this is a semilattice operation (idempotent)."""
        ...

    def is_abelian(self) -> bool:
        """Check if this is an Abelian group operation."""
        ...

    def description(self) -> str:
        """Get a description of this operation type."""
        ...

    def __str__(self) -> str: ...
    def __repr__(self) -> str: ...


class PyAlgebraicValue:
    """A value that can be algebraically merged.

    Supports various types for different merge operations:
    - Integer: For counters, timestamps, numeric comparisons
    - Float: For continuous values
    - StringSet: For tags, permissions (set operations)
    - IntSet: For ID collections
    - Boolean: For flags
    """

    def __init__(self, value: Optional[Union[int, float, bool, List[str], List[int]]]) -> None:
        """Create an algebraic value from a Python value (type inferred)."""
        ...

    @staticmethod
    def integer(value: int) -> "PyAlgebraicValue":
        """Create an integer value."""
        ...

    @staticmethod
    def float(value: float) -> "PyAlgebraicValue":
        """Create a float value."""
        ...

    @staticmethod
    def string_set(values: List[str]) -> "PyAlgebraicValue":
        """Create a string set value."""
        ...

    @staticmethod
    def int_set(values: List[int]) -> "PyAlgebraicValue":
        """Create an integer set value."""
        ...

    @staticmethod
    def boolean(value: bool) -> "PyAlgebraicValue":
        """Create a boolean value."""
        ...

    @staticmethod
    def null() -> "PyAlgebraicValue":
        """Create a null value."""
        ...

    def is_numeric(self) -> bool:
        """Check if this is a numeric type."""
        ...

    def is_set(self) -> bool:
        """Check if this is a set type."""
        ...

    def is_null(self) -> bool:
        """Check if this is null."""
        ...

    def type_name(self) -> str:
        """Get the type name."""
        ...

    def __str__(self) -> str: ...
    def __repr__(self) -> str: ...


class PyTableAlgebraicSchema:
    """Schema-level algebraic configuration for a table.

    Defines how each column should be merged when concurrent changes occur.
    """

    def __init__(self, table_name: str, default_op_type: Optional[PyOpType] = None) -> None:
        """Create a new schema for the given table."""
        ...

    @staticmethod
    def all_additive(table_name: str) -> "PyTableAlgebraicSchema":
        """Create a schema where all columns use additive merge."""
        ...

    @staticmethod
    def all_max(table_name: str) -> "PyTableAlgebraicSchema":
        """Create a schema where all columns use max merge."""
        ...

    def add_column(self, column: str, op_type: PyOpType) -> None:
        """Add a column with the specified operation type."""
        ...

    def get_op_type(self, column: str) -> PyOpType:
        """Get the operation type for a column."""
        ...

    def is_fully_conflict_free(self) -> bool:
        """Check if all columns can be auto-merged."""
        ...

    def conflict_free_columns(self) -> List[str]:
        """Get list of columns that can be auto-merged."""
        ...

    def conflicting_columns(self) -> List[str]:
        """Get list of columns that may conflict."""
        ...


class PyAlgebraicSchemaRegistry:
    """Registry for table algebraic schemas.

    Provides centralized lookup for schemas across all tables.
    """

    def __init__(self) -> None:
        """Create an empty registry."""
        ...

    def register(self, schema: PyTableAlgebraicSchema) -> None:
        """Register a schema for a table."""
        ...

    def get(self, table: str) -> Optional[PyTableAlgebraicSchema]:
        """Get the schema for a table."""
        ...

    def get_op_type(self, table: str, column: str) -> PyOpType:
        """Get the operation type for a table/column."""
        ...

    def has_table(self, table: str) -> bool:
        """Check if a table is registered."""
        ...

    def tables(self) -> List[str]:
        """Get all registered table names."""
        ...


class PyMergeAnalysis:
    """Result of analyzing merge compatibility between branches."""

    auto_mergeable: List[str]
    conflicting: List[str]
    source_only: List[str]
    target_only: List[str]
    unchanged: List[str]

    def can_merge(self) -> bool:
        """Check if merge can proceed without conflicts."""
        ...

    def tables_to_merge(self) -> List[str]:
        """Get all tables that need merging."""
        ...


class PyMergeOutcome:
    """Outcome of an algebraic merge operation."""

    source_branch: str
    target_branch: str
    fast_forwarded: List[str]
    algebraically_merged: List[str]
    conflicts: List[str]
    success: bool
    description: Optional[str]


def algebraic_merge(
    op_type: PyOpType,
    value1: PyAlgebraicValue,
    value2: PyAlgebraicValue,
) -> PyAlgebraicValue:
    """Merge two values using the specified algebraic operation.

    Args:
        op_type: Operation type (PyOpType object)
        value1: First value
        value2: Second value

    Returns:
        Merged value if successful

    Raises:
        ValueError: If merge fails (conflict or type mismatch)
    """
    ...


def analyze_merge(
    registry: PyAlgebraicSchemaRegistry,
    source_branch: PyBranch,
    target_branch: PyBranch,
) -> PyMergeAnalysis:
    """Analyze merge compatibility between two branches.

    Args:
        registry: Schema registry with algebraic annotations
        source_branch: Branch to merge from
        target_branch: Branch to merge into

    Returns:
        Analysis indicating which tables can be auto-merged
    """
    ...
