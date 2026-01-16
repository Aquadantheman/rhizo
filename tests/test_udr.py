"""
Integration tests for the Unified Data Runtime Python bindings.

Run with: pytest tests/test_udr.py -v
Requires: pip install maturin pytest
Build first: maturin develop
"""

import os
import tempfile
import shutil
import pytest

# Import will fail if the extension isn't built - that's expected
try:
    import udr
    UDR_AVAILABLE = True
except ImportError:
    UDR_AVAILABLE = False


pytestmark = pytest.mark.skipif(not UDR_AVAILABLE, reason="udr extension not built")


@pytest.fixture
def temp_dir():
    """Create a temporary directory for tests."""
    dir_path = tempfile.mkdtemp(prefix="udr_pytest_")
    yield dir_path
    shutil.rmtree(dir_path, ignore_errors=True)


class TestChunkStore:
    """Tests for PyChunkStore."""

    def test_put_get_roundtrip(self, temp_dir):
        """Test basic put and get operations."""
        store = udr.PyChunkStore(os.path.join(temp_dir, "chunks"))

        data = b"hello world"
        hash_str = store.put(data)

        assert len(hash_str) == 64  # BLAKE3 hash is 64 hex chars
        assert all(c in "0123456789abcdef" for c in hash_str)

        retrieved = store.get(hash_str)
        assert retrieved == data

    def test_deduplication(self, temp_dir):
        """Test that identical data produces the same hash."""
        store = udr.PyChunkStore(os.path.join(temp_dir, "chunks"))

        data = b"same content"
        hash1 = store.put(data)
        hash2 = store.put(data)

        assert hash1 == hash2

    def test_exists(self, temp_dir):
        """Test the exists method."""
        store = udr.PyChunkStore(os.path.join(temp_dir, "chunks"))

        data = b"test data"
        hash_str = store.put(data)

        assert store.exists(hash_str) is True
        # Valid format hash that doesn't exist
        fake_hash = "a" * 64
        assert store.exists(fake_hash) is False

    def test_delete(self, temp_dir):
        """Test the delete method."""
        store = udr.PyChunkStore(os.path.join(temp_dir, "chunks"))

        data = b"to be deleted"
        hash_str = store.put(data)

        assert store.exists(hash_str) is True
        store.delete(hash_str)
        assert store.exists(hash_str) is False

    def test_get_verified(self, temp_dir):
        """Test the get_verified method."""
        store = udr.PyChunkStore(os.path.join(temp_dir, "chunks"))

        data = b"verified data"
        hash_str = store.put(data)

        retrieved = store.get_verified(hash_str)
        assert retrieved == data

    def test_not_found_raises(self, temp_dir):
        """Test that getting a non-existent chunk raises an error."""
        store = udr.PyChunkStore(os.path.join(temp_dir, "chunks"))

        fake_hash = "a" * 64
        with pytest.raises(IOError, match="not found"):
            store.get(fake_hash)

    def test_invalid_hash_raises(self, temp_dir):
        """Test that invalid hash format raises ValueError."""
        store = udr.PyChunkStore(os.path.join(temp_dir, "chunks"))

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
        store = udr.PyChunkStore(os.path.join(temp_dir, "chunks"))

        # 1MB of data
        data = bytes(range(256)) * (1024 * 4)
        hash_str = store.put(data)
        retrieved = store.get(hash_str)

        assert len(retrieved) == len(data)
        assert retrieved == data

    def test_binary_data(self, temp_dir):
        """Test handling of binary data with all byte values."""
        store = udr.PyChunkStore(os.path.join(temp_dir, "chunks"))

        data = bytes(range(256))
        hash_str = store.put(data)
        retrieved = store.get(hash_str)

        assert retrieved == data


