"""
Database - Simple, high-level interface for Rhizo.

This module provides an easy-to-use API for common operations:

    import rhizo

    db = rhizo.open("./mydata")
    db.write("users", df)
    result = db.sql("SELECT * FROM users WHERE age > 21")
    db.close()

For advanced features (branching, transactions, OLAP), use QueryEngine directly.
"""

from __future__ import annotations

import os
import warnings
from pathlib import Path
from typing import TYPE_CHECKING, Optional, Union, Dict, List, Any

from .export import ExportEngine, ExportResult
from .diff import DiffEngine, DiffResult, SchemaDiff, RowDiff
from .gc import GCPolicy, GCResult, GarbageCollector, AutoGC

import pyarrow as pa

from .engine import QueryEngine, QueryResult
from .writer import WriteResult
try:
    from .olap_engine import OLAPEngine, DATAFUSION_AVAILABLE
except ImportError:
    DATAFUSION_AVAILABLE = False
    OLAPEngine = None  # type: ignore

if TYPE_CHECKING:
    import pandas as pd

# Default integrity verification: True for safety, override with RHIZO_VERIFY_INTEGRITY=false
_DEFAULT_VERIFY_INTEGRITY = os.environ.get("RHIZO_VERIFY_INTEGRITY", "true").lower() != "false"


