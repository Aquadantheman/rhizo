"""
TableWriter - Write DataFrames/Arrow tables as versioned, chunked Parquet.

The writer handles:
1. Converting input data to Arrow format
2. Chunking large tables for efficient storage
3. Serializing chunks as Parquet (PyArrow or native Rust encoder)
4. Storing chunks in the content-addressable store
5. Committing table versions to the catalog
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional, Union, List, Dict, Any

import pyarrow as pa
import pyarrow.parquet as pq

from rhizo.exceptions import (
    validate_table_name,
    SchemaEvolutionError,
    PrimaryKeyViolationError,
)
from rhizo.table_meta import TableMeta, TableMetaStore
from rhizo.schema_utils import (
    SCHEMA_METADATA_KEY,
    serialize_schema,
    deserialize_schema,
    compare_schemas,
)

if TYPE_CHECKING:
    import pandas as pd

# Try to import native Parquet encoder (Phase 4)
try:
    from _rhizo import PyParquetEncoder
    _NATIVE_PARQUET_AVAILABLE = True
except ImportError:
    _NATIVE_PARQUET_AVAILABLE = False


# Default chunk size: 64MB uncompressed (will be smaller after Parquet compression)
DEFAULT_CHUNK_SIZE_BYTES = 64 * 1024 * 1024
# Default rows per chunk if byte estimation fails
DEFAULT_CHUNK_SIZE_ROWS = 100_000

# Size limits (configurable via constructor)
# 10GB default: ~2x PyArrow overhead means 20-30GB peak RAM for typical servers
DEFAULT_MAX_TABLE_SIZE_BYTES = 10 * 1024 * 1024 * 1024    # 10 GB
DEFAULT_MAX_COLUMNS = 1_000                                # Reasonable schema limit


@dataclass
class WriteResult:
    """Result of a table write operation."""
    table_name: str
    version: int
    chunk_count: int
    chunk_hashes: List[str]
    total_rows: int
    total_bytes: int


@dataclass
class ChunkWriteResult:
    """
    Result of writing chunks without catalog commit.

    Used internally by transactions to separate chunk storage from
    catalog commit (which the TransactionManager handles).
    """
    table_name: str
    next_version: int  # The version that WILL be assigned
    chunk_count: int
    chunk_hashes: List[str]
    total_rows: int
    total_bytes: int


class TableWriter:
    """
    Writes data to Rhizo as versioned, content-addressable Parquet chunks.

    Example:
        >>> from _rhizo import PyChunkStore, PyCatalog
        >>> from rhizo import TableWriter
        >>>
        >>> store = PyChunkStore("./data/chunks")
        >>> catalog = PyCatalog("./data/catalog")
        >>> writer = TableWriter(store, catalog)
        >>>
        >>> # Write a DataFrame
        >>> import pandas as pd
        >>> df = pd.DataFrame({"id": [1, 2, 3], "value": [10, 20, 30]})
        >>> result = writer.write("my_table", df)
        >>> print(f"Committed version {result.version} with {result.chunk_count} chunks")
    """

    def __init__(
        self,
        store,  # PyChunkStore
        catalog,  # PyCatalog
        chunk_size_bytes: int = DEFAULT_CHUNK_SIZE_BYTES,
        chunk_size_rows: Optional[int] = None,
        use_native_parquet: bool = True,
        max_table_size_bytes: int = DEFAULT_MAX_TABLE_SIZE_BYTES,
        max_columns: int = DEFAULT_MAX_COLUMNS,
        catalog_path: Optional[str] = None,
    ):
        """
        Initialize the TableWriter.

        Args:
            store: PyChunkStore instance for content-addressable storage
            catalog: PyCatalog instance for version metadata
            chunk_size_bytes: Target size per chunk in bytes (default 64MB)
            chunk_size_rows: Optional fixed row count per chunk (overrides byte-based)
            use_native_parquet: Use Rust-native Parquet encoder for better performance
                               (default True, falls back to PyArrow if unavailable)
            max_table_size_bytes: Maximum table size in bytes (default 10GB).
                                  Prevents OOM attacks from oversized inputs.
            max_columns: Maximum number of columns (default 1000).
                        Prevents schema explosion attacks.
            catalog_path: Path to catalog directory for table metadata.
                         Enables schema evolution and primary key enforcement.
        """
        self.store = store
        self.catalog = catalog
        self.chunk_size_bytes = chunk_size_bytes
        self.chunk_size_rows = chunk_size_rows
        self.use_native_parquet = use_native_parquet and _NATIVE_PARQUET_AVAILABLE
        self.max_table_size_bytes = max_table_size_bytes
        self.max_columns = max_columns
        self._meta_store = TableMetaStore(catalog_path) if catalog_path else None

        # Initialize native encoder if available and requested
        self._native_encoder = None
        if self.use_native_parquet:
            self._native_encoder = PyParquetEncoder("zstd")

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
        Write data as a new version of the specified table.

        Args:
            table_name: Name of the table to write
            data: DataFrame or Arrow Table to write
            metadata: Optional key-value metadata for this version
            primary_key: Columns that form the primary key (set once, immutable).
                        Enforces uniqueness at write time.
            schema_mode: Schema evolution mode override for this write.
                        "additive" (default) or "flexible".

        Returns:
            WriteResult with version info and statistics

        Raises:
            ValueError: If data is empty, invalid, or exceeds size limits
            SchemaEvolutionError: If schema change violates evolution policy
            PrimaryKeyViolationError: If data contains duplicate key values
        """
        table_name = validate_table_name(table_name)

        # Convert to Arrow Table if needed
        table = self._to_arrow(data)

        if table.num_rows == 0:
            raise ValueError("Cannot write empty table")

        # Validate size limits (prevents OOM attacks)
        if table.nbytes > self.max_table_size_bytes:
            raise ValueError(
                f"Table size ({table.nbytes:,} bytes) exceeds maximum "
                f"({self.max_table_size_bytes:,} bytes). "
                f"Increase max_table_size_bytes or chunk your data."
            )

        if len(table.column_names) > self.max_columns:
            raise ValueError(
                f"Column count ({len(table.column_names)}) exceeds maximum "
                f"({self.max_columns}). Reduce columns or increase max_columns."
            )

        # Schema evolution and primary key checks
        version_metadata = dict(metadata) if metadata else {}
        table_meta = self._validate_schema_and_pk(
            table_name, table, primary_key, schema_mode
        )

        # Store serialized schema in version metadata
        version_metadata[SCHEMA_METADATA_KEY] = serialize_schema(table.schema)

        # Determine chunking strategy
        chunks = self._chunk_table(table)

        # Serialize all chunks to Parquet bytes
        # Use parallel encoding for multiple chunks if native encoder available
        if len(chunks) > 1 and self._native_encoder is not None:
            # Convert all chunks to batches for parallel encoding via Rayon
            batches = [chunk.combine_chunks().to_batches()[0] for chunk in chunks]
            parquet_chunks = [bytes(b) for b in self._native_encoder.encode_batch(batches)]
        else:
            parquet_chunks = [self._to_parquet_bytes(chunk) for chunk in chunks]
        total_bytes = sum(len(p) for p in parquet_chunks)

        # Store chunks: use zero-copy put() for single chunks,
        # put_batch() for multiple chunks (parallelizes via Rayon)
        if len(parquet_chunks) == 1:
            chunk_hashes = [self.store.put(parquet_chunks[0])]
        else:
            chunk_hashes = self.store.put_batch(parquet_chunks)

        # Commit with metadata (schema info attached to version)
        committed_version = self.catalog.commit_next_with_meta(
            table_name, chunk_hashes, version_metadata
        )

        # Save table meta if PK was newly set
        if table_meta and self._meta_store:
            self._meta_store.save(table_name, table_meta)

        return WriteResult(
            table_name=table_name,
            version=committed_version,
            chunk_count=len(chunk_hashes),
            chunk_hashes=chunk_hashes,
            total_rows=table.num_rows,
            total_bytes=total_bytes,
        )

    def write_chunks_only(
        self,
        table_name: str,
        data: Union["pd.DataFrame", pa.Table],
        *,
        primary_key: Optional[List[str]] = None,
        schema_mode: Optional[str] = None,
    ) -> ChunkWriteResult:
        """
        Write data chunks without committing to catalog.

        This is used by transactions to separate the chunk write phase
        from the catalog commit phase. The TransactionManager handles
        the catalog commit for atomicity.

        Args:
            table_name: Name of the table to write
            data: DataFrame or Arrow Table to write
            primary_key: Columns that form the primary key
            schema_mode: Schema evolution mode override

        Returns:
            ChunkWriteResult with chunk info and the version that will be assigned

        Raises:
            ValueError: If data is empty, invalid, or exceeds size limits
            SchemaEvolutionError: If schema change violates evolution policy
            PrimaryKeyViolationError: If data contains duplicate key values
        """
        table_name = validate_table_name(table_name)

        # Convert to Arrow Table if needed
        table = self._to_arrow(data)

        if table.num_rows == 0:
            raise ValueError("Cannot write empty table")

        # Validate size limits (prevents OOM attacks)
        if table.nbytes > self.max_table_size_bytes:
            raise ValueError(
                f"Table size ({table.nbytes:,} bytes) exceeds maximum "
                f"({self.max_table_size_bytes:,} bytes). "
                f"Increase max_table_size_bytes or chunk your data."
            )

        if len(table.column_names) > self.max_columns:
            raise ValueError(
                f"Column count ({len(table.column_names)}) exceeds maximum "
                f"({self.max_columns}). Reduce columns or increase max_columns."
            )

        # Schema evolution and primary key checks
        self._validate_schema_and_pk(table_name, table, primary_key, schema_mode)

        # Determine chunking strategy
        chunks = self._chunk_table(table)

        # Serialize all chunks to Parquet bytes
        # Use parallel encoding for multiple chunks if native encoder available
        if len(chunks) > 1 and self._native_encoder is not None:
            # Convert all chunks to batches for parallel encoding via Rayon
            batches = [chunk.combine_chunks().to_batches()[0] for chunk in chunks]
            parquet_chunks = [bytes(b) for b in self._native_encoder.encode_batch(batches)]
        else:
            parquet_chunks = [self._to_parquet_bytes(chunk) for chunk in chunks]
        total_bytes = sum(len(p) for p in parquet_chunks)

        # Store chunks: use zero-copy put() for single chunks,
        # put_batch() for multiple chunks (parallelizes via Rayon)
        if len(parquet_chunks) == 1:
            chunk_hashes = [self.store.put(parquet_chunks[0])]
        else:
            chunk_hashes = self.store.put_batch(parquet_chunks)

        # Determine what the next version WILL be (don't commit yet)
        next_version = self._get_next_version(table_name)

        return ChunkWriteResult(
            table_name=table_name,
            next_version=next_version,
            chunk_count=len(chunk_hashes),
            chunk_hashes=chunk_hashes,
            total_rows=table.num_rows,
            total_bytes=total_bytes,
        )

    def _to_arrow(self, data: Union["pd.DataFrame", pa.Table]) -> pa.Table:
        """Convert input data to Arrow Table."""
        if isinstance(data, pa.Table):
            return data

        # Assume pandas DataFrame
        try:
            return pa.Table.from_pandas(data, preserve_index=False)
        except Exception as e:
            raise ValueError(f"Failed to convert data to Arrow: {e}") from e

    def _chunk_table(self, table: pa.Table) -> List[pa.Table]:
        """
        Split table into chunks based on configured strategy.

        Returns list of Arrow Tables, each representing one chunk.
        """
        total_rows = table.num_rows

        if total_rows == 0:
            return []

        # If row-based chunking is specified, use it
        if self.chunk_size_rows is not None:
            rows_per_chunk = self.chunk_size_rows
        else:
            # Estimate rows per chunk based on byte size
            rows_per_chunk = self._estimate_rows_per_chunk(table)

        # Single chunk if small enough
        if total_rows <= rows_per_chunk:
            return [table]

        # Split into chunks
        chunks = []
        offset = 0

        while offset < total_rows:
            end = min(offset + rows_per_chunk, total_rows)
            chunk = table.slice(offset, end - offset)
            chunks.append(chunk)
            offset = end

        return chunks

    def _estimate_rows_per_chunk(self, table: pa.Table) -> int:
        """
        Estimate how many rows fit in the target chunk size.

        Uses a sample-based approach for large tables.
        """
        total_rows = table.num_rows

        if total_rows == 0:
            return DEFAULT_CHUNK_SIZE_ROWS

        # Sample a small portion to estimate size
        sample_rows = min(1000, total_rows)
        sample = table.slice(0, sample_rows)
        sample_bytes = self._to_parquet_bytes(sample)

        bytes_per_row = len(sample_bytes) / sample_rows

        if bytes_per_row <= 0:
            return DEFAULT_CHUNK_SIZE_ROWS

        estimated_rows = int(self.chunk_size_bytes / bytes_per_row)

        # Clamp to reasonable bounds
        return max(1000, min(estimated_rows, 10_000_000))

    def _to_parquet_bytes(self, table: pa.Table) -> bytes:
        """Serialize Arrow Table to Parquet bytes."""
        if self._native_encoder is not None:
            # Use native Rust encoder for better performance
            # Convert Table to a single RecordBatch for encoding
            batch = table.combine_chunks().to_batches()[0]
            return bytes(self._native_encoder.encode(batch))

        # Fallback to PyArrow
        buffer = io.BytesIO()
        pq.write_table(
            table,
            buffer,
            compression="zstd",  # Good balance of speed and compression
            write_statistics=True,  # Enables query optimization
        )
        return buffer.getvalue()

    def _validate_schema_and_pk(
        self,
        table_name: str,
        table: pa.Table,
        primary_key: Optional[List[str]],
        schema_mode: Optional[str],
    ) -> Optional[TableMeta]:
        """Validate schema evolution and primary key constraints.

        Returns TableMeta if it was created or updated (caller should save),
        or None if no meta changes needed.
        """
        if not self._meta_store:
            return None

        meta = self._meta_store.load(table_name)
        meta_changed = False

        # Handle primary key
        if primary_key is not None:
            if meta.primary_key is not None and meta.primary_key != primary_key:
                raise ValueError(
                    f"Primary key already set to {meta.primary_key} for '{table_name}'. "
                    f"Primary keys are immutable once set."
                )
            self._validate_pk_columns(primary_key, table)
            meta.primary_key = primary_key
            meta_changed = True

        # Handle schema mode
        if schema_mode is not None:
            if schema_mode not in ("additive", "flexible"):
                raise ValueError(
                    f"Invalid schema_mode '{schema_mode}': must be 'additive' or 'flexible'"
                )
            meta.schema_mode = schema_mode
            meta_changed = True

        # Resolve effective schema mode
        effective_mode = schema_mode or meta.schema_mode

        # Schema evolution check: compare against latest version's schema
        existing_schema = self._get_latest_schema(table_name)
        if existing_schema is not None:
            result = compare_schemas(existing_schema, table.schema, effective_mode)
            if not result.compatible:
                raise SchemaEvolutionError(table_name, result.error_message)

        # Primary key uniqueness check
        effective_pk = meta.primary_key
        if effective_pk:
            self._check_uniqueness(table_name, table, effective_pk)

        return meta if meta_changed else None

    def _get_latest_schema(self, table_name: str) -> Optional[pa.Schema]:
        """Get the schema from the latest version, or None if no versions exist."""
        try:
            version = self.catalog.get_version(table_name, None)
            schema_json = version.metadata.get(SCHEMA_METADATA_KEY)
            if schema_json:
                return deserialize_schema(schema_json)
            return None
        except (OSError, Exception):
            return None

    @staticmethod
    def _validate_pk_columns(pk_columns: List[str], table: pa.Table) -> None:
        """Validate that all PK columns exist in the table."""
        table_cols = set(table.column_names)
        missing = [c for c in pk_columns if c not in table_cols]
        if missing:
            raise ValueError(
                f"Primary key columns not found in data: {missing}. "
                f"Available columns: {sorted(table_cols)}"
            )

    @staticmethod
    def _check_uniqueness(
        table_name: str,
        table: pa.Table,
        pk_columns: List[str],
    ) -> None:
        """Check that the primary key columns have no duplicate values.

        Uses a GROUP BY / HAVING approach to correctly handle NULLs.
        NULLs in PK columns are allowed but treated as distinct values
        (two NULLs do not conflict).
        """
        import duckdb

        con = duckdb.connect()
        con.register("__pk_check", table)
        cols = ", ".join(f'"{c}"' for c in pk_columns)
        result = con.execute(
            f"SELECT COUNT(*) FROM ("
            f"  SELECT {cols} FROM __pk_check "
            f"  GROUP BY {cols} HAVING COUNT(*) > 1"
            f")"
        ).fetchone()
        dup_groups = result[0]
        if dup_groups > 0:
            raise PrimaryKeyViolationError(
                table_name, pk_columns, dup_groups
            )

    def _get_next_version(self, table_name: str) -> int:
        """Get the next version number for a table."""
        from .exceptions import TableNotFoundError

        try:
            versions = self.catalog.list_versions(table_name)
            if versions:
                return max(versions) + 1
            return 1
        except TableNotFoundError:
            # Explicit table not found - this is the first version
            return 1
        except OSError as e:
            # Rust catalog raises OSError for "not found" - check message
            if "not found" in str(e).lower():
                return 1
            # Re-raise unexpected I/O errors (disk full, permissions, etc.)
            raise
