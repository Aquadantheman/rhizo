"""
Integration tests for the Unified Data Runtime Python bindings.

Run with: pytest tests/test_rhizo.py -v
Requires: pip install maturin pytest
Build first: maturin develop
"""

import os
import tempfile
import shutil
import pytest

# Import will fail if the extension isn't built - that's expected
try:
    import _rhizo
    RHIZO_AVAILABLE = True
except ImportError:
    RHIZO_AVAILABLE = False


pytestmark = pytest.mark.skipif(not RHIZO_AVAILABLE, reason="rhizo extension not built")


@pytest.fixture
def temp_dir():
    """Create a temporary directory for tests."""
    dir_path = tempfile.mkdtemp(prefix="rhizo_pytest_")
    yield dir_path
    shutil.rmtree(dir_path, ignore_errors=True)


class TestChunkStore:
    """Tests for PyChunkStore."""

    def test_put_get_roundtrip(self, temp_dir):
        """Test basic put and get operations."""
        store = _rhizo.PyChunkStore(os.path.join(temp_dir, "chunks"))

        data = b"hello world"
        hash_str = store.put(data)

        assert len(hash_str) == 64  # BLAKE3 hash is 64 hex chars
        assert all(c in "0123456789abcdef" for c in hash_str)

        retrieved = store.get(hash_str)
        assert retrieved == data

    def test_deduplication(self, temp_dir):
        """Test that identical data produces the same hash."""
        store = _rhizo.PyChunkStore(os.path.join(temp_dir, "chunks"))

        data = b"same content"
        hash1 = store.put(data)
        hash2 = store.put(data)

        assert hash1 == hash2

    def test_exists(self, temp_dir):
        """Test the exists method."""
        store = _rhizo.PyChunkStore(os.path.join(temp_dir, "chunks"))

        data = b"test data"
        hash_str = store.put(data)

        assert store.exists(hash_str) is True
        # Valid format hash that doesn't exist
        fake_hash = "a" * 64
        assert store.exists(fake_hash) is False

    def test_delete(self, temp_dir):
        """Test the delete method."""
        store = _rhizo.PyChunkStore(os.path.join(temp_dir, "chunks"))

        data = b"to be deleted"
        hash_str = store.put(data)

        assert store.exists(hash_str) is True
        store.delete(hash_str)
        assert store.exists(hash_str) is False

    def test_get_verified(self, temp_dir):
        """Test the get_verified method."""
        store = _rhizo.PyChunkStore(os.path.join(temp_dir, "chunks"))

        data = b"verified data"
        hash_str = store.put(data)

        retrieved = store.get_verified(hash_str)
        assert retrieved == data

    def test_not_found_raises(self, temp_dir):
        """Test that getting a non-existent chunk raises an error."""
        store = _rhizo.PyChunkStore(os.path.join(temp_dir, "chunks"))

        fake_hash = "a" * 64
        with pytest.raises(IOError, match="not found"):
            store.get(fake_hash)

    def test_invalid_hash_raises(self, temp_dir):
        """Test that invalid hash format raises ValueError."""
        store = _rhizo.PyChunkStore(os.path.join(temp_dir, "chunks"))

        # Too short
        with pytest.raises(ValueError, match="Invalid hash"):
            store.get("abc")

        # Wrong length
        with pytest.raises(ValueError, match="Invalid hash"):
            store.get("a" * 32)

        # Non-hex characters
        with pytest.raises(ValueError, match="Invalid hash"):
            store.get("g" * 64)

    def test_large_data(self, temp_dir):
        """Test handling of large data."""
        store = _rhizo.PyChunkStore(os.path.join(temp_dir, "chunks"))

        # 1MB of data
        data = bytes(range(256)) * (1024 * 4)
        hash_str = store.put(data)
        retrieved = store.get(hash_str)

        assert len(retrieved) == len(data)
        assert retrieved == data

    def test_binary_data(self, temp_dir):
        """Test handling of binary data with all byte values."""
        store = _rhizo.PyChunkStore(os.path.join(temp_dir, "chunks"))

        data = bytes(range(256))
        hash_str = store.put(data)
        retrieved = store.get(hash_str)

        assert retrieved == data

    # ========== Batch Operations ==========

    def test_put_batch_empty(self, temp_dir):
        """Test put_batch with empty list."""
        store = _rhizo.PyChunkStore(os.path.join(temp_dir, "chunks"))

        hashes = store.put_batch([])
        assert hashes == []

    def test_put_batch_single(self, temp_dir):
        """Test put_batch with a single chunk."""
        store = _rhizo.PyChunkStore(os.path.join(temp_dir, "chunks"))

        data = b"single chunk"
        hashes = store.put_batch([data])

        assert len(hashes) == 1
        assert len(hashes[0]) == 64
        assert store.get(hashes[0]) == data

    def test_put_batch_multiple(self, temp_dir):
        """Test put_batch with multiple chunks."""
        store = _rhizo.PyChunkStore(os.path.join(temp_dir, "chunks"))

        chunks = [b"chunk1", b"chunk2", b"chunk3", b"chunk4"]
        hashes = store.put_batch(chunks)

        assert len(hashes) == 4
        for i, hash_str in enumerate(hashes):
            assert store.get(hash_str) == chunks[i]

    def test_put_batch_deduplication(self, temp_dir):
        """Test that put_batch correctly deduplicates identical content."""
        store = _rhizo.PyChunkStore(os.path.join(temp_dir, "chunks"))

        # Same content multiple times
        same_data = b"duplicate content"
        chunks = [same_data, same_data, same_data]
        hashes = store.put_batch(chunks)

        assert len(hashes) == 3
        # All hashes should be the same
        assert hashes[0] == hashes[1] == hashes[2]

    def test_put_batch_large(self, temp_dir):
        """Test put_batch with many chunks for parallelism."""
        store = _rhizo.PyChunkStore(os.path.join(temp_dir, "chunks"))

        # Create 100 unique chunks
        chunks = [f"chunk_{i}".encode() for i in range(100)]
        hashes = store.put_batch(chunks)

        assert len(hashes) == 100
        # Verify all are unique (since content is unique)
        assert len(set(hashes)) == 100

    def test_get_batch_empty(self, temp_dir):
        """Test get_batch with empty list."""
        store = _rhizo.PyChunkStore(os.path.join(temp_dir, "chunks"))

        results = store.get_batch([])
        assert results == []

    def test_get_batch_multiple(self, temp_dir):
        """Test get_batch with multiple hashes."""
        store = _rhizo.PyChunkStore(os.path.join(temp_dir, "chunks"))

        # Store chunks first
        chunks = [b"data_a", b"data_b", b"data_c"]
        hashes = store.put_batch(chunks)

        # Retrieve in different order
        retrieved = store.get_batch([hashes[2], hashes[0], hashes[1]])

        assert retrieved == [b"data_c", b"data_a", b"data_b"]

    def test_get_batch_not_found(self, temp_dir):
        """Test get_batch raises error if any hash not found."""
        store = _rhizo.PyChunkStore(os.path.join(temp_dir, "chunks"))

        hash1 = store.put(b"exists")
        fake_hash = "a" * 64

        with pytest.raises(IOError, match="not found"):
            store.get_batch([hash1, fake_hash])

    def test_get_batch_verified(self, temp_dir):
        """Test get_batch_verified method."""
        store = _rhizo.PyChunkStore(os.path.join(temp_dir, "chunks"))

        chunks = [b"verify_a", b"verify_b"]
        hashes = store.put_batch(chunks)

        retrieved = store.get_batch_verified(hashes)
        assert retrieved == chunks

    def test_put_batch_get_batch_roundtrip(self, temp_dir):
        """Test full roundtrip with batch operations."""
        store = _rhizo.PyChunkStore(os.path.join(temp_dir, "chunks"))

        # Create chunks of varying sizes
        chunks = [
            b"small",
            b"medium" * 100,
            b"large" * 10000,
            bytes(range(256)),
        ]

        hashes = store.put_batch(chunks)
        retrieved = store.get_batch_verified(hashes)

        assert len(retrieved) == len(chunks)
        for i, chunk in enumerate(chunks):
            assert retrieved[i] == chunk

    # ========== Memory-Mapped Operations ==========

    def test_get_mmap_basic(self, temp_dir):
        """Test get_mmap returns same data as get."""
        store = _rhizo.PyChunkStore(os.path.join(temp_dir, "chunks"))

        data = b"memory mapped data"
        hash_str = store.put(data)

        via_get = store.get(hash_str)
        via_mmap = store.get_mmap(hash_str)

        assert via_get == via_mmap == data

    def test_get_mmap_large(self, temp_dir):
        """Test get_mmap with large data."""
        store = _rhizo.PyChunkStore(os.path.join(temp_dir, "chunks"))

        # 1MB of data
        data = bytes(range(256)) * (1024 * 4)
        hash_str = store.put(data)

        retrieved = store.get_mmap(hash_str)
        assert retrieved == data

    def test_get_mmap_not_found(self, temp_dir):
        """Test get_mmap raises error for missing chunk."""
        store = _rhizo.PyChunkStore(os.path.join(temp_dir, "chunks"))

        fake_hash = "a" * 64
        with pytest.raises(IOError, match="not found"):
            store.get_mmap(fake_hash)

    def test_get_mmap_batch(self, temp_dir):
        """Test get_mmap_batch retrieves multiple chunks."""
        store = _rhizo.PyChunkStore(os.path.join(temp_dir, "chunks"))

        chunks = [b"mmap_one", b"mmap_two", b"mmap_three"]
        hashes = store.put_batch(chunks)

        retrieved = store.get_mmap_batch(hashes)

        assert len(retrieved) == 3
        assert retrieved == chunks

    def test_get_mmap_batch_empty(self, temp_dir):
        """Test get_mmap_batch with empty list."""
        store = _rhizo.PyChunkStore(os.path.join(temp_dir, "chunks"))

        result = store.get_mmap_batch([])
        assert result == []


