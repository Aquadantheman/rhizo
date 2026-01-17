"""
TableReader - Read versioned tables from content-addressable storage.

The reader handles:
1. Fetching table version metadata from the catalog
2. Retrieving chunks from the content-addressable store
3. Deserializing Parquet chunks to Arrow
4. Optionally concatenating into a single table
"""

from __future__ import annotations

import io
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional, Iterator, List

import pyarrow as pa
import pyarrow.parquet as pq

if TYPE_CHECKING:
    import pandas as pd


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
        >>> from armillaria import PyChunkStore, PyCatalog
        >>> from armillaria_query import TableReader
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
        """
        self.store = store
        self.catalog = catalog
        self.verify_integrity = verify_integrity
        self.use_mmap = use_mmap
        self.parallel_workers = parallel_workers

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
    ) -> pa.Table:
        """
        Read a table version as an Arrow Table.

        This materializes the entire table in memory. For large tables,
        consider using iter_chunks() instead.

        Args:
            table_name: Name of the table
            version: Specific version to read (None for latest)

        Returns:
            Arrow Table containing all data

        Raises:
            IOError: If table or version not found
            ValueError: If chunk data is corrupted
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
                arrow_chunks = list(executor.map(self._parquet_to_arrow, chunk_data_list))
        else:
            # Sequential parsing (default, or single chunk)
            arrow_chunks = [self._parquet_to_arrow(data) for data in chunk_data_list]

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
    ) -> Iterator[pa.Table]:
        """
        Iterate over table chunks as Arrow Tables.

        Memory-efficient for large tables - only one chunk in memory at a time.

        Args:
            table_name: Name of the table
            version: Specific version to read (None for latest)

        Yields:
            Arrow Tables, one per chunk

        Raises:
            IOError: If table or version not found
            ValueError: If chunk data is corrupted
        """
        metadata = self.get_metadata(table_name, version)

        for chunk_hash in metadata.chunk_hashes:
            chunk_data = self._fetch_chunk(chunk_hash)
            arrow_table = self._parquet_to_arrow(chunk_data)
            yield arrow_table

    def _fetch_chunk(self, chunk_hash: str) -> bytes:
        """Fetch chunk data from the store."""
        if self.use_mmap:
            return self.store.get_mmap(chunk_hash)
        elif self.verify_integrity:
            return self.store.get_verified(chunk_hash)
        else:
            return self.store.get(chunk_hash)

    def _parquet_to_arrow(self, data: bytes) -> pa.Table:
        """Deserialize Parquet bytes to Arrow Table."""
        buffer = io.BytesIO(data)
        return pq.read_table(buffer)

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
