"""
Rhizo - Data, connected. SQL queries over versioned, content-addressable data.

This module provides:
- TableWriter: Write DataFrames as chunked Parquet files
- TableReader: Read and assemble tables from chunks
- QueryEngine: SQL interface with time travel support (DuckDB-based)
- OLAPEngine: High-performance analytical queries (DataFusion-based)
- CacheManager: LRU cache for Arrow tables
- TransactionContext: ACID transactions across multiple tables
- Subscriber: Stream changelog events with polling or callbacks
- ChangeEvent: Individual table change within a transaction
- ExportEngine: Export tables to Parquet, CSV, or JSON
- ExportResult: Metadata from an export operation
- Filter: Predicate filter builder for pushdown optimization

Low-level types (from _rhizo):
- PyChunkStore: Content-addressable chunk storage
- PyCatalog: Table version catalog
- PyBranchManager: Git-like branching
- PyTransactionManager: Cross-table ACID transactions
- PyMerkleConfig, merkle_build_tree, merkle_diff_trees, merkle_verify_tree: Merkle tree operations
- PyParquetEncoder, PyParquetDecoder: High-performance Parquet I/O
- PyPredicateFilter: Predicate pushdown filters
- PyOpType, PyAlgebraicValue: Algebraic merge types
- PyTableAlgebraicSchema, PyAlgebraicSchemaRegistry: Schema-level merge configuration
"""

from .writer import TableWriter
from .database import Database, open
from .reader import TableReader, Filter
from .engine import QueryEngine
from .export import ExportEngine, ExportResult
from .transaction import TransactionContext
from .subscriber import Subscriber, ChangeEvent
from .cache import CacheManager, CacheKey, CacheStats
from .olap_engine import OLAPEngine, is_datafusion_available
from .metrics import (
    AlgebraicSignature,
    CommitMetrics,
    OperationClassifier,
    MetricsCollector,
    InstrumentedWriter,
    run_validation_benchmark,
)
from .exceptions import (
    RhizoError,
    TableNotFoundError,
    VersionNotFoundError,
    EmptyResultError,
    SizeLimitExceededError,
)

# Re-export low-level types from _rhizo for convenience
from _rhizo import (
    PyChunkStore,
    PyCatalog,
    PyBranchManager,
    PyTransactionManager,
    PyTableVersion,
    PyBranch,
    PyBranchDiff,
    PyMerkleConfig,
    PyMerkleTree,
    PyMerkleDiff,
    PyDataChunk,
    PyMerkleNode,
    merkle_build_tree,
    merkle_diff_trees,
    merkle_verify_tree,
    PyParquetEncoder,
    PyParquetDecoder,
    PyPredicateFilter,
    PyFilterOp,
    PyScalarValue,
    PyChangelogEntry,
    PyTableChange,
    PyTransactionInfo,
    PyRecoveryReport,
    # Algebraic types
    PyOpType,
    PyAlgebraicValue,
    PyTableAlgebraicSchema,
    PyAlgebraicSchemaRegistry,
    PyMergeAnalysis,
    PyMergeOutcome,
    algebraic_merge,
    # Distributed types (coordination-free transactions)
    PyNodeId,
    PyCausalOrder,
    PyVectorClock,
    # Local commit protocol (coordination-free transactions)
    PyAlgebraicOperation,
    PyAlgebraicTransaction,
    PyVersionedUpdate,
    PyLocalCommitProtocol,
)

# Simulation types (optional — only available when compiled with simulation support)
try:
    from _rhizo import (
        PyNetworkCondition,
        PySimulationConfig,
        PySimulationStats,
        PySimulatedNode,
        PySimulatedCluster,
        PySimulationBuilder,
    )
except ImportError:
    pass

def export(
    db_path: str,
    table_name: str,
    output_path: str,
    *,
    version=None,
    columns=None,
    format=None,
    compression=None,
) -> ExportResult:
    """
    Export a table from a Rhizo database to a file.

    Convenience function that opens the database, exports, and closes.
    For repeated exports, prefer using ``rhizo.open()`` and ``db.export()``.

    Args:
        db_path: Path to the Rhizo database directory.
        table_name: Name of the table to export.
        output_path: Output file path (.parquet, .csv, .json, etc.).
        version: Table version to export (None for latest).
        columns: Columns to include (None for all).
        format: Explicit format override ('parquet', 'csv', 'json').
        compression: Parquet compression codec (default: 'zstd').

    Returns:
        ExportResult with path, row_count, file_size_bytes, etc.

    Example:
        >>> rhizo.export("./mydata", "users", "users.parquet")
        >>> rhizo.export("./mydata", "users", "backup.csv", version=3)
    """
    with open(db_path) as db:
        return db.export(
            table_name, output_path,
            version=version, columns=columns,
            format=format, compression=compression,
        )


try:
    from ._version import version as __version__
except ImportError:
    __version__ = "0.0.0+unknown"  # Fallback for editable installs without build
__all__ = [
    # High-level API
    "open",
    "export",
    "Database",
    "TableWriter",
    "TableReader",
    "QueryEngine",
    "ExportEngine",
    "ExportResult",
    "OLAPEngine",
    "CacheManager",
    "CacheKey",
    "CacheStats",
    "is_datafusion_available",
    "TransactionContext",
    "Subscriber",
    "ChangeEvent",
    "Filter",
    # Metrics & Instrumentation (Coordination Bounds Validation)
    "AlgebraicSignature",
    "CommitMetrics",
    "OperationClassifier",
    "MetricsCollector",
    "InstrumentedWriter",
    "run_validation_benchmark",
    # Exceptions
    "RhizoError",
    "TableNotFoundError",
    "VersionNotFoundError",
    "EmptyResultError",
    "SizeLimitExceededError",
    # Low-level types
    "PyChunkStore",
    "PyCatalog",
    "PyBranchManager",
    "PyTransactionManager",
    "PyTableVersion",
    "PyBranch",
    "PyBranchDiff",
    "PyMerkleConfig",
    "PyMerkleTree",
    "PyMerkleDiff",
    "PyDataChunk",
    "PyMerkleNode",
    "merkle_build_tree",
    "merkle_diff_trees",
    "merkle_verify_tree",
    "PyParquetEncoder",
    "PyParquetDecoder",
    "PyPredicateFilter",
    "PyFilterOp",
    "PyScalarValue",
    "PyChangelogEntry",
    "PyTableChange",
    "PyTransactionInfo",
    "PyRecoveryReport",
    # Algebraic types
    "PyOpType",
    "PyAlgebraicValue",
    "PyTableAlgebraicSchema",
    "PyAlgebraicSchemaRegistry",
    "PyMergeAnalysis",
    "PyMergeOutcome",
    "algebraic_merge",
    # Distributed types
    "PyNodeId",
    "PyCausalOrder",
    "PyVectorClock",
    # Local commit protocol
    "PyAlgebraicOperation",
    "PyAlgebraicTransaction",
    "PyVersionedUpdate",
    "PyLocalCommitProtocol",
    # Simulation types
    "PyNetworkCondition",
    "PySimulationConfig",
    "PySimulationStats",
    "PySimulatedNode",
    "PySimulatedCluster",
    "PySimulationBuilder",
]
