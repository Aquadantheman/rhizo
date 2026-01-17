"""
QueryEngine - SQL queries over versioned data with time travel and branching.

The engine provides:
1. SQL interface via DuckDB
2. Time travel - query any historical version
3. Git-like branching - isolated workspaces for data development
4. Cross-table ACID transactions with snapshot isolation
5. Table registration caching for performance
6. Multiple result formats (Arrow, pandas, dict)
"""

from __future__ import annotations

import re
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional, Dict, List, Any, Union, Generator

import duckdb
import pyarrow as pa

from .reader import TableReader
from .writer import TableWriter, WriteResult

if TYPE_CHECKING:
    import pandas as pd
    import armillaria
    from .transaction import TransactionContext
    from .subscriber import Subscriber


@dataclass
class RegisteredTable:
    """Tracks a table registered with DuckDB."""
    table_name: str
    version: int
    arrow_table: pa.Table


@dataclass
class QueryResult:
    """Result of a SQL query."""
    arrow_table: pa.Table
    row_count: int
    column_names: List[str]

    def to_pandas(self) -> "pd.DataFrame":
        """Convert result to pandas DataFrame."""
        return self.arrow_table.to_pandas()

    def to_dict(self) -> List[Dict[str, Any]]:
        """Convert result to list of dictionaries."""
        return self.arrow_table.to_pylist()

    def to_arrow(self) -> pa.Table:
        """Return as Arrow Table."""
        return self.arrow_table