class Database:
    """
    High-level Rhizo database interface.

    Provides simple methods for common operations while handling all the
    infrastructure setup automatically. For advanced features like branching,
    transactions, and OLAP queries, access the underlying engine directly.

    Example:
        >>> import rhizo
        >>> import pandas as pd
        >>>
        >>> # Open or create a database
        >>> db = rhizo.open("./mydata")
        >>>
        >>> # Write data
        >>> df = pd.DataFrame({"id": [1, 2, 3], "name": ["Alice", "Bob", "Carol"]})
        >>> db.write("users", df)
        >>>
        >>> # Query with SQL
        >>> result = db.sql("SELECT * FROM users WHERE id > 1")
        >>> print(result.to_pandas())
        >>>
        >>> # Time travel - query old version
        >>> old_result = db.sql("SELECT * FROM users", versions={"users": 1})
        >>>
        >>> # Clean up
        >>> db.close()

    Note:
        Use as a context manager for automatic cleanup:

        >>> with rhizo.open("./mydata") as db:
        ...     db.write("users", df)
        ...     result = db.sql("SELECT * FROM users")
    """

    def __init__(
        self,
        path: str,
        *,
        enable_branches: bool = True,
        enable_transactions: bool = True,
        verify_integrity: bool = _DEFAULT_VERIFY_INTEGRITY,
        auto_gc: Optional[GCPolicy] = None,
        auto_gc_interval: float = 3600.0,
    ):
        """
        Initialize a Database at the given path.

        Prefer using rhizo.open() instead of calling this directly.

        Args:
            path: Directory path for the database. Created if it doesn't exist.
            enable_branches: Enable git-like branching (default: True)
            enable_transactions: Enable ACID transactions (default: True)
            verify_integrity: Verify chunk hashes on read (default: True for safety).
                             Set to False for faster reads in trusted environments.
                             Override default via RHIZO_VERIFY_INTEGRITY env var.
            auto_gc: If set, run background GC with this policy (default: None).
            auto_gc_interval: Seconds between auto-GC runs (default: 3600).
        """
        self._path = Path(path).resolve()
        self._closed = False

        # Create directory structure
        self._path.mkdir(parents=True, exist_ok=True)
        chunks_dir = self._path / "chunks"
        catalog_dir = self._path / "catalog"
        branches_dir = self._path / "branches"
        transactions_dir = self._path / "transactions"

        chunks_dir.mkdir(exist_ok=True)
        catalog_dir.mkdir(exist_ok=True)

        # Import low-level components
        from _rhizo import PyChunkStore, PyCatalog

        # Initialize core components
        self._store = PyChunkStore(str(chunks_dir))
        self._catalog = PyCatalog(str(catalog_dir))

        # Optional branch manager
        self._branch_manager = None
        if enable_branches:
            branches_dir.mkdir(exist_ok=True)
            from _rhizo import PyBranchManager
            self._branch_manager = PyBranchManager(str(branches_dir))
            # Ensure main branch exists
            try:
                self._branch_manager.get("main")
            except OSError:
                self._branch_manager.create("main")

        # Optional transaction manager
        self._transaction_manager = None
        if enable_transactions:
            transactions_dir.mkdir(exist_ok=True)
            from _rhizo import PyTransactionManager
            self._transaction_manager = PyTransactionManager(
                str(transactions_dir),
                str(catalog_dir),
                str(branches_dir) if enable_branches else None,
                auto_recover=True,
            )

        # Table metadata store (schema evolution + primary keys)
        from rhizo.table_meta import TableMetaStore
        self._table_meta_store = TableMetaStore(str(catalog_dir))

        # Create the DuckDB query engine (fallback/compatibility)
        self._engine = QueryEngine(
            store=self._store,
            catalog=self._catalog,
            branch_manager=self._branch_manager,
            transaction_manager=self._transaction_manager,
            verify_integrity=verify_integrity,
            catalog_path=str(catalog_dir),
        )

        # Create the DataFusion OLAP engine (primary, if available)
        self._olap_engine = None
        if DATAFUSION_AVAILABLE and OLAPEngine is not None:
            self._olap_engine = OLAPEngine(
                store=self._store,
                catalog=self._catalog,
                branch_manager=self._branch_manager,
                verify_integrity=verify_integrity,
            )

        # Optional background GC
        self._auto_gc = None
        if auto_gc is not None:
            collector = GarbageCollector(
                self._catalog, self._store,
                self._branch_manager, self._transaction_manager,
            )
            self._auto_gc = AutoGC(collector, auto_gc, auto_gc_interval)
            self._auto_gc.start()

    @property
    def path(self) -> Path:
        """Get the database directory path."""
        return self._path

    @property
    def engine(self) -> QueryEngine:
        """
        Access the underlying QueryEngine for advanced features.

        Use this for:
        - Branching operations (create_branch, checkout, merge_branch)
        - Transaction context (with engine.transaction())
        - OLAP queries (olap_query, query_time_travel)
        - Changelog queries (get_changes, subscribe)

        Example:
            >>> db = rhizo.open("./mydata")
            >>> # Create a feature branch
            >>> db.engine.create_branch("experiment")
            >>> db.engine.checkout("experiment")
        """
        self._check_closed()
        return self._engine

    def sql(
        self,
        query: str,
        versions: Optional[Dict[str, int]] = None,
        params: Optional[List[Any]] = None,
    ) -> QueryResult:
        """
        Execute a SQL query using DataFusion (fast) or DuckDB (fallback).

        Uses the high-performance DataFusion OLAP engine by default.
        Falls back to DuckDB if DataFusion is not installed.

        Args:
            query: SQL query string
            versions: Optional dict mapping table names to specific versions
                     for time travel queries
            params: Optional query parameters (DuckDB fallback only)

        Returns:
            QueryResult with .to_pandas(), .to_arrow(), .to_dict() methods

        Example:
            >>> # Simple query
            >>> result = db.sql("SELECT * FROM users")
            >>>
            >>> # With time travel
            >>> result = db.sql("SELECT * FROM users", versions={"users": 1})
            >>>
            >>> # Convert to pandas
            >>> df = result.to_pandas()

        Note:
            For parameterized queries, use sql_duckdb() which supports params.
        """
        self._check_closed()

        # Use DataFusion if available (26x faster)
        if self._olap_engine is not None:
            arrow_table = self._olap_engine.query(query, versions=versions)
            return QueryResult(
                arrow_table=arrow_table,
                row_count=arrow_table.num_rows,
                column_names=arrow_table.column_names,
            )

        # Fallback to DuckDB
        return self._engine.query(query, versions=versions, params=params)

    def sql_duckdb(
        self,
        query: str,
        versions: Optional[Dict[str, int]] = None,
        params: Optional[List[Any]] = None,
    ) -> QueryResult:
        """
        Execute a SQL query using DuckDB (full SQL compatibility).

        Use this for:
        - Parameterized queries with ? placeholders
        - DuckDB-specific SQL extensions
        - When you need DuckDB's specific SQL dialect

        Args:
            query: SQL query string
            versions: Optional dict mapping table names to specific versions
            params: Optional query parameters for prepared statements

        Returns:
            QueryResult with .to_pandas(), .to_arrow(), .to_dict() methods

        Example:
            >>> # With parameters
            >>> result = db.sql_duckdb("SELECT * FROM users WHERE id = ?", params=[42])
        """
        self._check_closed()
        return self._engine.query(query, versions=versions, params=params)

    def write(
        self,
        table_name: str,
        data: Union["pd.DataFrame", pa.Table],
        metadata: Optional[Dict[str, str]] = None,
        *,
        primary_key: Optional[List[str]] = None,
        schema_mode: Optional[str] = None,
    ) -> WriteResult:
        """
        Write data as a new version of a table.

        Args:
            table_name: Name of the table (must be a valid SQL identifier)
            data: pandas DataFrame or PyArrow Table to write
            metadata: Optional key-value metadata for this version
            primary_key: Columns that form the primary key (set once, immutable).
                        Enforces uniqueness at write time.
            schema_mode: Schema evolution mode override for this write.
                        "additive" (default) or "flexible".

        Returns:
            WriteResult with version info and statistics

        Example:
            >>> import pandas as pd
            >>> df = pd.DataFrame({"id": [1, 2], "name": ["Alice", "Bob"]})
            >>> result = db.write("users", df, primary_key=["id"])
            >>> print(f"Wrote version {result.version}")
        """
        self._check_closed()
        return self._engine.write_table(
            table_name, data, metadata=metadata,
            primary_key=primary_key, schema_mode=schema_mode,
        )

    def read(
        self,
        table_name: str,
        version: Optional[int] = None,
        columns: Optional[List[str]] = None,
    ) -> pa.Table:
        """
        Read a table as an Arrow Table.

        Args:
            table_name: Name of the table to read
            version: Specific version to read (None for latest)
            columns: If specified, only read these columns (faster)

        Returns:
            PyArrow Table containing the data

        Example:
            >>> table = db.read("users")
            >>> df = table.to_pandas()
            >>>
            >>> # Read specific version
            >>> old_table = db.read("users", version=1)
            >>>
            >>> # Read only specific columns (faster)
            >>> table = db.read("users", columns=["name", "age"])
        """
        self._check_closed()
        return self._engine.reader.read_arrow(table_name, version=version, columns=columns)

    def read_pandas(
        self,
        table_name: str,
        version: Optional[int] = None,
    ) -> "pd.DataFrame":
        """
        Read a table as a pandas DataFrame.

        Args:
            table_name: Name of the table to read
            version: Specific version to read (None for latest)

        Returns:
            pandas DataFrame containing the data
        """
        self._check_closed()
        return self._engine.reader.read_pandas(table_name, version=version)

    def tables(self) -> List[str]:
        """
        List all tables in the database.

        Returns:
            List of table names
        """
        self._check_closed()
        return self._engine.list_tables()

    def versions(self, table_name: str) -> List[int]:
        """
        List all versions of a table.

        Args:
            table_name: Name of the table

        Returns:
            List of version numbers
        """
        self._check_closed()
        return self._engine.list_versions(table_name)

    def info(self, table_name: str, version: Optional[int] = None) -> Dict[str, Any]:
        """
        Get information about a table.

        Args:
            table_name: Name of the table
            version: Specific version (None for latest)

        Returns:
            Dict with table metadata including schema, row count, etc.
        """
        self._check_closed()
        return self._engine.get_table_info(table_name, version)

    def diff(
        self,
        table_name: str,
        *,
        version_a: Optional[int] = None,
        version_b: Optional[int] = None,
        branch_a: Optional[str] = None,
        branch_b: Optional[str] = None,
        key_columns: Optional[List[str]] = None,
        schema=None,
    ) -> DiffResult:
        """
        Diff two versions or branches of a table.

        Computes schema-level, row-level, and column-level diffs with
        Merkle chunk optimization. Optionally produces semantic diffs
        (e.g. "counter increased by 47") when an algebraic schema is provided.

        Args:
            table_name: Name of the table to diff.
            version_a: Old version number.
            version_b: New version number.
            branch_a: Old branch (exclusive with version_a).
            branch_b: New branch (exclusive with version_b).
            key_columns: Primary key columns for row-level diff.
                If None, only schema diff and row counts are returned.
            schema: Optional PyTableAlgebraicSchema for semantic diffs.

        Returns:
            DiffResult with schema, row, and column diffs.

        Example:
            >>> diff = db.diff("users", version_a=1, version_b=3, key_columns=["id"])
            >>> print(diff.summary())
        """
        self._check_closed()

        if (branch_a is not None or branch_b is not None) and \
           (version_a is not None or version_b is not None):
            raise ValueError("Cannot specify both version and branch parameters")

        # Resolve versions from branches
        if branch_a is not None or branch_b is not None:
            if self._branch_manager is None:
                raise RuntimeError("Branch manager not available")
            if branch_a is not None:
                version_a = self._branch_manager.get_table_version(branch_a, table_name)
                if version_a is None:
                    raise ValueError(f"Table '{table_name}' not found on branch '{branch_a}'")
            if branch_b is not None:
                version_b = self._branch_manager.get_table_version(branch_b, table_name)
                if version_b is None:
                    raise ValueError(f"Table '{table_name}' not found on branch '{branch_b}'")

        # Default: latest vs previous
        if version_a is None and version_b is None:
            versions = self._catalog.list_versions(table_name)
            if len(versions) < 2:
                raise ValueError(f"Table '{table_name}' has fewer than 2 versions")
            version_b = versions[-1]
            version_a = versions[-2]
        elif version_a is None:
            version_a = version_b - 1
        elif version_b is None:
            versions = self._catalog.list_versions(table_name)
            version_b = versions[-1]

        if not hasattr(self, "_diff_engine") or self._diff_engine is None:
            self._diff_engine = DiffEngine(
                self._catalog, self._store, self._engine.reader,
                self._branch_manager,
                table_meta_store=self._table_meta_store,
            )

        return self._diff_engine.diff(
            table_name, version_a, version_b,
            key_columns=key_columns, schema=schema,
        )

    # =========================================================================
    # Schema & Primary Key API
    # =========================================================================

    def schema(
        self,
        table_name: str,
        version: Optional[int] = None,
    ) -> pa.Schema:
        """
        Get the Arrow schema for a table.

        Args:
            table_name: Name of the table.
            version: Specific version (None for latest).

        Returns:
            PyArrow Schema.

        Raises:
            TableNotFoundError: If the table does not exist.
        """
        self._check_closed()
        from rhizo.schema_utils import SCHEMA_METADATA_KEY, deserialize_schema
        tv = self._catalog.get_version(table_name, version)
        schema_json = tv.metadata.get(SCHEMA_METADATA_KEY)
        if schema_json:
            return deserialize_schema(schema_json)
        # Fallback: read data to get schema
        table = self.read(table_name, version=tv.version)
        return table.schema

    def schema_history(self, table_name: str) -> List[Dict]:
        """
        Get the schema for each version with change tracking.

        Returns:
            List of dicts with 'version', 'schema' (field dict), and 'changes'.
        """
        self._check_closed()
        from rhizo.schema_utils import (
            SCHEMA_METADATA_KEY,
            deserialize_schema,
            compare_schemas,
        )

        versions = self._catalog.list_versions(table_name)
        history = []
        prev_schema = None

        for v in sorted(versions):
            tv = self._catalog.get_version(table_name, v)
            schema_json = tv.metadata.get(SCHEMA_METADATA_KEY)
            if schema_json:
                schema = deserialize_schema(schema_json)
            else:
                schema = None

            entry = {
                "version": v,
                "schema": (
                    {f.name: str(f.type) for f in schema}
                    if schema else None
                ),
                "changes": None,
            }

            if prev_schema is not None and schema is not None:
                cmp = compare_schemas(prev_schema, schema, "flexible")
                entry["changes"] = {
                    "added": cmp.added_columns,
                    "removed": cmp.removed_columns,
                    "type_changes": cmp.type_changes,
                }
            prev_schema = schema
            history.append(entry)

        return history

    def primary_key(self, table_name: str) -> Optional[List[str]]:
        """Get the primary key columns for a table, or None if not set."""
        self._check_closed()
        meta = self._table_meta_store.load(table_name)
        return meta.primary_key

    def set_primary_key(self, table_name: str, columns: List[str]) -> None:
        """Set the primary key for a table. Immutable once set.

        Args:
            table_name: Name of the table.
            columns: List of column names forming the primary key.

        Raises:
            ValueError: If primary key is already set to different columns.
        """
        self._check_closed()
        meta = self._table_meta_store.load(table_name)
        if meta.primary_key is not None and meta.primary_key != columns:
            raise ValueError(
                f"Primary key already set to {meta.primary_key} for '{table_name}'. "
                f"Primary keys are immutable once set."
            )
        meta.primary_key = columns
        self._table_meta_store.save(table_name, meta)

    def set_schema_mode(self, table_name: str, mode: str) -> None:
        """Set the schema evolution mode for a table.

        Args:
            table_name: Name of the table.
            mode: "additive" (new columns only) or "flexible" (any change).

        Raises:
            ValueError: If mode is invalid.
        """
        self._check_closed()
        if mode not in ("additive", "flexible"):
            raise ValueError(
                f"Invalid schema_mode '{mode}': must be 'additive' or 'flexible'"
            )
        meta = self._table_meta_store.load(table_name)
        meta.schema_mode = mode
        self._table_meta_store.save(table_name, meta)

    def gc(
        self,
        *,
        max_age_seconds: Optional[float] = None,
        max_versions_per_table: Optional[int] = None,
    ) -> GCResult:
        """
        Run garbage collection to reclaim disk space.

        Deletes old table versions and sweeps unreferenced chunks.
        At least one policy constraint must be provided.

        Safety: Never deletes the latest version, versions referenced by
        branches, or versions referenced by active transactions.

        Args:
            max_age_seconds: Delete versions older than this (seconds).
            max_versions_per_table: Keep at most this many versions per table.

        Returns:
            GCResult with counts of deleted versions, chunks, and bytes freed.

        Example:
            >>> result = db.gc(max_versions_per_table=5)
            >>> print(f"Freed {result.bytes_freed} bytes")
        """
        self._check_closed()
        policy = GCPolicy(
            max_age_seconds=max_age_seconds,
            max_versions_per_table=max_versions_per_table,
        )
        collector = GarbageCollector(
            self._catalog, self._store,
            self._branch_manager, self._transaction_manager,
        )
        return collector.collect(policy)

    def export(
        self,
        table_name: str,
        path: str,
        *,
        version: Optional[int] = None,
        columns: Optional[List[str]] = None,
        format: Optional[str] = None,
        compression: Optional[str] = None,
    ) -> ExportResult:
        """
        Export a table to Parquet, CSV, or JSON.

        The format is auto-detected from the file extension unless
        explicitly specified with the ``format`` parameter.

        Args:
            table_name: Name of the table to export.
            path: Output file path. Recognized extensions:
                  .parquet, .pq, .csv, .json, .jsonl, .ndjson
            version: Specific version to export (None for latest).
            columns: Export only these columns (None for all).
            format: Explicit format override ('parquet', 'csv', 'json').
            compression: Parquet compression codec (default: 'zstd').
                        Ignored for CSV and JSON.

        Returns:
            ExportResult with path, row_count, file_size_bytes, etc.

        Raises:
            IOError: If table or version doesn't exist.
            ValueError: If format is unsupported or columns are invalid.
            RuntimeError: If the database has been closed.

        Example:
            >>> db.export("users", "users.parquet")
            >>> db.export("users", "backup.csv", version=3)
            >>> db.export("users", "subset.parquet", columns=["id", "name"])
        """
        self._check_closed()
        if not hasattr(self, "_export_engine") or self._export_engine is None:
            self._export_engine = ExportEngine(
                self._engine.reader, self._store, self._catalog
            )
        return self._export_engine.export_table(
            table_name, path,
            version=version, columns=columns,
            format=format, compression=compression,
        )

    def close(self) -> None:
        """
        Close the database connection.

        After closing, no further operations are allowed.
        """
        if not self._closed:
            if self._auto_gc is not None:
                self._auto_gc.stop()
            self._engine.close()
            self._closed = True

    def _check_closed(self) -> None:
        """Raise an error if the database has been closed."""
        if self._closed:
            raise RuntimeError("Database has been closed")

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    def __del__(self) -> None:
        if not getattr(self, "_closed", True):
            warnings.warn(
                f"Database('{self._path}') was not closed explicitly. "
                "Use db.close() or 'with rhizo.open(...)' to avoid resource leaks.",
                ResourceWarning,
                stacklevel=1,
            )
            try:
                self.close()
            except Exception:
                pass

    def __repr__(self) -> str:
        status = "closed" if self._closed else "open"
        engine = "DataFusion" if self._olap_engine else "DuckDB"
        return f"Database('{self._path}', {status}, engine={engine})"


