"""
TableReader - Read versioned tables from content-addressable storage.

The reader handles:
1. Fetching table version metadata from the catalog
2. Retrieving chunks from the content-addressable store
3. Deserializing Parquet chunks to Arrow (PyArrow or native Rust decoder)
4. Optionally concatenating into a single table
"""

from __future__ import annotations

import io
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional, Iterator, List, Any

import pyarrow as pa
import pyarrow.parquet as pq

if TYPE_CHECKING:
    import pandas as pd
    from _rhizo import PyPredicateFilter

# Try to import native Parquet decoder (Phase 4) and filters (Phase R.2)
try:
    from _rhizo import PyParquetDecoder, PyPredicateFilter as _PyPredicateFilter
    _NATIVE_PARQUET_AVAILABLE = True
except ImportError:
    _NATIVE_PARQUET_AVAILABLE = False
    _PyPredicateFilter = None


def _create_filter(column: str, op: str, value: Any) -> "PyPredicateFilter":
    """Create a predicate filter (internal helper with runtime check)."""
    if _PyPredicateFilter is None:
        raise RuntimeError("Filter requires native Parquet decoder")
    return _PyPredicateFilter(column, op, value)


class Filter:
    """
    Helper class for building predicate filters.

    Provides a fluent API for creating predicates:
        Filter("age").gt(50)  # age > 50
        Filter("status").eq("active")  # status = 'active'

    Multiple filters are ANDed together when passed to read methods.

    Mathematical Model:
        Predicate pushdown reduces data by selectivity `s`:
        Combined with projection: Speedup ≈ (n/k) × (1/s)
        Example: 10 columns, query 2, 1% selectivity → up to 500x speedup
    """

    def __init__(self, column: str):
        """Create a filter builder for a column."""
        if not _NATIVE_PARQUET_AVAILABLE:
            raise RuntimeError(
                "Filter requires native Parquet decoder. "
                "Please ensure the rhizo package is installed correctly."
            )
        self.column = column

    def eq(self, value):
        """Equal: column = value"""
        return _create_filter(self.column, "eq", value)

    def ne(self, value):
        """Not equal: column != value"""
        return _create_filter(self.column, "ne", value)

    def lt(self, value):
        """Less than: column < value"""
        return _create_filter(self.column, "lt", value)

    def le(self, value):
        """Less than or equal: column <= value"""
        return _create_filter(self.column, "le", value)

    def gt(self, value):
        """Greater than: column > value"""
        return _create_filter(self.column, "gt", value)

    def ge(self, value):
        """Greater than or equal: column >= value"""
        return _create_filter(self.column, "ge", value)


@dataclass
class TableMetadata:
    """Metadata about a table version."""
    table_name: str
    version: int
    chunk_count: int
    chunk_hashes: List[str]
    created_at: int
    parent_version: Optional[int]