class TestCatalog:
    """Tests for PyCatalog and PyTableVersion."""

    def test_commit_and_get(self, temp_dir):
        """Test committing and retrieving a version."""
        catalog = _rhizo.PyCatalog(os.path.join(temp_dir, "catalog"))

        version = _rhizo.PyTableVersion("test_table", 1, ["hash1", "hash2"])
        committed_version = catalog.commit(version)

        assert committed_version == 1

        retrieved = catalog.get_version("test_table", 1)
        assert retrieved.table_name == "test_table"
        assert retrieved.version == 1
        assert retrieved.chunk_hashes == ["hash1", "hash2"]

    def test_get_latest(self, temp_dir):
        """Test getting the latest version."""
        catalog = _rhizo.PyCatalog(os.path.join(temp_dir, "catalog"))

        catalog.commit(_rhizo.PyTableVersion("test_table", 1, ["v1"]))
        catalog.commit(_rhizo.PyTableVersion("test_table", 2, ["v2"]))
        catalog.commit(_rhizo.PyTableVersion("test_table", 3, ["v3"]))

        # Get latest (version=None)
        latest = catalog.get_version("test_table")
        assert latest.version == 3
        assert latest.chunk_hashes == ["v3"]

    def test_version_sequence_enforced(self, temp_dir):
        """Test that version numbers must be sequential."""
        catalog = _rhizo.PyCatalog(os.path.join(temp_dir, "catalog"))

        catalog.commit(_rhizo.PyTableVersion("test_table", 1, []))

        # Try to skip version 2
        with pytest.raises(ValueError, match="Invalid version"):
            catalog.commit(_rhizo.PyTableVersion("test_table", 3, []))

    def test_list_versions(self, temp_dir):
        """Test listing all versions of a table."""
        catalog = _rhizo.PyCatalog(os.path.join(temp_dir, "catalog"))

        catalog.commit(_rhizo.PyTableVersion("test_table", 1, []))
        catalog.commit(_rhizo.PyTableVersion("test_table", 2, []))
        catalog.commit(_rhizo.PyTableVersion("test_table", 3, []))

        versions = catalog.list_versions("test_table")
        assert versions == [1, 2, 3]

    def test_list_tables(self, temp_dir):
        """Test listing all tables."""
        catalog = _rhizo.PyCatalog(os.path.join(temp_dir, "catalog"))

        catalog.commit(_rhizo.PyTableVersion("alpha", 1, []))
        catalog.commit(_rhizo.PyTableVersion("beta", 1, []))
        catalog.commit(_rhizo.PyTableVersion("gamma", 1, []))

        tables = catalog.list_tables()
        assert tables == ["alpha", "beta", "gamma"]

    def test_time_travel(self, temp_dir):
        """Test accessing historical versions."""
        catalog = _rhizo.PyCatalog(os.path.join(temp_dir, "catalog"))

        catalog.commit(_rhizo.PyTableVersion("data", 1, ["old"]))
        catalog.commit(_rhizo.PyTableVersion("data", 2, ["new"]))

        v1 = catalog.get_version("data", 1)
        v2 = catalog.get_version("data", 2)

        assert v1.chunk_hashes == ["old"]
        assert v2.chunk_hashes == ["new"]

    def test_table_not_found(self, temp_dir):
        """Test that accessing a non-existent table raises an error."""
        catalog = _rhizo.PyCatalog(os.path.join(temp_dir, "catalog"))

        with pytest.raises(IOError, match="not found"):
            catalog.get_version("nonexistent")

        with pytest.raises(IOError, match="not found"):
            catalog.list_versions("nonexistent")

    def test_version_not_found(self, temp_dir):
        """Test that accessing a non-existent version raises an error."""
        catalog = _rhizo.PyCatalog(os.path.join(temp_dir, "catalog"))

        catalog.commit(_rhizo.PyTableVersion("test_table", 1, []))

        with pytest.raises(IOError, match="not found"):
            catalog.get_version("test_table", 999)

    def test_table_version_attributes(self, temp_dir):
        """Test PyTableVersion attributes."""
        version = _rhizo.PyTableVersion("my_table", 5, ["h1", "h2", "h3"])

        assert version.table_name == "my_table"
        assert version.version == 5
        assert version.chunk_hashes == ["h1", "h2", "h3"]
        assert version.parent_version == 4  # auto-computed
        assert version.created_at > 0  # timestamp
        assert version.schema_hash is None
        assert version.metadata == {}