class TestCatalog:
    """Tests for PyCatalog and PyTableVersion."""

    def test_commit_and_get(self, temp_dir):
        """Test committing and retrieving a version."""
        catalog = udr.PyCatalog(os.path.join(temp_dir, "catalog"))

        version = udr.PyTableVersion("test_table", 1, ["hash1", "hash2"])
        committed_version = catalog.commit(version)

        assert committed_version == 1

        retrieved = catalog.get_version("test_table", 1)
        assert retrieved.table_name == "test_table"
        assert retrieved.version == 1
        assert retrieved.chunk_hashes == ["hash1", "hash2"]

    def test_get_latest(self, temp_dir):
        """Test getting the latest version."""
        catalog = udr.PyCatalog(os.path.join(temp_dir, "catalog"))

        catalog.commit(udr.PyTableVersion("test_table", 1, ["v1"]))
        catalog.commit(udr.PyTableVersion("test_table", 2, ["v2"]))
        catalog.commit(udr.PyTableVersion("test_table", 3, ["v3"]))

        # Get latest (version=None)
        latest = catalog.get_version("test_table")
        assert latest.version == 3
        assert latest.chunk_hashes == ["v3"]

    def test_version_sequence_enforced(self, temp_dir):
        """Test that version numbers must be sequential."""
        catalog = udr.PyCatalog(os.path.join(temp_dir, "catalog"))

        catalog.commit(udr.PyTableVersion("test_table", 1, []))

        # Try to skip version 2
        with pytest.raises(ValueError, match="Invalid version"):
            catalog.commit(udr.PyTableVersion("test_table", 3, []))

    def test_list_versions(self, temp_dir):
        """Test listing all versions of a table."""
        catalog = udr.PyCatalog(os.path.join(temp_dir, "catalog"))

        catalog.commit(udr.PyTableVersion("test_table", 1, []))
        catalog.commit(udr.PyTableVersion("test_table", 2, []))
        catalog.commit(udr.PyTableVersion("test_table", 3, []))

        versions = catalog.list_versions("test_table")
        assert versions == [1, 2, 3]

    def test_list_tables(self, temp_dir):
        """Test listing all tables."""
        catalog = udr.PyCatalog(os.path.join(temp_dir, "catalog"))

        catalog.commit(udr.PyTableVersion("alpha", 1, []))
        catalog.commit(udr.PyTableVersion("beta", 1, []))
        catalog.commit(udr.PyTableVersion("gamma", 1, []))

        tables = catalog.list_tables()
        assert tables == ["alpha", "beta", "gamma"]

    def test_time_travel(self, temp_dir):
        """Test accessing historical versions."""
        catalog = udr.PyCatalog(os.path.join(temp_dir, "catalog"))

        catalog.commit(udr.PyTableVersion("data", 1, ["old"]))
        catalog.commit(udr.PyTableVersion("data", 2, ["new"]))

        v1 = catalog.get_version("data", 1)
        v2 = catalog.get_version("data", 2)

        assert v1.chunk_hashes == ["old"]
        assert v2.chunk_hashes == ["new"]

    def test_table_not_found(self, temp_dir):
        """Test that accessing a non-existent table raises an error."""
        catalog = udr.PyCatalog(os.path.join(temp_dir, "catalog"))

        with pytest.raises(IOError, match="not found"):
            catalog.get_version("nonexistent")

        with pytest.raises(IOError, match="not found"):
            catalog.list_versions("nonexistent")

    def test_version_not_found(self, temp_dir):
        """Test that accessing a non-existent version raises an error."""
        catalog = udr.PyCatalog(os.path.join(temp_dir, "catalog"))

        catalog.commit(udr.PyTableVersion("test_table", 1, []))

        with pytest.raises(IOError, match="not found"):
            catalog.get_version("test_table", 999)

    def test_table_version_attributes(self, temp_dir):
        """Test PyTableVersion attributes."""
        version = udr.PyTableVersion("my_table", 5, ["h1", "h2", "h3"])

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
        store = udr.PyChunkStore(os.path.join(temp_dir, "chunks"))
        catalog = udr.PyCatalog(os.path.join(temp_dir, "catalog"))

        # Store some data chunks
        chunk1 = store.put(b"row1,row2,row3")
        chunk2 = store.put(b"row4,row5,row6")

        # Create version 1 with chunk1
        v1 = udr.PyTableVersion("users", 1, [chunk1])
        catalog.commit(v1)

        # Store more data and create version 2
        chunk3 = store.put(b"row7,row8,row9")
        v2 = udr.PyTableVersion("users", 2, [chunk1, chunk2, chunk3])
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
        store = udr.PyChunkStore(os.path.join(temp_dir, "chunks"))
        catalog = udr.PyCatalog(os.path.join(temp_dir, "catalog"))

        # Create multiple tables
        for table_name in ["users", "orders", "products"]:
            chunk = store.put(f"data for {table_name}".encode())
            version = udr.PyTableVersion(table_name, 1, [chunk])
            catalog.commit(version)

        # Verify all tables exist
        tables = catalog.list_tables()
        assert set(tables) == {"users", "orders", "products"}

        # Each table has its own data
        for table_name in tables:
            version = catalog.get_version(table_name)
            data = store.get(version.chunk_hashes[0])
            assert data == f"data for {table_name}".encode()
