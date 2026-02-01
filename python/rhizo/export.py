"""
ExportEngine - Export Rhizo tables to Parquet, CSV, and JSON.

Provides efficient export with:
- Streaming Parquet export (one chunk in memory at a time)
- Single-chunk fast path (zero deserialize for single-chunk tables)
- Atomic writes (temp file + rename for crash safety)
- Format auto-detection from file extension

Example:
    >>> from rhizo.export import ExportEngine
    >>> engine = ExportEngine(reader, store, catalog)
    >>> result = engine.export_table("users", "users.parquet")
    >>> print(f"Exported {result.row_count} rows to {result.path}")
"""

from __future__ import annotations

import json
import os
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional, List, Generator

import pyarrow as pa
import pyarrow.csv as pcsv
import pyarrow.parquet as pq

from .exceptions import validate_table_name

if TYPE_CHECKING:
    from .reader import TableReader


SUPPORTED_FORMATS = {"parquet", "csv", "json"}

EXTENSION_MAP = {
    ".parquet": "parquet",
    ".pq": "parquet",
    ".csv": "csv",
    ".json": "json",
    ".jsonl": "json",
    ".ndjson": "json",
}


@dataclass
class ExportResult:
    """Result of an export operation.

    Attributes:
        path: Absolute path to the exported file.
        format: Export format used ('parquet', 'csv', or 'json').
        row_count: Number of rows exported.
        file_size_bytes: Size of the exported file in bytes.
        columns_exported: List of column names in the exported file.
    """
    path: str
    format: str
    row_count: int
    file_size_bytes: int
    columns_exported: List[str]


def detect_format(path: str, format: Optional[str] = None) -> str:
    """Detect export format from file extension or explicit parameter.

    Args:
        path: Output file path.
        format: Explicit format override. If provided, takes priority
                over the file extension.

    Returns:
        Format string: 'parquet', 'csv', or 'json'.

    Raises:
        ValueError: If format cannot be determined or is unsupported.
    """
    if format is not None:
        fmt = format.lower()
        if fmt not in SUPPORTED_FORMATS:
            raise ValueError(
                f"Unsupported export format: {format!r}. "
                f"Supported: {', '.join(sorted(SUPPORTED_FORMATS))}"
            )
        return fmt

    # Try file extension
    lower_path = path.lower()
    for ext, fmt in EXTENSION_MAP.items():
        if lower_path.endswith(ext):
            return fmt

    raise ValueError(
        f"Cannot detect export format from path: {path!r}. "
        f"Use a recognized extension ({', '.join(sorted(EXTENSION_MAP))}) "
        f"or pass format= explicitly."
    )