class TestIntegration:
    """Integration tests combining ChunkStore and Catalog."""

    def test_full_workflow(self, temp_dir):
        """Test a complete workflow: store chunks, create versions, time travel."""
        store = _rhizo.PyChunkStore(os.path.join(temp_dir, "chunks"))
        catalog = _rhizo.PyCatalog(os.path.join(temp_dir, "catalog"))

        # Store some data chunks
        chunk1 = store.put(b"row1,row2,row3")
        chunk2 = store.put(b"row4,row5,row6")

        # Create version 1 with chunk1
        v1 = _rhizo.PyTableVersion("users", 1, [chunk1])
        catalog.commit(v1)

        # Store more data and create version 2
        chunk3 = store.put(b"row7,row8,row9")
        v2 = _rhizo.PyTableVersion("users", 2, [chunk1, chunk2, chunk3])
        catalog.commit(v2)

        # Time travel to version 1
        old_version = catalog.get_version("users", 1)
        assert len(old_version.chunk_hashes) == 1

        # Reconstruct the data from version 1
        data = b""
        for hash_str in old_version.chunk_hashes:
            data += store.get_verified(hash_str)
        assert data == b"row1,row2,row3"

        # Get latest version
        latest = catalog.get_version("users")
        assert len(latest.chunk_hashes) == 3

    def test_multiple_tables(self, temp_dir):
        """Test managing multiple tables."""
        store = _rhizo.PyChunkStore(os.path.join(temp_dir, "chunks"))
        catalog = _rhizo.PyCatalog(os.path.join(temp_dir, "catalog"))

        # Create multiple tables
        for table_name in ["users", "orders", "products"]:
            chunk = store.put(f"data for {table_name}".encode())
            version = _rhizo.PyTableVersion(table_name, 1, [chunk])
            catalog.commit(version)

        # Verify all tables exist
        tables = catalog.list_tables()
        assert set(tables) == {"users", "orders", "products"}

        # Each table has its own data
        for table_name in tables:
            version = catalog.get_version(table_name)
            data = store.get(version.chunk_hashes[0])
            assert data == f"data for {table_name}".encode()