class QueryEngine:
    """
    SQL query engine with time travel and branching support.

    Provides a DuckDB-based SQL interface over versioned Armillaria tables.
    Tables are loaded on-demand and cached for performance.

    Example:
        >>> from armillaria import PyChunkStore, PyCatalog, PyBranchManager
        >>> from armillaria_query import QueryEngine
        >>>
        >>> store = PyChunkStore("./data/chunks")
        >>> catalog = PyCatalog("./data/catalog")
        >>> branches = PyBranchManager("./data/branches")
        >>> engine = QueryEngine(store, catalog, branch_manager=branches)
        >>>
        >>> # Query latest version on current branch (main by default)
        >>> result = engine.query("SELECT * FROM users WHERE age > 21")
        >>> df = result.to_pandas()
        >>>
        >>> # Switch to a feature branch
        >>> engine.checkout("feature/new-scoring")
        >>>
        >>> # Query on a specific branch without switching
        >>> result = engine.query(
        ...     "SELECT * FROM users",
        ...     branch="feature/experiment"
        ... )
        >>>
        >>> # Time travel - query version 5 of users table (overrides branch)
        >>> result = engine.query(
        ...     "SELECT * FROM users WHERE age > 21",
        ...     versions={"users": 5}
        ... )

    Note:
        Table names in queries are case-insensitive but stored lowercase.

    Version Resolution Order:
        1. Explicit version in `versions` dict (highest priority)
        2. Branch head pointer (from `branch` param or current_branch)
        3. Catalog latest (fallback for backward compatibility)
    """

    def __init__(
        self,
        store: "armillaria.PyChunkStore",
        catalog: "armillaria.PyCatalog",
        verify_integrity: bool = False,
        branch_manager: Optional["armillaria.PyBranchManager"] = None,
        transaction_manager: Optional["armillaria.PyTransactionManager"] = None,
    ):
        """
        Initialize the QueryEngine.

        Args:
            store: PyChunkStore instance for content-addressable storage
            catalog: PyCatalog instance for version metadata
            verify_integrity: If True, verify chunk hashes on read
            branch_manager: Optional PyBranchManager for branch-aware queries.
                           If None, operates in branchless mode (backward compatible).
            transaction_manager: Optional PyTransactionManager for ACID transactions.
                                If None, transaction() will raise RuntimeError.
        """
        self.store = store
        self.catalog = catalog
        self.reader = TableReader(store, catalog, verify_integrity)
        self.writer = TableWriter(store, catalog)
        self.branch_manager = branch_manager
        self.transaction_manager = transaction_manager

        # Current branch (only used if branch_manager is provided)
        self._current_branch: str = "main"

        # DuckDB connection (in-memory)
        self._conn = duckdb.connect(":memory:")

        # Cache of registered tables: (table_name, version) -> RegisteredTable
        self._registered: Dict[tuple, RegisteredTable] = {}

        # Active transaction tracking (for nested transaction prevention)
        self._active_tx: Optional[int] = None

    def query(
        self,
        sql: str,
        versions: Optional[Dict[str, int]] = None,
        params: Optional[List[Any]] = None,
        branch: Optional[str] = None,
    ) -> QueryResult:
        """
        Execute a SQL query with optional time travel and branching.

        Args:
            sql: SQL query string
            versions: Dict mapping table names to specific versions.
                      If a table is not in this dict, branch head or latest is used.
            params: Optional query parameters for prepared statements
            branch: Branch to query from. If None, uses current_branch.
                   Only used if branch_manager is configured.

        Returns:
            QueryResult with Arrow table and metadata

        Raises:
            IOError: If a referenced table doesn't exist
            duckdb.Error: If SQL is invalid

        Example:
            >>> # Query latest on current branch
            >>> engine.query("SELECT * FROM users")
            >>>
            >>> # Query on specific branch
            >>> engine.query("SELECT * FROM users", branch="feature/test")
            >>>
            >>> # Time travel (overrides branch)
            >>> engine.query("SELECT * FROM users", versions={"users": 5})
            >>>
            >>> # Multiple tables at different versions
            >>> engine.query(
            ...     "SELECT u.name, o.total FROM users u JOIN orders o ON u.id = o.user_id",
            ...     versions={"users": 5, "orders": 10}
            ... )
        """
        versions = versions or {}
        effective_branch = branch or self._current_branch

        # Extract table names from query
        table_names = self._extract_table_names(sql)

        # Register each table with the appropriate version
        for table_name in table_names:
            version = versions.get(table_name)  # None means resolve via branch/latest
            self._ensure_registered(table_name, version, effective_branch)

        # Execute query
        if params:
            result = self._conn.execute(sql, params)
        else:
            result = self._conn.execute(sql)

        arrow_table = result.fetch_arrow_table()

        return QueryResult(
            arrow_table=arrow_table,
            row_count=arrow_table.num_rows,
            column_names=arrow_table.column_names,
        )

    def query_pandas(
        self,
        sql: str,
        versions: Optional[Dict[str, int]] = None,
        params: Optional[List[Any]] = None,
        branch: Optional[str] = None,
    ) -> "pd.DataFrame":
        """Execute query and return pandas DataFrame."""
        return self.query(sql, versions, params, branch).to_pandas()

    def query_arrow(
        self,
        sql: str,
        versions: Optional[Dict[str, int]] = None,
        params: Optional[List[Any]] = None,
        branch: Optional[str] = None,
    ) -> pa.Table:
        """Execute query and return Arrow Table."""
        return self.query(sql, versions, params, branch).to_arrow()

    def write_table(
        self,
        table_name: str,
        data: Union["pd.DataFrame", pa.Table],
        metadata: Optional[Dict[str, str]] = None,
        branch: Optional[str] = None,
    ) -> WriteResult:
        """
        Write data as a new version of a table.

        Convenience method that wraps TableWriter. If a branch_manager is
        configured, also updates the branch head pointer.

        Args:
            table_name: Name of the table
            data: DataFrame or Arrow Table to write
            metadata: Optional metadata for this version
            branch: Branch to update. If None, uses current_branch.
                   Only used if branch_manager is configured.

        Returns:
            WriteResult with version info
        """
        result = self.writer.write(table_name, data, metadata)

        # Update branch head if branch_manager is configured
        if self.branch_manager is not None:
            effective_branch = branch or self._current_branch
            self.branch_manager.update_head(effective_branch, table_name, result.version)

        # Invalidate cache for this table (force reload on next query)
        self._invalidate_cache(table_name)

        return result

    def list_tables(self) -> List[str]:
        """List all tables in the catalog."""
        return self.reader.list_tables()

    def list_versions(self, table_name: str) -> List[int]:
        """List all versions of a table."""
        return self.reader.list_versions(table_name)

    # =========================================================================
    # Transaction Operations
    # =========================================================================

    @contextmanager
    def transaction(
        self,
        branch: Optional[str] = None,
    ) -> Generator["TransactionContext", None, None]:
        """
        Create a transaction context for atomic multi-table operations.

        Provides ACID guarantees across multiple table writes:
        - Atomicity: All writes commit together or none do
        - Consistency: Conflict detection prevents inconsistent states
        - Isolation: Snapshot isolation - reads see consistent point-in-time
        - Durability: Committed changes are persisted

        Args:
            branch: Branch to operate on. If None, uses current_branch.

        Yields:
            TransactionContext for read/write operations

        Raises:
            RuntimeError: If transaction_manager is not configured
            RuntimeError: If a transaction is already active (no nesting)

        Example:
            >>> with engine.transaction() as tx:
            ...     # Read data (sees snapshot at transaction start)
            ...     users = tx.query("SELECT * FROM users")
            ...
            ...     # Buffer writes (not visible outside transaction)
            ...     tx.write_table("users", updated_df)
            ...     tx.write_table("audit_log", audit_df)
            ...
            ...     # Read-your-writes: query sees buffered data
            ...     result = tx.query("SELECT COUNT(*) FROM users")
            ...
            ...     # Auto-commits on successful exit
            ... # Auto-rollback if exception raised

        Note:
            Nested transactions are not supported. Attempting to start
            a transaction while one is active will raise RuntimeError.

        Future Extensions:
            - Savepoints for partial rollback
            - Distributed transactions
            - Custom isolation levels
        """
        # Import here to avoid circular import
        from .transaction import TransactionContext

        if self.transaction_manager is None:
            raise RuntimeError(
                "Cannot start transaction: transaction_manager not configured. "
                "Initialize QueryEngine with a PyTransactionManager."
            )

        if self._active_tx is not None:
            raise RuntimeError(
                "Nested transactions are not supported. "
                f"Transaction {self._active_tx} is already active."
            )

        effective_branch = branch or self._current_branch

        # Begin transaction through the Rust manager
        tx_id = self.transaction_manager.begin(effective_branch)
        self._active_tx = tx_id

        # Capture read snapshot (current versions of all known tables)
        snapshot = self._capture_snapshot(effective_branch)

        # Create the transaction context
        ctx = TransactionContext(self, tx_id, effective_branch, snapshot)

        try:
            yield ctx
            # Commit on successful exit (if not already committed/aborted)
            if ctx.is_active:
                ctx.commit()
        except Exception as e:
            # Rollback on exception (if not already committed/aborted)
            if ctx.is_active:
                ctx.abort(f"Exception: {e}")
            raise
        finally:
            self._active_tx = None
            # Invalidate cache - versions may have changed
            self._registered.clear()

    def _capture_snapshot(self, branch: str) -> Dict[str, int]:
        """
        Capture the current version snapshot for a transaction.

        Returns a mapping of table names to their current versions
        on the specified branch (or catalog latest if branchless).
        """
        snapshot: Dict[str, int] = {}

        # Get all tables from catalog
        tables = self.catalog.list_tables()

        for table_name in tables:
            version = None

            # Try branch head first
            if self.branch_manager is not None:
                version = self.branch_manager.get_table_version(branch, table_name)

            # Fall back to catalog latest
            if version is None:
                try:
                    metadata = self.reader.get_metadata(table_name)
                    version = metadata.version
                except Exception:
                    continue  # Table doesn't exist yet

            snapshot[table_name.lower()] = version

        return snapshot

    # =========================================================================
    # Branch Operations
    # =========================================================================

    @property
    def current_branch(self) -> str:
        """Get the current branch name."""
        return self._current_branch

    def checkout(self, branch_name: str) -> None:
        """
        Switch to a different branch.

        Args:
            branch_name: Name of the branch to switch to

        Raises:
            IOError: If branch doesn't exist
            RuntimeError: If branch_manager is not configured
        """
        if self.branch_manager is None:
            raise RuntimeError("Cannot checkout: branch_manager not configured")

        # Verify branch exists (will raise if not found)
        self.branch_manager.get(branch_name)

        self._current_branch = branch_name

        # Invalidate all cached tables (versions may differ on new branch)
        self._registered.clear()

    def create_branch(
        self,
        name: str,
        from_branch: Optional[str] = None,
        description: Optional[str] = None,
    ) -> Any:
        """
        Create a new branch.

        Args:
            name: Name of the new branch
            from_branch: Branch to create from (default: current branch)
            description: Optional description for the branch

        Returns:
            The created branch object

        Raises:
            RuntimeError: If branch_manager is not configured
            ValueError: If branch already exists
        """
        if self.branch_manager is None:
            raise RuntimeError("Cannot create branch: branch_manager not configured")

        source = from_branch or self._current_branch
        return self.branch_manager.create(name, from_branch=source, description=description)

    def list_branches(self) -> List[str]:
        """
        List all branch names.

        Returns:
            List of branch names

        Raises:
            RuntimeError: If branch_manager is not configured
        """
        if self.branch_manager is None:
            raise RuntimeError("Cannot list branches: branch_manager not configured")

        return self.branch_manager.list()

    def diff_branches(self, source: str, target: str) -> Dict[str, Any]:
        """
        Compare two branches.

        Args:
            source: Source branch name
            target: Target branch name

        Returns:
            Dict with comparison results including:
            - unchanged: Tables with same version on both branches
            - modified: Tables with different versions (table, source_ver, target_ver)
            - added_in_source: Tables only in source branch
            - added_in_target: Tables only in target branch
            - has_conflicts: True if branches have diverged

        Raises:
            RuntimeError: If branch_manager is not configured
        """
        if self.branch_manager is None:
            raise RuntimeError("Cannot diff branches: branch_manager not configured")

        diff = self.branch_manager.diff(source, target)

        return {
            "source_branch": diff.source_branch,
            "target_branch": diff.target_branch,
            "unchanged": diff.unchanged,
            "modified": diff.modified,
            "added_in_source": diff.added_in_source,
            "added_in_target": diff.added_in_target,
            "has_conflicts": diff.has_conflicts,
        }

    def merge_branch(self, source: str, into: Optional[str] = None) -> None:
        """
        Merge source branch into target branch (fast-forward only).

        Args:
            source: Branch to merge from
            into: Branch to merge into (default: current branch)

        Raises:
            RuntimeError: If branch_manager is not configured
            ValueError: If merge conflict (branches have diverged)
        """
        if self.branch_manager is None:
            raise RuntimeError("Cannot merge: branch_manager not configured")

        target = into or self._current_branch
        self.branch_manager.merge(source, into=target)

        # Invalidate cache if we merged into current branch
        if target == self._current_branch:
            self._registered.clear()

    def get_table_info(self, table_name: str, version: Optional[int] = None) -> Dict[str, Any]:
        """
        Get information about a table version.

        Args:
            table_name: Name of the table
            version: Specific version (None for latest)

        Returns:
            Dict with table metadata and schema info
        """
        metadata = self.reader.get_metadata(table_name, version)

        # Load schema from first chunk
        arrow_table = self.reader.read_arrow(table_name, version)

        return {
            "table_name": metadata.table_name,
            "version": metadata.version,
            "chunk_count": metadata.chunk_count,
            "row_count": arrow_table.num_rows,
            "created_at": metadata.created_at,
            "parent_version": metadata.parent_version,
            "schema": {
                field.name: str(field.type)
                for field in arrow_table.schema
            },
        }

    def diff_versions(
        self,
        table_name: str,
        version_a: int,
        version_b: int,
        key_columns: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """
        Compare two versions of a table.

        Args:
            table_name: Name of the table
            version_a: First version to compare
            version_b: Second version to compare
            key_columns: Columns to use as primary key for row matching.
                        If None, compares aggregate statistics only.

        Returns:
            Dict with comparison results
        """
        table_a = self.reader.read_arrow(table_name, version_a)
        table_b = self.reader.read_arrow(table_name, version_b)

        result = {
            "table_name": table_name,
            "version_a": version_a,
            "version_b": version_b,
            "rows_a": table_a.num_rows,
            "rows_b": table_b.num_rows,
            "row_diff": table_b.num_rows - table_a.num_rows,
            "schema_changed": table_a.schema != table_b.schema,
        }

        if key_columns:
            # Detailed row-level diff using DuckDB
            # Register both versions temporarily
            self._conn.register("__diff_a", table_a)
            self._conn.register("__diff_b", table_b)

            # Find added rows (in B but not in A)
            added = self._conn.execute(f"""
                SELECT * FROM __diff_b b
                WHERE NOT EXISTS (
                    SELECT 1 FROM __diff_a a
                    WHERE {" AND ".join(f"a.{c} = b.{c}" for c in key_columns)}
                )
            """).fetch_arrow_table()

            # Find removed rows (in A but not in B)
            removed = self._conn.execute(f"""
                SELECT * FROM __diff_a a
                WHERE NOT EXISTS (
                    SELECT 1 FROM __diff_b b
                    WHERE {" AND ".join(f"a.{c} = b.{c}" for c in key_columns)}
                )
            """).fetch_arrow_table()

            result["rows_added"] = added.num_rows
            result["rows_removed"] = removed.num_rows

            # Unregister temp tables
            self._conn.unregister("__diff_a")
            self._conn.unregister("__diff_b")

        return result

    def _ensure_registered(
        self,
        table_name: str,
        version: Optional[int] = None,
        branch: Optional[str] = None,
    ) -> None:
        """
        Ensure a table is registered with DuckDB at the specified version.

        Version resolution order:
        1. Explicit version parameter (highest priority)
        2. Branch head pointer (if branch_manager configured)
        3. Catalog latest (fallback)
        """
        # Resolve version
        if version is None:
            # Try to resolve via branch head
            if self.branch_manager is not None and branch is not None:
                version = self.branch_manager.get_table_version(branch, table_name)

            # Fallback to catalog latest if branch doesn't have this table
            if version is None:
                metadata = self.reader.get_metadata(table_name)
                version = metadata.version

        cache_key = (table_name.lower(), version)

        # Check if already registered at this version
        if cache_key in self._registered:
            return

        # Load the table
        arrow_table = self.reader.read_arrow(table_name, version)

        # Unregister any previous version of this table
        self._unregister_table(table_name)

        # Register with DuckDB
        self._conn.register(table_name.lower(), arrow_table)

        # Cache the registration
        self._registered[cache_key] = RegisteredTable(
            table_name=table_name.lower(),
            version=version,
            arrow_table=arrow_table,
        )

    def _unregister_table(self, table_name: str) -> None:
        """Unregister all versions of a table from DuckDB."""
        table_lower = table_name.lower()

        # Find and remove all cached versions
        to_remove = [k for k in self._registered if k[0] == table_lower]

        for key in to_remove:
            del self._registered[key]

        # Try to unregister from DuckDB (may not exist)
        try:
            self._conn.unregister(table_lower)
        except Exception:
            pass  # Table wasn't registered

    def _invalidate_cache(self, table_name: str) -> None:
        """Invalidate cache for a table (called after writes)."""
        self._unregister_table(table_name)

    def _extract_table_names(self, sql: str) -> List[str]:
        """
        Extract table names from a SQL query.

        This is a simple regex-based approach. For production,
        consider using sqlparse or DuckDB's query plan.
        """
        # Normalize whitespace
        sql_normalized = " ".join(sql.split())

        # Patterns to match table names:
        # - FROM table_name
        # - JOIN table_name
        # - FROM table_name alias
        # - JOIN table_name alias
        patterns = [
            r'\bFROM\s+(\w+)',
            r'\bJOIN\s+(\w+)',
        ]

        table_names = set()
        for pattern in patterns:
            matches = re.findall(pattern, sql_normalized, re.IGNORECASE)
            table_names.update(m.lower() for m in matches)

        # Filter out DuckDB built-in functions/keywords that might match
        keywords = {'select', 'where', 'group', 'order', 'limit', 'offset', 'union', 'except', 'intersect'}
        table_names -= keywords

        return list(table_names)

    # =========================================================================
    # Integrity & Recovery Operations
    # =========================================================================

    def verify_integrity(self) -> Dict[str, Any]:
        """
        Verify the integrity of the UDR storage system.

        Checks:
        - Transaction log consistency (if transaction_manager configured)
        - More checks can be added in the future (chunk integrity, catalog consistency)

        Returns:
            Dict with:
                - is_healthy: True if no issues found
                - issues: List of issue descriptions
                - transaction_issues: List of transaction-specific issues (if applicable)

        Example:
            >>> result = engine.verify_integrity()
            >>> if not result["is_healthy"]:
            ...     print(f"Issues found: {result['issues']}")
        """
        result: Dict[str, Any] = {
            "is_healthy": True,
            "issues": [],
            "transaction_issues": [],
        }

        # Check transaction system consistency
        if self.transaction_manager is not None:
            try:
                tx_issues = self.transaction_manager.verify_consistency()
                if tx_issues:
                    result["is_healthy"] = False
                    result["issues"].extend(tx_issues)
                    result["transaction_issues"] = tx_issues
            except Exception as e:
                result["is_healthy"] = False
                result["issues"].append(f"Transaction verification failed: {e}")

        return result

    def recover(self, apply: bool = True) -> Dict[str, Any]:
        """
        Run recovery to clean up incomplete transactions.

        This should be called on startup after a crash or unclean shutdown.
        Scans the transaction log and either marks pending transactions
        as aborted (if apply=True) or just reports what would be done.

        Args:
            apply: If True, mark pending transactions as aborted.
                   If False, just report what would be recovered (dry run).

        Returns:
            Dict with recovery results:
                - is_clean: True if no issues found
                - replayed: List of transaction IDs that were replayed
                - rolled_back: List of transaction IDs that were rolled back
                - warnings: List of warning messages
                - errors: List of error messages

        Raises:
            RuntimeError: If transaction_manager is not configured

        Example:
            >>> # On startup
            >>> report = engine.recover()
            >>> if not report["is_clean"]:
            ...     print(f"Recovered from crash: {report}")
        """
        if self.transaction_manager is None:
            raise RuntimeError("Cannot recover: transaction_manager not configured")

        if apply:
            report = self.transaction_manager.recover_and_apply()
        else:
            report = self.transaction_manager.recover()

        return {
            "is_clean": report.is_clean,
            "last_committed_epoch": report.last_committed_epoch,
            "replayed": list(report.replayed),
            "rolled_back": list(report.rolled_back),
            "already_aborted": list(report.already_aborted),
            "already_committed": list(report.already_committed),
            "warnings": list(report.warnings),
            "errors": list(report.errors),
        }

    # =========================================================================
    # Changelog & Subscription Operations
    # =========================================================================

    def get_changes(
        self,
        since_tx_id: Optional[int] = None,
        since_timestamp: Optional[int] = None,
        tables: Optional[List[str]] = None,
        branch: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """
        Get changelog entries since a specific point.

        This is the batch interface for querying what changed:
        - Batch state query: "What is the data?" -> engine.query()
        - Batch change query: "What changed?" -> engine.get_changes()
        - Stream changes: "Notify me of changes" -> engine.subscribe()

        Args:
            since_tx_id: Start from this transaction ID (exclusive)
            since_timestamp: Start from this Unix timestamp (inclusive)
            tables: Filter to specific tables (None = all tables)
            branch: Filter to specific branch (None = all branches)
            limit: Maximum entries to return (None = no limit)

        Returns:
            List of changelog entries as dicts, each containing:
            - tx_id: Transaction ID
            - epoch_id: Epoch the transaction was in
            - committed_at: Unix timestamp of commit
            - branch: Branch name
            - changes: List of table changes, each with:
              - table_name: Name of changed table
              - old_version: Previous version (None if new table)
              - new_version: New version after change

        Raises:
            RuntimeError: If transaction_manager is not configured

        Example:
            >>> # Get all changes since transaction 100
            >>> changes = engine.get_changes(since_tx_id=100)
            >>> for entry in changes:
            ...     for change in entry["changes"]:
            ...         print(f"{change['table_name']}: v{change['old_version']} -> v{change['new_version']}")

            >>> # Get changes to specific tables in last hour
            >>> import time
            >>> one_hour_ago = int(time.time()) - 3600
            >>> changes = engine.get_changes(
            ...     since_timestamp=one_hour_ago,
            ...     tables=["users", "orders"]
            ... )
        """
        if self.transaction_manager is None:
            raise RuntimeError(
                "Cannot get changes: transaction_manager not configured. "
                "Initialize QueryEngine with a PyTransactionManager."
            )

        entries = self.transaction_manager.get_changelog(
            since_tx_id=since_tx_id,
            since_timestamp=since_timestamp,
            tables=tables,
            branch=branch,
            limit=limit,
        )

        return [
            {
                "tx_id": e.tx_id,
                "epoch_id": e.epoch_id,
                "committed_at": e.committed_at,
                "branch": e.branch,
                "changes": [
                    {
                        "table_name": c.table_name,
                        "old_version": c.old_version,
                        "new_version": c.new_version,
                    }
                    for c in e.changes
                ],
            }
            for e in entries
        ]

    def subscribe(
        self,
        tables: Optional[List[str]] = None,
        since_tx_id: Optional[int] = None,
        branch: Optional[str] = None,
        poll_interval: float = 1.0,
    ) -> "Subscriber":
        """
        Create a subscriber for changelog events.

        This is the streaming interface for the unified batch/stream model.
        Returns a Subscriber that can be iterated or used with callbacks.

        Args:
            tables: Only receive events for these tables (None = all)
            since_tx_id: Start from this transaction (exclusive).
                        None = start from current latest (won't replay history)
            branch: Filter to specific branch (None = all branches)
            poll_interval: Seconds between polls when waiting (default: 1.0)

        Returns:
            Subscriber instance for processing change events

        Raises:
            RuntimeError: If transaction_manager is not configured

        Example (iterator):
            >>> for event in engine.subscribe(tables=["users"]):
            ...     print(f"{event.table_name}: v{event.old_version} -> v{event.new_version}")
            ...     if should_stop:
            ...         break

        Example (non-blocking poll):
            >>> subscriber = engine.subscribe()
            >>> events = subscriber.poll()  # Returns immediately
            >>> process_events(events)

        Example (background processing):
            >>> def on_change(event):
            ...     print(f"Changed: {event.table_name}")
            >>> subscriber = engine.subscribe()
            >>> subscriber.start_background(on_change)
            >>> # ... do other work ...
            >>> subscriber.stop()
        """
        # Import here to avoid circular import
        from .subscriber import Subscriber

        if self.transaction_manager is None:
            raise RuntimeError(
                "Cannot subscribe: transaction_manager not configured. "
                "Initialize QueryEngine with a PyTransactionManager."
            )

        return Subscriber(
            transaction_manager=self.transaction_manager,
            since_tx_id=since_tx_id,
            tables=tables,
            branch=branch,
            poll_interval=poll_interval,
        )

    def latest_tx_id(self) -> Optional[int]:
        """
        Get the latest committed transaction ID.

        Useful for establishing a checkpoint before processing,
        then using get_changes(since_tx_id=checkpoint) later.

        Returns:
            Latest transaction ID, or None if no transactions yet

        Raises:
            RuntimeError: If transaction_manager is not configured
        """
        if self.transaction_manager is None:
            raise RuntimeError(
                "Cannot get latest_tx_id: transaction_manager not configured"
            )

        return self.transaction_manager.latest_tx_id()

    def close(self) -> None:
        """Close the DuckDB connection."""
        self._conn.close()

    def __enter__(self) -> "QueryEngine":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()
