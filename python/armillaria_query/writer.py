"""
TableWriter - Write DataFrames/Arrow tables as versioned, chunked Parquet.

The writer handles:
1. Converting input data to Arrow format
2. Chunking large tables for efficient storage
3. Serializing chunks as Parquet
4. Storing chunks in the content-addressable store
5. Committing table versions to the catalog
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional, Union, List, Dict, Any

import pyarrow as pa
import pyarrow.parquet as pq

if TYPE_CHECKING:
    import pandas as pd


# Default chunk size: 64MB uncompressed (will be smaller after Parquet compression)
DEFAULT_CHUNK_SIZE_BYTES = 64 * 1024 * 1024
# Default rows per chunk if byte estimation fails
DEFAULT_CHUNK_SIZE_ROWS = 100_000


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
    Writes data to Armillaria as versioned, content-addressable Parquet chunks.

    Example:
        >>> from armillaria import PyChunkStore, PyCatalog
        >>> from armillaria_query import TableWriter
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
    ):
        """
        Initialize the TableWriter.

        Args:
            store: PyChunkStore instance for content-addressable storage
            catalog: PyCatalog instance for version metadata
            chunk_size_bytes: Target size per chunk in bytes (default 64MB)
            chunk_size_rows: Optional fixed row count per chunk (overrides byte-based)
        """
        self.store = store
        self.catalog = catalog
        self.chunk_size_bytes = chunk_size_bytes
        self.chunk_size_rows = chunk_size_rows

    def write(
        self,
        table_name: str,
        data: Union["pd.DataFrame", pa.Table],
        metadata: Optional[Dict[str, str]] = None,
    ) -> WriteResult:
        """
        Write data as a new version of the specified table.

        Args:
            table_name: Name of the table to write
            data: DataFrame or Arrow Table to write
            metadata: Optional key-value metadata for this version

        Returns:
            WriteResult with version info and statistics

        Raises:
            ValueError: If data is empty or invalid
        """
        # Convert to Arrow Table if needed
        table = self._to_arrow(data)

        if table.num_rows == 0:
            raise ValueError("Cannot write empty table")

        # Determine chunking strategy
        chunks = self._chunk_table(table)

        # Store each chunk and collect hashes
        chunk_hashes = []
        total_bytes = 0

        for chunk in chunks:
            parquet_bytes = self._to_parquet_bytes(chunk)
            chunk_hash = self.store.put(parquet_bytes)
            chunk_hashes.append(chunk_hash)
            total_bytes += len(parquet_bytes)

        # Determine the next version number
        version = self._get_next_version(table_name)

        # Import here to avoid circular dependency
        import armillaria

        # Create and commit the version
        table_version = armillaria.PyTableVersion(table_name, version, chunk_hashes)
        # Note: metadata handling would require extending PyTableVersion

        committed_version = self.catalog.commit(table_version)

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
    ) -> ChunkWriteResult:
        """
        Write data chunks without committing to catalog.

        This is used by transactions to separate the chunk write phase
        from the catalog commit phase. The TransactionManager handles
        the catalog commit for atomicity.

        Args:
            table_name: Name of the table to write
            data: DataFrame or Arrow Table to write

        Returns:
            ChunkWriteResult with chunk info and the version that will be assigned

        Raises:
            ValueError: If data is empty or invalid
        """
        # Convert to Arrow Table if needed
        table = self._to_arrow(data)

        if table.num_rows == 0:
            raise ValueError("Cannot write empty table")

        # Determine chunking strategy
        chunks = self._chunk_table(table)

        # Store each chunk and collect hashes
        chunk_hashes = []
        total_bytes = 0

        for chunk in chunks:
            parquet_bytes = self._to_parquet_bytes(chunk)
            chunk_hash = self.store.put(parquet_bytes)
            chunk_hashes.append(chunk_hash)
            total_bytes += len(parquet_bytes)

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
        buffer = io.BytesIO()
        pq.write_table(
            table,
            buffer,
            compression="zstd",  # Good balance of speed and compression
            write_statistics=True,  # Enables query optimization
        )
        return buffer.getvalue()

    def _get_next_version(self, table_name: str) -> int:
        """Get the next version number for a table."""
        try:
            versions = self.catalog.list_versions(table_name)
            if versions:
                return max(versions) + 1
            return 1
        except Exception:
            # Table doesn't exist yet
            return 1
