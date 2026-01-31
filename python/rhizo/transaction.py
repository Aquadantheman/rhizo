"""
Transaction support for Rhizo QueryEngine.

Provides ACID transactions across multiple tables with:
- Buffered writes (not visible until commit)
- Read-your-writes within the transaction
- Automatic rollback on exceptions
- Branch-aware operations

Design Notes:
    - Writes are buffered in Python until commit
    - At commit time, writes go through TransactionManager for atomicity
    - Read-your-writes works by temporarily registering buffered data with DuckDB
    - This design supports future row-level conflict detection (Phase 5.5)

Example:
    >>> with engine.transaction() as tx:
    ...     # Read current data
    ...     users = tx.query("SELECT * FROM users")
    ...
    ...     # Buffer writes (not visible outside transaction)
    ...     tx.write_table("users", updated_users_df)
    ...     tx.write_table("audit_log", audit_df)
    ...
    ...     # Read-your-writes: sees buffered data
    ...     result = tx.query("SELECT * FROM users")
    ...
    ...     # Auto-commits on exit
    ... # Rolls back if exception raised
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional, Dict, List, Any, Union

import pyarrow as pa

from rhizo.exceptions import validate_table_name

if TYPE_CHECKING:
    import pandas as pd
    from .engine import QueryEngine, QueryResult


@dataclass
class BufferedWrite:
    """A write operation buffered within a transaction."""
    table_name: str
    data: pa.Table  # Always stored as Arrow for consistency
    metadata: Optional[Dict[str, str]] = None


class TransactionContext:
    """
    Context for operations within a transaction.

    Provides isolated read/write operations with ACID guarantees.
    Writes are buffered until commit; reads see the transaction's
    snapshot plus any buffered writes (read-your-writes).

    This class should not be instantiated directly. Use
    `QueryEngine.transaction()` to create a transaction context.

    Attributes:
        tx_id: The unique transaction identifier
        branch: The branch this transaction operates on

    Thread Safety:
        TransactionContext is NOT thread-safe. Each transaction should
        be used from a single thread. For concurrent operations, create
        separate transactions.

    Future Extensions:
        - Row-level conflict detection (Phase 5.5)
        - Distributed transactions (Phase 5.x)
        - Savepoints for partial rollback
    """

    def __init__(
        self,
        engine: "QueryEngine",
        tx_id: int,
        branch: str,
        snapshot: Dict[str, int],
    ):
        """
        Initialize a transaction context.

        Args:
            engine: The QueryEngine that created this transaction
            tx_id: Unique transaction identifier from TransactionManager
            branch: Branch this transaction operates on
            snapshot: Read snapshot mapping table names to versions
        """
        self._engine: "QueryEngine" = engine
        self._tx_id: int = tx_id
        self._branch: str = branch
        self._snapshot: Dict[str, int] = snapshot
        self._committed: bool = False
        self._aborted: bool = False
        self._buffered_writes: Dict[str, BufferedWrite] = {}
        self._temp_tables: List[str] = []  # Track temp tables for cleanup

    @property
    def tx_id(self) -> int:
        """Get the transaction ID."""
        return self._tx_id

    @property
    def branch(self) -> str:
        """Get the branch this transaction operates on."""
        return self._branch

    @property
    def is_active(self) -> bool:
        """Check if the transaction is still active."""
        return not self._committed and not self._aborted

    def query(
        self,
        sql: str,
        params: Optional[List[Any]] = None,
    ) -> "QueryResult":
        """
        Execute a SQL query within this transaction.

        Reads see the snapshot at transaction start, plus any
        writes made within this transaction (read-your-writes).

        Args:
            sql: SQL query string
            params: Optional query parameters for prepared statements

        Returns:
            QueryResult with Arrow table and metadata

        Raises:
            RuntimeError: If transaction is no longer active
            duckdb.Error: If SQL is invalid
        """
        self._check_active("query")

        # Register any buffered writes as temporary tables for read-your-writes
        self._register_buffered_writes()

        # Extract table names from query (pure regex, no conn access)
        table_names = self._engine._extract_table_names(sql)

        with self._engine._conn_lock:
            # For tables NOT in buffered writes, ensure they're registered from snapshot
            for table_name in table_names:
                if table_name.lower() not in self._buffered_writes:
                    # Get version from snapshot
                    version = self._snapshot.get(table_name.lower())
                    if version is not None:
                        self._engine._ensure_registered(table_name, version, self._branch)
                    else:
                        # Table didn't exist when transaction started.
                        # Snapshot isolation: cannot read tables created after tx began.
                        raise ValueError(
                            f"Table '{table_name}' does not exist in transaction snapshot. "
                            f"It may have been created after this transaction started."
                        )

            # Execute query directly on DuckDB connection
            if params:
                result = self._engine._conn.execute(sql, params)
            else:
                result = self._engine._conn.execute(sql)

            arrow_table = result.fetch_arrow_table()

        # Import QueryResult to avoid circular import issues
        from .engine import QueryResult
        return QueryResult(
            arrow_table=arrow_table,
            row_count=arrow_table.num_rows,
            column_names=arrow_table.column_names,
        )

    def query_pandas(
        self,
        sql: str,
        params: Optional[List[Any]] = None,
    ) -> "pd.DataFrame":
        """Execute query and return pandas DataFrame."""
        return self.query(sql, params).to_pandas()

    def write_table(
        self,
        table_name: str,
        data: Union["pd.DataFrame", pa.Table],
        metadata: Optional[Dict[str, str]] = None,
    ) -> None:
        """
        Buffer a table write within this transaction.

        Writes are not visible to other transactions until commit.
        Multiple writes to the same table within a transaction
        will overwrite previous buffered writes.

        Args:
            table_name: Name of the table to write
            data: DataFrame or Arrow Table to write
            metadata: Optional metadata for this version

        Raises:
            RuntimeError: If transaction is no longer active
        """
        self._check_active("write")
        table_name = validate_table_name(table_name)

        # Import pandas for type checking
        try:
            import pandas as pd
            is_pandas = isinstance(data, pd.DataFrame)
        except ImportError:
            is_pandas = False

        # Convert to Arrow if needed
        if isinstance(data, pa.Table):
            arrow_data = data
        elif is_pandas:
            # pandas DataFrame - convert via Arrow
            arrow_data = pa.Table.from_pandas(data)
        else:
            raise TypeError(f"Expected DataFrame or Arrow Table, got {type(data)}")

        # Buffer the write
        self._buffered_writes[table_name.lower()] = BufferedWrite(
            table_name=table_name.lower(),
            data=arrow_data,
            metadata=metadata,
        )

    def commit(self) -> None:
        """
        Commit the transaction.

        Applies all buffered writes atomically. After commit,
        the transaction context cannot be used for further operations.

        Raises:
            RuntimeError: If transaction was already committed or aborted
            Exception: If commit fails (transaction will be aborted)
        """
        if self._committed:
            return  # Idempotent
        if self._aborted:
            raise RuntimeError("Cannot commit aborted transaction")

        # Transaction manager is guaranteed non-None (checked in QueryEngine.transaction)
        tx_manager = self._engine.transaction_manager
        assert tx_manager is not None, "TransactionContext requires transaction_manager"

        try:
            # Write chunks (without catalog commit) and register with tx manager
            for table_name, buffered in self._buffered_writes.items():
                # Write chunks only - don't commit to catalog yet
                result = self._engine.writer.write_chunks_only(
                    table_name,
                    buffered.data,
                )

                # Record write with transaction manager
                # The TransactionManager will handle catalog commit on tx.commit()
                tx_manager.add_write(
                    self._tx_id,
                    table_name,
                    result.next_version,
                    result.chunk_hashes,
                )

            # Commit through transaction manager
            # This validates conflicts, applies writes to catalog,
            # and updates branch heads atomically
            tx_manager.commit(self._tx_id)
            self._committed = True

        except Exception:
            # If anything fails, abort the transaction
            self.abort("Commit failed")
            raise
        finally:
            self._cleanup_temp_tables()

    def abort(self, reason: Optional[str] = None) -> None:
        """
        Abort the transaction.

        Discards all buffered writes. After abort, the transaction
        context cannot be used for further operations.

        Args:
            reason: Optional reason for the abort (for logging/debugging)

        Raises:
            RuntimeError: If transaction was already committed
        """
        if self._committed:
            raise RuntimeError("Cannot abort committed transaction")
        if self._aborted:
            return  # Idempotent

        try:
            # Transaction manager is guaranteed non-None (checked in QueryEngine.transaction)
            tx_manager = self._engine.transaction_manager
            assert tx_manager is not None, "TransactionContext requires transaction_manager"

            tx_manager.abort(
                self._tx_id,
                reason or "User requested",
            )
            self._aborted = True
        finally:
            # Always clean up temp tables, even if tx_manager.abort() fails
            self._cleanup_temp_tables()

    def _check_active(self, operation: str) -> None:
        """Raise if transaction is not active."""
        if self._committed:
            raise RuntimeError(f"Cannot {operation}: transaction already committed")
        if self._aborted:
            raise RuntimeError(f"Cannot {operation}: transaction was aborted")

    def _register_buffered_writes(self) -> None:
        """
        Register buffered writes with DuckDB for read-your-writes.

        This allows queries within the transaction to see uncommitted
        writes. The tables are registered with a special naming scheme
        and cleaned up on commit/abort.

        If registration fails partway through, any tables that were
        successfully registered are tracked for cleanup.
        """
        for table_name, buffered in self._buffered_writes.items():
            # Track the table BEFORE registering so cleanup happens
            # even if registration fails partway through
            if table_name not in self._temp_tables:
                self._temp_tables.append(table_name)

            try:
                # Register the buffered data, replacing any existing registration
                # The engine's _ensure_registered will use this instead of disk
                with self._engine._conn_lock:
                    self._engine._conn.register(table_name, buffered.data)
            except Exception:
                # Registration failed - but table is already tracked for cleanup
                # Re-raise to let caller handle the error
                raise

    def _cleanup_temp_tables(self) -> None:
        """Unregister any temporary tables created for read-your-writes."""
        with self._engine._conn_lock:
            for table_name in self._temp_tables:
                try:
                    self._engine._conn.unregister(table_name)
                except Exception:
                    pass  # Ignore cleanup errors

        # Invalidate engine's cache for tables we wrote to
        # (they may need to be reloaded with committed versions)
        # Must happen BEFORE clearing _buffered_writes
        for table_name in list(self._buffered_writes.keys()):
            self._engine._invalidate_cache(table_name)

        self._temp_tables.clear()
        self._buffered_writes.clear()

    def __enter__(self) -> "TransactionContext":
        """
        Enter the transaction context.

        Returns:
            self for use in with statements
        """
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """
        Exit the transaction context with proper cleanup.

        On successful exit (no exception), commits if still active.
        On exception, aborts if still active.
        Always ensures temp tables are cleaned up.
        """
        try:
            if exc_type is not None:
                # Exception occurred - abort if active
                if self.is_active:
                    self.abort(f"Exception: {exc_val}")
            else:
                # Normal exit - commit if active
                if self.is_active:
                    self.commit()
        finally:
            # Defensive cleanup: ensure temp tables are always cleaned up
            # even if commit/abort failed in an unexpected way
            self._cleanup_temp_tables()