# =============================================================================
# Phase 4: Native Parquet Encoder/Decoder Tests
# =============================================================================

class TestParquetEncoder:
    """Tests for PyParquetEncoder (Rust Parquet encoding)."""

    def test_encode_simple(self):
        """Test basic encoding of a RecordBatch."""
        import pyarrow as pa

        encoder = _rhizo.PyParquetEncoder()

        # Create test data
        ids = pa.array([1, 2, 3, 4, 5])
        values = pa.array([1.0, 2.0, 3.0, 4.0, 5.0])
        batch = pa.RecordBatch.from_arrays([ids, values], names=["id", "value"])

        # Encode
        parquet_bytes = encoder.encode(batch)

        # Verify we got valid Parquet (magic bytes)
        assert parquet_bytes[:4] == b"PAR1"
        assert len(parquet_bytes) > 0

    def test_encode_with_compression(self):
        """Test encoding with different compression types."""
        import pyarrow as pa

        ids = pa.array(list(range(1000)))
        values = pa.array([float(i) for i in range(1000)])
        batch = pa.RecordBatch.from_arrays([ids, values], names=["id", "value"])

        compressions = ["zstd", "snappy", "none"]
        sizes = []

        for compression in compressions:
            encoder = _rhizo.PyParquetEncoder(compression)
            parquet_bytes = encoder.encode(batch)
            sizes.append(len(parquet_bytes))

        # Uncompressed should be largest
        assert sizes[2] > sizes[0]  # none > zstd

    def test_encode_batch_parallel(self):
        """Test parallel encoding of multiple batches."""
        import pyarrow as pa

        encoder = _rhizo.PyParquetEncoder()

        # Create multiple batches
        batches = []
        for i in range(10):
            ids = pa.array(list(range(i * 100, (i + 1) * 100)))
            values = pa.array([float(x) for x in range(100)])
            batches.append(
                pa.RecordBatch.from_arrays([ids, values], names=["id", "value"])
            )

        # Encode all in parallel
        results = encoder.encode_batch(batches)

        assert len(results) == 10
        for r in results:
            assert r[:4] == b"PAR1"