@contextmanager
def _atomic_write(target_path: str) -> Generator[str, None, None]:
    """Context manager for atomic file writes.

    Writes to a temp file in the same directory, then renames on success.
    Cleans up the temp file on failure.

    Args:
        target_path: Final destination path.

    Yields:
        Path to the temporary file to write to.
    """
    target_dir = os.path.dirname(os.path.abspath(target_path))
    os.makedirs(target_dir, exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(dir=target_dir, suffix=".rhizo_export_tmp")
    os.close(fd)
    try:
        yield tmp_path
        os.replace(tmp_path, target_path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


class ExportEngine:
    """Exports Rhizo tables to external file formats.

    Handles format detection, streaming writes, and atomic file operations.
    Uses the existing TableReader infrastructure for efficient chunk access.

    Args:
        reader: TableReader for accessing table data.
        store: PyChunkStore for raw chunk byte access.
        catalog: PyCatalog for version metadata.

    Example:
        >>> engine = ExportEngine(reader, store, catalog)
        >>> result = engine.export_table("users", "users.parquet")
        >>> result = engine.export_table("users", "users.csv", version=3)
        >>> result = engine.export_table("users", "out.parquet", columns=["id"])
    """

    def __init__(self, reader: "TableReader", store, catalog):
        self.reader = reader
        self.store = store
        self.catalog = catalog

    def export_table(
        self,
        table_name: str,
        path: str,
        *,
        version: Optional[int] = None,
        columns: Optional[List[str]] = None,
        format: Optional[str] = None,
        compression: Optional[str] = None,
    ) -> ExportResult:
        """Export a table version to a file.

        Args:
            table_name: Name of the table to export.
            path: Output file path. Format is auto-detected from extension
                  unless ``format`` is specified.
            version: Table version to export. None for latest.
            columns: Columns to include. None for all columns.
            format: Explicit format ('parquet', 'csv', 'json').
            compression: Parquet compression codec (default: 'zstd').
                        Ignored for CSV and JSON.

        Returns:
            ExportResult with export metadata.

        Raises:
            IOError: If the table or version does not exist.
            ValueError: If the format is unsupported or columns are invalid.

        Example:
            >>> result = engine.export_table("users", "users.parquet")
            >>> result = engine.export_table("users", "data.csv", version=2)
            >>> result = engine.export_table("users", "out.pq", columns=["id", "name"])
        """
        table_name = validate_table_name(table_name)
        fmt = detect_format(path, format)
        abs_path = os.path.abspath(path)

        if fmt == "parquet":
            return self._export_parquet(
                table_name, abs_path, version, columns, compression
            )
        else:
            # CSV and JSON: materialize Arrow table then write
            table = self.reader.read_arrow(table_name, version, columns=columns)
            return self._write_format(table, abs_path, fmt)

    def export_arrow(
        self,
        arrow_table: pa.Table,
        path: str,
        *,
        format: Optional[str] = None,
        compression: Optional[str] = None,
    ) -> ExportResult:
        """Export an Arrow table to a file.

        Useful for exporting SQL query results.

        Args:
            arrow_table: Arrow table to export.
            path: Output file path.
            format: Explicit format ('parquet', 'csv', 'json').
            compression: Parquet compression codec (default: 'zstd').

        Returns:
            ExportResult with export metadata.

        Example:
            >>> result = engine.export_arrow(query_result, "filtered.parquet")
        """
        fmt = detect_format(path, format)
        abs_path = os.path.abspath(path)

        if fmt == "parquet":
            compression = compression or "zstd"
            with _atomic_write(abs_path) as tmp_path:
                pq.write_table(arrow_table, tmp_path, compression=compression)
            return ExportResult(
                path=abs_path,
                format="parquet",
                row_count=arrow_table.num_rows,
                file_size_bytes=os.path.getsize(abs_path),
                columns_exported=arrow_table.column_names,
            )
        else:
            return self._write_format(arrow_table, abs_path, fmt)

    def _export_parquet(
        self,
        table_name: str,
        path: str,
        version: Optional[int],
        columns: Optional[List[str]],
        compression: Optional[str],
    ) -> ExportResult:
        """Export table to Parquet with streaming and fast-path optimizations."""
        compression = compression or "zstd"
        metadata = self.reader.get_metadata(table_name, version)

        # Fast path: single chunk, no projection — raw byte copy
        if metadata.chunk_count == 1 and columns is None:
            return self._export_parquet_single_chunk(
                metadata.chunk_hashes[0], path
            )

        # Streaming path: iterate chunks through ParquetWriter
        row_count = 0
        writer = None
        exported_columns = None

        with _atomic_write(path) as tmp_path:
            for chunk_table in self.reader.iter_chunks(
                table_name, version, columns=columns
            ):
                if writer is None:
                    writer = pq.ParquetWriter(
                        tmp_path, chunk_table.schema, compression=compression
                    )
                    exported_columns = chunk_table.column_names
                writer.write_table(chunk_table)
                row_count += chunk_table.num_rows

            if writer is not None:
                writer.close()
            else:
                # Empty table: write an empty Parquet file with schema
                # Read schema from the first chunk's metadata
                first_chunk_data = self.store.get(metadata.chunk_hashes[0])
                schema = pq.read_schema(pa.BufferReader(first_chunk_data))
                if columns is not None:
                    schema = pa.schema(
                        [schema.field(c) for c in columns]
                    )
                empty_table = pa.table(
                    {name: pa.array([], type=schema.field(name).type)
                     for name in schema.names}
                )
                pq.write_table(empty_table, tmp_path, compression=compression)
                exported_columns = list(schema.names)

        return ExportResult(
            path=path,
            format="parquet",
            row_count=row_count,
            file_size_bytes=os.path.getsize(path),
            columns_exported=exported_columns or [],
        )

    def _export_parquet_single_chunk(
        self,
        chunk_hash: str,
        path: str,
    ) -> ExportResult:
        """Fast path: copy raw Parquet bytes for single-chunk tables."""
        chunk_data = self.store.get(chunk_hash)

        with _atomic_write(path) as tmp_path:
            with open(tmp_path, "wb") as f:
                f.write(chunk_data)

        # Read metadata from the written file for the result
        pf = pq.ParquetFile(path)
        row_count = pf.metadata.num_rows
        schema = pf.schema_arrow

        return ExportResult(
            path=path,
            format="parquet",
            row_count=row_count,
            file_size_bytes=os.path.getsize(path),
            columns_exported=schema.names,
        )

    def _write_format(
        self,
        table: pa.Table,
        path: str,
        fmt: str,
    ) -> ExportResult:
        """Write an Arrow table to CSV or JSON."""
        with _atomic_write(path) as tmp_path:
            if fmt == "csv":
                pcsv.write_csv(table, tmp_path)
            elif fmt == "json":
                records = table.to_pylist()
                with open(tmp_path, "w", encoding="utf-8") as f:
                    json.dump(records, f, default=str)
            else:
                raise ValueError(f"Unsupported format: {fmt!r}")

        return ExportResult(
            path=path,
            format=fmt,
            row_count=table.num_rows,
            file_size_bytes=os.path.getsize(path),
            columns_exported=table.column_names,
        )
