"""
Diff Engine — Schema, row, and column-level diffs between table versions.

Three diff levels:
  1. Schema diff: Column additions, removals, type changes (always computed).
  2. Row diff: Added, removed, modified rows (requires key_columns).
  3. Column diff: Old/new values for each changed column in modified rows.

Merkle acceleration: Compares chunk hashes between versions to detect
  identical data without scanning. When chunks match, the diff is instant.

Semantic diffs: With an algebraic schema, shows "counter increased by 47"
  instead of "value changed from 100 to 147" for abelian columns.

Example:
    >>> from rhizo.diff import DiffEngine
    >>> engine = DiffEngine(catalog, store, reader)
    >>> result = engine.diff("users", 1, 5, key_columns=["id"])
    >>> print(result.summary())
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Dict, List, Optional, Set, Tuple

import duckdb
import pyarrow as pa

if TYPE_CHECKING:
    from _rhizo import (
        PyBranchManager,
        PyCatalog,
        PyChunkStore,
        PyTableAlgebraicSchema,
    )
    from .reader import TableReader

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------

@dataclass
class SchemaDiff:
    """Schema-level differences between two versions.

    Attributes:
        added_columns: Columns present in version B but not A.
        removed_columns: Columns present in version A but not B.
        type_changes: Columns present in both but with different types.
        unchanged_columns: Columns identical in both versions.
    """

    added_columns: List[Tuple[str, str]] = field(default_factory=list)
    removed_columns: List[Tuple[str, str]] = field(default_factory=list)
    type_changes: List[Tuple[str, str, str]] = field(default_factory=list)
    unchanged_columns: List[str] = field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        return bool(self.added_columns or self.removed_columns or self.type_changes)


@dataclass
class RowDiff:
    """Row-level differences (requires key columns).

    Attributes:
        added: Rows in version B not in version A.
        removed: Rows in version A not in version B.
        modified: Modified rows with key columns + __old_{col} / __new_{col} pairs.
        unchanged_count: Number of rows with identical key and values.
    """

    added: pa.Table
    removed: pa.Table
    modified: pa.Table
    unchanged_count: int


@dataclass
class DiffResult:
    """Complete diff result with schema, row, and column diffs.

    Attributes:
        table_name: Name of the diffed table.
        version_a: First (old) version number.
        version_b: Second (new) version number.
        rows_a: Row count in version A.
        rows_b: Row count in version B.
        schema: Schema-level diff (always present).
        rows: Row-level diff (None if key_columns not provided).
        chunks_a: Number of chunks in version A.
        chunks_b: Number of chunks in version B.
        chunks_scanned: Chunks that needed scanning (changed chunks).
        chunks_skipped: Chunks skipped via Merkle optimization (unchanged).
        semantic_changes: Algebraic semantic descriptions per column.
        elapsed_seconds: Wall-clock time for the diff.
    """

    table_name: str
    version_a: int
    version_b: int
    rows_a: int = 0
    rows_b: int = 0
    schema: SchemaDiff = field(default_factory=SchemaDiff)
    rows: Optional[RowDiff] = None
    chunks_a: int = 0
    chunks_b: int = 0
    chunks_scanned: int = 0
    chunks_skipped: int = 0
    semantic_changes: Optional[Dict[str, List[str]]] = None
    elapsed_seconds: float = 0.0

    def summary(self) -> str:
        """Human-readable summary of the diff."""
        lines = [f"Diff: {self.table_name} v{self.version_a} -> v{self.version_b}"]

        # Schema
        lines.append(
            f"Schema: {len(self.schema.added_columns)} added, "
            f"{len(self.schema.removed_columns)} removed, "
            f"{len(self.schema.type_changes)} type changes"
        )

        # Rows
        if self.rows is not None:
            lines.append(
                f"Rows: {self.rows.added.num_rows} added, "
                f"{self.rows.removed.num_rows} removed, "
                f"{self.rows.modified.num_rows} modified, "
                f"{self.rows.unchanged_count} unchanged"
            )
        else:
            lines.append(f"Rows: {self.rows_a} in v{self.version_a}, {self.rows_b} in v{self.version_b}")

        # Chunks
        total = self.chunks_scanned + self.chunks_skipped
        if total > 0:
            pct = self.chunks_skipped / total * 100
            lines.append(
                f"Chunks: {self.chunks_scanned} of {total} scanned "
                f"({pct:.0f}% skipped via Merkle)"
            )

        # Semantic
        if self.semantic_changes:
            for col, descriptions in self.semantic_changes.items():
                for desc in descriptions[:3]:  # Show first 3
                    lines.append(f"  {col}: {desc}")
                if len(descriptions) > 3:
                    lines.append(f"  {col}: ... and {len(descriptions) - 3} more")

        lines.append(f"Elapsed: {self.elapsed_seconds:.3f}s")
        return "\n".join(lines)

    def __repr__(self) -> str:
        return self.summary()


# ---------------------------------------------------------------------------
# DiffEngine
# ---------------------------------------------------------------------------

class DiffEngine:
    """Core diff engine with Merkle optimization.

    Args:
        catalog: PyCatalog for version metadata.
        store: PyChunkStore for chunk data.
        reader: TableReader for loading Arrow tables.
        branch_manager: Optional PyBranchManager for branch diffs.
    """

    def __init__(
        self,
        catalog: "PyCatalog",
        store: "PyChunkStore",
        reader: "TableReader",
        branch_manager: Optional["PyBranchManager"] = None,
        table_meta_store=None,
    ):
        self.catalog = catalog
        self.store = store
        self.reader = reader
        self.branch_manager = branch_manager
        self._table_meta_store = table_meta_store

    def diff(
        self,
        table_name: str,
        version_a: int,
        version_b: int,
        key_columns: Optional[List[str]] = None,
        schema: Optional["PyTableAlgebraicSchema"] = None,
    ) -> DiffResult:
        """Diff two versions of a table.

        Args:
            table_name: Table to diff.
            version_a: Old version.
            version_b: New version.
            key_columns: Primary key columns for row-level diff.
            schema: Algebraic schema for semantic diffs.

        Returns:
            DiffResult with schema, row, and column diffs.
        """
        # Auto-resolve key_columns from primary key metadata
        if key_columns is None and self._table_meta_store is not None:
            try:
                meta = self._table_meta_store.load(table_name)
                if meta.primary_key:
                    key_columns = meta.primary_key
            except Exception:
                pass

        t0 = time.monotonic()
        result = DiffResult(
            table_name=table_name,
            version_a=version_a,
            version_b=version_b,
        )

        # 1. Get version metadata
        meta_a = self.catalog.get_version(table_name, version_a)
        meta_b = self.catalog.get_version(table_name, version_b)

        result.chunks_a = len(meta_a.chunk_hashes)
        result.chunks_b = len(meta_b.chunk_hashes)

        # 2. Load tables for schema + row counts
        table_a = self.reader.read_arrow(table_name, version=version_a)
        table_b = self.reader.read_arrow(table_name, version=version_b)

        result.rows_a = table_a.num_rows
        result.rows_b = table_b.num_rows

        # 3. Schema diff
        result.schema = self._diff_schemas(table_a.schema, table_b.schema)

        # 4. Merkle chunk stats
        chunks_a_set = set(meta_a.chunk_hashes)
        chunks_b_set = set(meta_b.chunk_hashes)
        unchanged_chunks = chunks_a_set & chunks_b_set
        only_a = chunks_a_set - chunks_b_set
        only_b = chunks_b_set - chunks_a_set
        result.chunks_scanned = len(only_a) + len(only_b)
        result.chunks_skipped = len(unchanged_chunks)

        # 5. Fast path: identical chunk hashes = identical data
        if chunks_a_set == chunks_b_set:
            if key_columns is not None:
                empty = pa.table({col: pa.array([], type=table_a.schema.field(col).type)
                                  for col in table_a.column_names})
                result.rows = RowDiff(
                    added=empty,
                    removed=empty,
                    modified=_empty_modified_table(table_a.schema, key_columns),
                    unchanged_count=table_a.num_rows,
                )
            result.elapsed_seconds = time.monotonic() - t0
            return result

        # 6. Row-level diff (if key columns provided)
        if key_columns is not None:
            self._validate_key_columns(key_columns, table_a, table_b)
            row_diff = self._diff_rows(table_a, table_b, key_columns)
            result.rows = row_diff

            # 7. Semantic diffs (if schema provided)
            if schema is not None and row_diff.modified.num_rows > 0:
                result.semantic_changes = self._compute_semantic_diffs(
                    row_diff.modified, key_columns, schema
                )

        result.elapsed_seconds = time.monotonic() - t0
        logger.info(
            "Diff %s v%d->v%d: %d added, %d removed, %d modified in %.3fs",
            table_name, version_a, version_b,
            result.rows.added.num_rows if result.rows else 0,
            result.rows.removed.num_rows if result.rows else 0,
            result.rows.modified.num_rows if result.rows else 0,
            result.elapsed_seconds,
        )
        return result

    def _diff_schemas(self, schema_a: pa.Schema, schema_b: pa.Schema) -> SchemaDiff:
        """Compare two Arrow schemas."""
        names_a = {f.name for f in schema_a}
        names_b = {f.name for f in schema_b}

        fields_a = {f.name: f for f in schema_a}
        fields_b = {f.name: f for f in schema_b}

        added = [(name, str(fields_b[name].type)) for name in sorted(names_b - names_a)]
        removed = [(name, str(fields_a[name].type)) for name in sorted(names_a - names_b)]

        type_changes = []
        unchanged = []
        for name in sorted(names_a & names_b):
            if fields_a[name].type != fields_b[name].type:
                type_changes.append((name, str(fields_a[name].type), str(fields_b[name].type)))
            else:
                unchanged.append(name)

        return SchemaDiff(
            added_columns=added,
            removed_columns=removed,
            type_changes=type_changes,
            unchanged_columns=unchanged,
        )

    def _validate_key_columns(
        self, key_columns: List[str], table_a: pa.Table, table_b: pa.Table
    ) -> None:
        """Validate key columns exist in both tables."""
        all_cols = set(table_a.column_names) | set(table_b.column_names)
        for col in key_columns:
            if not re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', col):
                raise ValueError(
                    f"Invalid column name '{col}': must be a valid SQL identifier"
                )
            if col not in all_cols:
                raise ValueError(
                    f"Key column '{col}' not found. Available: {sorted(all_cols)}"
                )

    def _diff_rows(
        self,
        table_a: pa.Table,
        table_b: pa.Table,
        key_columns: List[str],
    ) -> RowDiff:
        """Compute row-level diff using DuckDB."""
        conn = duckdb.connect()
        try:
            conn.register("__diff_a", table_a)
            conn.register("__diff_b", table_b)

            join_cond = " AND ".join(f'a."{c}" = b."{c}"' for c in key_columns)

            # Added: rows in B not in A
            added = conn.execute(f"""
                SELECT b.* FROM __diff_b b
                WHERE NOT EXISTS (
                    SELECT 1 FROM __diff_a a WHERE {join_cond}
                )
            """).fetch_arrow_table()

            # Removed: rows in A not in B
            removed = conn.execute(f"""
                SELECT a.* FROM __diff_a a
                WHERE NOT EXISTS (
                    SELECT 1 FROM __diff_b b WHERE {join_cond}
                )
            """).fetch_arrow_table()

            # Modified: same key, different non-key values
            non_key_cols = [c for c in table_a.column_names if c not in key_columns]
            if not non_key_cols:
                # All columns are keys — no modification possible
                empty_mod = _empty_modified_table(table_a.schema, key_columns)
                unchanged_count = table_a.num_rows - removed.num_rows
                return RowDiff(
                    added=added, removed=removed,
                    modified=empty_mod, unchanged_count=unchanged_count,
                )

            # Build WHERE clause for detecting any column change
            change_checks = []
            for c in non_key_cols:
                change_checks.append(
                    f'(a."{c}" IS DISTINCT FROM b."{c}")'
                )
            where_changed = " OR ".join(change_checks)

            # Build SELECT for modified rows: key_cols + __old_{col} + __new_{col}
            select_parts = [f'a."{c}"' for c in key_columns]
            for c in non_key_cols:
                select_parts.append(f'a."{c}" AS "__old_{c}"')
                select_parts.append(f'b."{c}" AS "__new_{c}"')
            select_clause = ", ".join(select_parts)

            modified = conn.execute(f"""
                SELECT {select_clause}
                FROM __diff_a a
                INNER JOIN __diff_b b ON {join_cond}
                WHERE {where_changed}
            """).fetch_arrow_table()

            unchanged_count = table_a.num_rows - removed.num_rows - modified.num_rows

            return RowDiff(
                added=added,
                removed=removed,
                modified=modified,
                unchanged_count=unchanged_count,
            )
        finally:
            conn.close()

    def _compute_semantic_diffs(
        self,
        modified: pa.Table,
        key_columns: List[str],
        schema: "PyTableAlgebraicSchema",
    ) -> Dict[str, List[str]]:
        """Compute semantic diff descriptions from algebraic schema."""
        from _rhizo import PyOpType

        result: Dict[str, List[str]] = {}

        # Identify changed columns from the modified table
        # Modified table has: key_cols, __old_{col}, __new_{col}
        col_names = modified.column_names
        changed_cols = set()
        for name in col_names:
            if name.startswith("__old_"):
                changed_cols.add(name[6:])  # strip __old_ prefix

        for col in sorted(changed_cols):
            old_col = f"__old_{col}"
            new_col = f"__new_{col}"
            if old_col not in col_names or new_col not in col_names:
                continue

            try:
                op_type = schema.get_op_type(col)
            except Exception:
                op_type = None

            descriptions = []
            old_arr = modified.column(old_col)
            new_arr = modified.column(new_col)

            for i in range(min(modified.num_rows, 1000)):  # Cap at 1000 rows
                old_val = old_arr[i].as_py()
                new_val = new_arr[i].as_py()

                if old_val == new_val:
                    continue

                desc = _describe_change(col, old_val, new_val, op_type)
                descriptions.append(desc)

            if descriptions:
                result[col] = descriptions

        return result if result else None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _empty_modified_table(schema: pa.Schema, key_columns: List[str]) -> pa.Table:
    """Create an empty modified-rows table with the right schema."""
    fields = []
    for col in key_columns:
        fields.append(pa.field(col, schema.field(col).type))
    for f in schema:
        if f.name not in key_columns:
            fields.append(pa.field(f"__old_{f.name}", f.type))
            fields.append(pa.field(f"__new_{f.name}", f.type))
    mod_schema = pa.schema(fields)
    return pa.table({f.name: pa.array([], type=f.type) for f in mod_schema})


def _describe_change(col: str, old_val, new_val, op_type) -> str:
    """Generate a human-readable change description."""
    if op_type is None:
        return f"changed from {old_val} to {new_val}"

    op_name = str(op_type).lower()

    if "add" in op_name and isinstance(old_val, (int, float)) and isinstance(new_val, (int, float)):
        delta = new_val - old_val
        if delta >= 0:
            return f"incremented by {delta}"
        return f"decremented by {abs(delta)}"

    if "max" in op_name:
        return f"new maximum: {new_val} (was {old_val})"

    if "min" in op_name:
        return f"new minimum: {new_val} (was {old_val})"

    if "union" in op_name:
        if isinstance(old_val, (list, set)) and isinstance(new_val, (list, set)):
            old_set = set(old_val) if not isinstance(old_val, set) else old_val
            new_set = set(new_val) if not isinstance(new_val, set) else new_val
            added = new_set - old_set
            removed_items = old_set - new_set
            parts = []
            if added:
                parts.append(f"added {added}")
            if removed_items:
                parts.append(f"removed {removed_items}")
            return ", ".join(parts) if parts else "no change"

    return f"changed from {old_val} to {new_val}"