class TableReader:
    """
    Reads versioned tables from Armillaria storage.

    Supports:
    - Reading specific versions (time travel)
    - Reading the latest version
    - Streaming chunks for memory efficiency
    - Full table materialization

    Example:
        >>> from _rhizo import PyChunkStore, PyCatalog
        >>> from rhizo import TableReader
        >>>
        >>> store = PyChunkStore("./data/chunks")
        >>> catalog = PyCatalog("./data/catalog")
        >>> reader = TableReader(store, catalog)
        >>>
        >>> # Read latest version as Arrow
        >>> table = reader.read_arrow("my_table")
        >>>
        >>> # Time travel to version 5
        >>> old_table = reader.read_arrow("my_table", version=5)
        >>>
        >>> # Stream chunks for large tables
        >>> for chunk in reader.iter_chunks("my_table"):
        ...     process(chunk)
    """

    def __init__(
        self,
        store,  # PyChunkStore
        catalog,  # PyCatalog
        verify_integrity: bool = False,
        use_mmap: bool = False,
        parallel_workers: Optional[int] = None,
        use_native_parquet: bool = True,
    ):
        """
        Initialize the TableReader.

        Args:
            store: PyChunkStore instance for content-addressable storage
            catalog: PyCatalog instance for version metadata
            verify_integrity: If True, verify chunk hashes on read (slower but safer)
            use_mmap: If True, use memory-mapped I/O for reading chunks (can improve
                      performance for large files through OS page caching)
            parallel_workers: Number of threads for parallel Parquet parsing.
                             None (default) = sequential parsing.
                             Set to number of CPU cores for best multi-chunk performance.
            use_native_parquet: Use Rust-native Parquet decoder for better performance
                               (default True, falls back to PyArrow if unavailable)
        """
        self.store = store
        self.catalog = catalog
        self.verify_integrity = verify_integrity
        self.use_mmap = use_mmap
        self.parallel_workers = parallel_workers
        self.use_native_parquet = use_native_parquet and _NATIVE_PARQUET_AVAILABLE

        # Initialize native decoder if available and requested
        self._native_decoder = None
        if self.use_native_parquet:
            self._native_decoder = PyParquetDecoder()

    def get_metadata(
        self,
        table_name: str,
        version: Optional[int] = None,
    ) -> TableMetadata:
        """
        Get metadata for a table version.

        Args:
            table_name: Name of the table
            version: Specific version to read (None for latest)

        Returns:
            TableMetadata with version information

        Raises:
            IOError: If table or version not found
        """
        table_version = self.catalog.get_version(table_name, version)

        return TableMetadata(
            table_name=table_version.table_name,
            version=table_version.version,
            chunk_count=len(table_version.chunk_hashes),
            chunk_hashes=list(table_version.chunk_hashes),
            created_at=table_version.created_at,
            parent_version=table_version.parent_version,
        )

    def read_arrow(
        self,
        table_name: str,
        version: Optional[int] = None,
        columns: Optional[List[str]] = None,
        filters: Optional[List] = None,
    ) -> pa.Table:
        """
        Read a table version as an Arrow Table.

        This materializes the entire table in memory. For large tables,
        consider using iter_chunks() instead.

        Args:
            table_name: Name of the table
            version: Specific version to read (None for latest)
            columns: If specified, only read these columns (projection pushdown).
                     This is significantly faster when you only need a subset
                     of columns. Expected speedup: n/k where n=total columns,
                     k=requested columns.
            filters: If specified, filter rows during decoding (predicate pushdown).
                     Use Filter class for building predicates:
                     [Filter("age").gt(50), Filter("status").eq("active")]
                     Multiple filters are ANDed together.

        Returns:
            Arrow Table containing all data (or selected columns/filtered rows)

        Raises:
            IOError: If table or version not found
            ValueError: If chunk data is corrupted or column not found

        Example:
            >>> # Projection pushdown only
            >>> reader.read_arrow("users", columns=["name", "age"])

            >>> # Filter pushdown only
            >>> reader.read_arrow("users", filters=[Filter("age").gt(50)])

            >>> # Combined projection + filter (maximum speedup)
            >>> reader.read_arrow("users",
            ...     columns=["name"],
            ...     filters=[Filter("age").gt(50), Filter("status").eq("active")])
        """
        metadata = self.get_metadata(table_name, version)

        if not metadata.chunk_hashes:
            raise ValueError(f"Table {table_name} has no chunks")

        # Fetch all chunks in parallel using batch operation
        if self.use_mmap:
            # Memory-mapped I/O for potentially better OS caching
            chunk_data_list = self.store.get_mmap_batch(metadata.chunk_hashes)
        elif self.verify_integrity:
            chunk_data_list = self.store.get_batch_verified(metadata.chunk_hashes)
        else:
            chunk_data_list = self.store.get_batch(metadata.chunk_hashes)

        # Deserialize chunks to Arrow - parallel if configured and multiple chunks
        if self.parallel_workers and len(chunk_data_list) > 1:
            # Use thread pool for parallel Parquet parsing
            with ThreadPoolExecutor(max_workers=self.parallel_workers) as executor:
                arrow_chunks = list(executor.map(
                    lambda data: self._parquet_to_arrow(data, columns=columns, filters=filters),
                    chunk_data_list
                ))
        else:
            # Sequential parsing (default, or single chunk)
            arrow_chunks = [self._parquet_to_arrow(data, columns=columns, filters=filters) for data in chunk_data_list]

        # Filter out empty tables (from filters that matched no rows)
        arrow_chunks = [t for t in arrow_chunks if t.num_rows > 0]

        if not arrow_chunks:
            # All chunks filtered out - return empty table with correct schema
            # Try to get schema from first chunk without filters
            schema = self._parquet_to_arrow(chunk_data_list[0], columns=columns).schema
            return pa.table({name: pa.array([], type=schema.field(name).type) for name in schema.names})

        if len(arrow_chunks) == 1:
            return arrow_chunks[0]

        return pa.concat_tables(arrow_chunks)

    def read_pandas(
        self,
        table_name: str,
        version: Optional[int] = None,
    ) -> "pd.DataFrame":
        """
        Read a table version as a pandas DataFrame.

        Args:
            table_name: Name of the table
            version: Specific version to read (None for latest)

        Returns:
            pandas DataFrame containing all data
        """
        arrow_table = self.read_arrow(table_name, version)
        return arrow_table.to_pandas()

    def iter_chunks(
        self,
        table_name: str,
        version: Optional[int] = None,
        columns: Optional[List[str]] = None,
        filters: Optional[List] = None,
    ) -> Iterator[pa.Table]:
        """
        Iterate over table chunks as Arrow Tables.

        Memory-efficient for large tables - only one chunk in memory at a time.

        Args:
            table_name: Name of the table
            version: Specific version to read (None for latest)
            columns: If specified, only read these columns (projection pushdown).
                     This is significantly faster when you only need a subset
                     of columns.
            filters: If specified, filter rows during decoding (predicate pushdown).
                     Use Filter class for building predicates.

        Yields:
            Arrow Tables, one per chunk (or with selected columns/filtered rows)
            Note: Chunks with no matching rows are skipped.

        Raises:
            IOError: If table or version not found
            ValueError: If chunk data is corrupted or column not found
        """
        metadata = self.get_metadata(table_name, version)

        for chunk_hash in metadata.chunk_hashes:
            chunk_data = self._fetch_chunk(chunk_hash)
            try:
                arrow_table = self._parquet_to_arrow(chunk_data, columns=columns, filters=filters)
                if arrow_table.num_rows > 0:
                    yield arrow_table
            except ValueError as e:
                # Handle empty data from filters
                if "empty" in str(e).lower():
                    continue
                raise

    def _fetch_chunk(self, chunk_hash: str) -> bytes:
        """Fetch chunk data from the store."""
        if self.use_mmap:
            return self.store.get_mmap(chunk_hash)
        elif self.verify_integrity:
            return self.store.get_verified(chunk_hash)
        else:
            return self.store.get(chunk_hash)

    def _parquet_to_arrow(
        self,
        data: bytes,
        columns: Optional[List[str]] = None,
        filters: Optional[List] = None,
    ) -> pa.Table:
        """Deserialize Parquet bytes to Arrow Table.

        Args:
            data: Parquet file bytes
            columns: If specified, only decode these columns (projection pushdown)
            filters: If specified, filter rows during decoding (predicate pushdown)
        """
        if self._native_decoder is not None:
            # Use native Rust decoder for better performance
            # Decoder returns RecordBatch, convert to Table

            if filters is not None and len(filters) > 0:
                # Predicate pushdown with optional projection
                # Note: column_indices would require resolving names to indices
                # For simplicity, we don't combine with column projection here
                # since decode_with_filter already returns all columns
                try:
                    batch = self._native_decoder.decode_with_filter(data, filters)
                    result = pa.Table.from_batches([batch])
                except ValueError as e:
                    if "empty" in str(e).lower():
                        # No rows matched - return empty table with schema
                        # Get schema from unfiltered decode
                        batch = self._native_decoder.decode(data)
                        schema = batch.schema
                        if columns is not None:
                            # Filter schema to only include requested columns
                            fields = [schema.field(name) for name in columns if name in schema.names]
                            schema = pa.schema(fields)
                        return pa.table({name: pa.array([], type=schema.field(name).type) for name in schema.names})
                    raise

                # Apply column projection after filtering if needed
                if columns is not None:
                    result = result.select(columns)
                return result

            elif columns is not None:
                # Projection pushdown only - only decode requested columns
                batch = self._native_decoder.decode_columns_by_name(data, columns)
                return pa.Table.from_batches([batch])

            else:
                # Full decode
                batch = self._native_decoder.decode(data)
                return pa.Table.from_batches([batch])

        # Fallback to PyArrow (no filter pushdown support)
        buffer = io.BytesIO(data)
        table = pq.read_table(buffer, columns=columns)

        # If filters specified but using PyArrow, apply them post-hoc
        if filters is not None and len(filters) > 0:
            import warnings
            warnings.warn(
                "Filter pushdown requires native Parquet decoder. "
                "Filtering applied after decoding (less efficient).",
                UserWarning
            )
            # Apply filters using PyArrow compute
            for f in filters:
                table = self._apply_pyarrow_filter(table, f)

        return table

    def _apply_pyarrow_filter(self, table: pa.Table, filter_pred: Any) -> pa.Table:
        """Apply a filter predicate to an Arrow table (fallback for PyArrow)."""
        import pyarrow.compute as pc

        column = filter_pred.column
        op = filter_pred.op
        value_str = filter_pred.value

        # Parse the value - it's stored as a string in the filter
        # Try to parse as the appropriate type
        try:
            value: Any = int(value_str)
        except ValueError:
            try:
                value = float(value_str)
            except ValueError:
                value = value_str.strip("'")

        col_array = table.column(column)

        # Map filter ops to PyArrow compute function names
        op_func_map = {
            "=": "equal",
            "!=": "not_equal",
            "<": "less",
            "<=": "less_equal",
            ">": "greater",
            ">=": "greater_equal",
        }

        if op not in op_func_map:
            raise ValueError(f"Unknown filter operation: {op}")

        compute_fn = getattr(pc, op_func_map[op])
        mask = compute_fn(col_array, value)
        return table.filter(mask)

    def list_versions(self, table_name: str) -> List[int]:
        """
        List all available versions of a table.

        Args:
            table_name: Name of the table

        Returns:
            List of version numbers in ascending order
        """
        return self.catalog.list_versions(table_name)

    def list_tables(self) -> List[str]:
        """
        List all tables in the catalog.

        Returns:
            List of table names in alphabetical order
        """
        return self.catalog.list_tables()

    def get_version_history(
        self,
        table_name: str,
        limit: Optional[int] = None,
    ) -> List[TableMetadata]:
        """
        Get metadata for all versions of a table.

        Args:
            table_name: Name of the table
            limit: Maximum number of versions to return (most recent first)

        Returns:
            List of TableMetadata, most recent first
        """
        versions = self.list_versions(table_name)

        # Sort descending (most recent first)
        versions = sorted(versions, reverse=True)

        if limit is not None:
            versions = versions[:limit]

        return [self.get_metadata(table_name, v) for v in versions]