def open(
    path: str,
    *,
    enable_branches: bool = True,
    enable_transactions: bool = True,
    verify_integrity: bool = _DEFAULT_VERIFY_INTEGRITY,
    auto_gc: Optional[GCPolicy] = None,
    auto_gc_interval: float = 3600.0,
) -> Database:
    """
    Open or create a Rhizo database at the given path.

    This is the main entry point for using Rhizo. It creates all necessary
    directories and initializes the storage system automatically.

    Args:
        path: Directory path for the database. Created if it doesn't exist.
        enable_branches: Enable git-like branching for data (default: True)
        enable_transactions: Enable ACID transactions (default: True)
        verify_integrity: Verify chunk hashes on every read (default: True for safety).
                         Set to False for faster reads in trusted environments.
                         Override default via RHIZO_VERIFY_INTEGRITY env var.
        auto_gc: If set, run background GC with this policy (default: None).
        auto_gc_interval: Seconds between auto-GC runs (default: 3600).

    Returns:
        Database instance ready for use

    Example:
        >>> import rhizo
        >>> import pandas as pd
        >>>
        >>> # Open database
        >>> db = rhizo.open("./mydata")
        >>>
        >>> # Write some data
        >>> df = pd.DataFrame({
        ...     "id": [1, 2, 3],
        ...     "name": ["Alice", "Bob", "Carol"],
        ...     "score": [85, 92, 78]
        ... })
        >>> db.write("students", df)
        >>>
        >>> # Query with SQL
        >>> result = db.sql("SELECT name, score FROM students WHERE score > 80")
        >>> print(result.to_pandas())
        >>>
        >>> # Close when done
        >>> db.close()

        # Or use as context manager:
        >>> with rhizo.open("./mydata") as db:
        ...     db.write("users", df)
        ...     result = db.sql("SELECT * FROM users")

        # With automatic background GC:
        >>> with rhizo.open("./mydata", auto_gc=GCPolicy(max_versions_per_table=10)) as db:
        ...     db.write("users", df)
    """
    return Database(
        path,
        enable_branches=enable_branches,
        enable_transactions=enable_transactions,
        verify_integrity=verify_integrity,
        auto_gc=auto_gc,
        auto_gc_interval=auto_gc_interval,
    )