class TestParquetDecoder:
    """Tests for PyParquetDecoder (Rust Parquet decoding)."""

    def test_decode_simple(self):
        """Test basic decoding."""
        import pyarrow as pa

        # Create and encode test data
        ids = pa.array([1, 2, 3, 4, 5])
        values = pa.array([1.0, 2.0, 3.0, 4.0, 5.0])
        original = pa.RecordBatch.from_arrays([ids, values], names=["id", "value"])

        encoder = _rhizo.PyParquetEncoder()
        parquet_bytes = encoder.encode(original)

        # Decode
        decoder = _rhizo.PyParquetDecoder()
        decoded = decoder.decode(parquet_bytes)

        assert decoded.num_rows == original.num_rows
        assert decoded.num_columns == original.num_columns

    def test_roundtrip_data_integrity(self):
        """Test that roundtrip preserves data exactly."""
        import pyarrow as pa
        import numpy as np

        np.random.seed(42)

        # Create test data with various types
        ids = pa.array(list(range(1000)))
        values = pa.array(np.random.random(1000))
        names = pa.array([f"item_{i}" for i in range(1000)])
        original = pa.RecordBatch.from_arrays(
            [ids, values, names], names=["id", "value", "name"]
        )

        # Roundtrip
        encoder = _rhizo.PyParquetEncoder()
        decoder = _rhizo.PyParquetDecoder()
        decoded = decoder.decode(encoder.encode(original))

        # Verify data integrity
        assert decoded.num_rows == 1000
        orig_ids = original.column(0).to_pylist()
        dec_ids = decoded.column(0).to_pylist()
        assert orig_ids == dec_ids

    def test_decode_batch_parallel(self):
        """Test parallel decoding of multiple chunks."""
        import pyarrow as pa

        encoder = _rhizo.PyParquetEncoder()
        decoder = _rhizo.PyParquetDecoder()

        # Create and encode multiple batches
        chunks = []
        for i in range(10):
            ids = pa.array(list(range(i * 100, (i + 1) * 100)))
            values = pa.array([float(x) for x in range(100)])
            batch = pa.RecordBatch.from_arrays([ids, values], names=["id", "value"])
            chunks.append(encoder.encode(batch))

        # Decode all in parallel
        decoded = decoder.decode_batch(chunks)

        assert len(decoded) == 10
        for d in decoded:
            assert d.num_rows == 100
