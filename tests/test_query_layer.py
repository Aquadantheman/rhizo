"""
Tests for the UDR Query Layer.

Run with: pytest tests/test_query_layer.py -v
Requires:
    - maturin develop (build udr extension)
    - pip install pyarrow duckdb pandas pytest
"""

import os
import tempfile
import shutil

import pytest
import pyarrow as pa
import pandas as pd

# Try to import - will fail if dependencies aren't installed
# Note: conftest.py adds python/ to sys.path
try:
    import udr
    from udr_query import TableWriter, TableReader, QueryEngine
    DEPS_AVAILABLE = True
except ImportError as e:
    DEPS_AVAILABLE = False
    IMPORT_ERROR = str(e)


pytestmark = pytest.mark.skipif(
    not DEPS_AVAILABLE,
    reason=f"Dependencies not available: {IMPORT_ERROR if not DEPS_AVAILABLE else ''}"
)


@pytest.fixture
def temp_storage():
    """Create temporary storage directories for testing."""
    base_dir = tempfile.mkdtemp(prefix="udr_query_test_")
    chunks_dir = os.path.join(base_dir, "chunks")
    catalog_dir = os.path.join(base_dir, "catalog")

    store = udr.PyChunkStore(chunks_dir)
    catalog = udr.PyCatalog(catalog_dir)

    yield store, catalog, base_dir

    # Cleanup
    shutil.rmtree(base_dir, ignore_errors=True)


@pytest.fixture
def sample_dataframe():
    """Create a sample pandas DataFrame for testing."""
    return pd.DataFrame({
        "id": [1, 2, 3, 4, 5],
        "name": ["Alice", "Bob", "Charlie", "Diana", "Eve"],
        "age": [25, 30, 35, 28, 32],
        "score": [85.5, 92.0, 78.5, 95.0, 88.5],
    })


@pytest.fixture
def large_dataframe():
    """Create a larger DataFrame for chunking tests."""
    import numpy as np
    n = 50000
    return pd.DataFrame({
        "id": range(n),
        "value": np.random.randn(n),
        "category": np.random.choice(["A", "B", "C"], n),
    })


class TestTableWriter:
    """Tests for TableWriter."""

    def test_write_basic(self, temp_storage, sample_dataframe):
        """Test basic write operation."""
        store, catalog, _ = temp_storage
        writer = TableWriter(store, catalog)

        result = writer.write("test_table", sample_dataframe)

        assert result.table_name == "test_table"
        assert result.version == 1
        assert result.chunk_count >= 1
        assert result.total_rows == 5
        assert len(result.chunk_hashes) == result.chunk_count

    def test_write_creates_version(self, temp_storage, sample_dataframe):
        """Test that write creates proper version in catalog."""
        store, catalog, _ = temp_storage
        writer = TableWriter(store, catalog)

        writer.write("test_table", sample_dataframe)

        # Verify version exists in catalog
        version = catalog.get_version("test_table")
        assert version.version == 1
        assert version.table_name == "test_table"
        assert len(version.chunk_hashes) >= 1

    def test_write_multiple_versions(self, temp_storage, sample_dataframe):
        """Test writing multiple versions of a table."""
        store, catalog, _ = temp_storage
        writer = TableWriter(store, catalog)

        # Write version 1
        result1 = writer.write("test_table", sample_dataframe)
        assert result1.version == 1

        # Modify data and write version 2
        df2 = sample_dataframe.copy()
        df2["age"] = df2["age"] + 1
        result2 = writer.write("test_table", df2)
        assert result2.version == 2

        # Write version 3
        df3 = sample_dataframe.copy()
        df3["score"] = df3["score"] * 1.1
        result3 = writer.write("test_table", df3)
        assert result3.version == 3

        # Verify all versions exist
        versions = catalog.list_versions("test_table")
        assert versions == [1, 2, 3]

    def test_write_arrow_table(self, temp_storage, sample_dataframe):
        """Test writing Arrow Table directly."""
        store, catalog, _ = temp_storage
        writer = TableWriter(store, catalog)

        arrow_table = pa.Table.from_pandas(sample_dataframe)
        result = writer.write("test_table", arrow_table)

        assert result.total_rows == 5

    def test_write_chunking(self, temp_storage, large_dataframe):
        """Test that large tables are chunked."""
        store, catalog, _ = temp_storage
        # Use small chunk size to force multiple chunks
        writer = TableWriter(store, catalog, chunk_size_rows=10000)

        result = writer.write("large_table", large_dataframe)

        assert result.chunk_count > 1
        assert result.total_rows == len(large_dataframe)

    def test_write_empty_fails(self, temp_storage):
        """Test that writing empty DataFrame raises error."""
        store, catalog, _ = temp_storage
        writer = TableWriter(store, catalog)

        empty_df = pd.DataFrame({"id": [], "value": []})

        with pytest.raises(ValueError, match="empty"):
            writer.write("test_table", empty_df)


class TestTableReader:
    """Tests for TableReader."""

    def test_read_arrow(self, temp_storage, sample_dataframe):
        """Test reading table as Arrow."""
        store, catalog, _ = temp_storage
        writer = TableWriter(store, catalog)
        reader = TableReader(store, catalog)

        writer.write("test_table", sample_dataframe)
        arrow_table = reader.read_arrow("test_table")

        assert arrow_table.num_rows == 5
        assert "id" in arrow_table.column_names
        assert "name" in arrow_table.column_names

    def test_read_pandas(self, temp_storage, sample_dataframe):
        """Test reading table as pandas DataFrame."""
        store, catalog, _ = temp_storage
        writer = TableWriter(store, catalog)
        reader = TableReader(store, catalog)

        writer.write("test_table", sample_dataframe)
        df = reader.read_pandas("test_table")

        pd.testing.assert_frame_equal(
            df.reset_index(drop=True),
            sample_dataframe.reset_index(drop=True)
        )

    def test_read_specific_version(self, temp_storage, sample_dataframe):
        """Test reading a specific version (time travel)."""
        store, catalog, _ = temp_storage
        writer = TableWriter(store, catalog)
        reader = TableReader(store, catalog)

        # Write version 1
        writer.write("test_table", sample_dataframe)

        # Write version 2 with different data
        df2 = sample_dataframe.copy()
        df2["age"] = df2["age"] + 10
        writer.write("test_table", df2)

        # Read version 1 (time travel)
        df_v1 = reader.read_pandas("test_table", version=1)
        assert df_v1["age"].tolist() == [25, 30, 35, 28, 32]

        # Read version 2 (latest)
        df_v2 = reader.read_pandas("test_table", version=2)
        assert df_v2["age"].tolist() == [35, 40, 45, 38, 42]

        # Read latest (should be v2)
        df_latest = reader.read_pandas("test_table")
        assert df_latest["age"].tolist() == [35, 40, 45, 38, 42]

    def test_read_chunked_table(self, temp_storage, large_dataframe):
        """Test reading a multi-chunk table."""
        store, catalog, _ = temp_storage
        writer = TableWriter(store, catalog, chunk_size_rows=10000)
        reader = TableReader(store, catalog)

        writer.write("large_table", large_dataframe)
        df = reader.read_pandas("large_table")

        assert len(df) == len(large_dataframe)

    def test_iter_chunks(self, temp_storage, large_dataframe):
        """Test iterating over chunks."""
        store, catalog, _ = temp_storage
        writer = TableWriter(store, catalog, chunk_size_rows=10000)
        reader = TableReader(store, catalog)

        writer.write("large_table", large_dataframe)

        total_rows = 0
        chunk_count = 0
        for chunk in reader.iter_chunks("large_table"):
            total_rows += chunk.num_rows
            chunk_count += 1

        assert total_rows == len(large_dataframe)
        assert chunk_count > 1

    def test_get_metadata(self, temp_storage, sample_dataframe):
        """Test getting table metadata."""
        store, catalog, _ = temp_storage
        writer = TableWriter(store, catalog)
        reader = TableReader(store, catalog)

        writer.write("test_table", sample_dataframe)
        metadata = reader.get_metadata("test_table")

        assert metadata.table_name == "test_table"
        assert metadata.version == 1
        assert metadata.chunk_count >= 1

    def test_list_versions(self, temp_storage, sample_dataframe):
        """Test listing table versions."""
        store, catalog, _ = temp_storage
        writer = TableWriter(store, catalog)
        reader = TableReader(store, catalog)

        writer.write("test_table", sample_dataframe)
        writer.write("test_table", sample_dataframe)
        writer.write("test_table", sample_dataframe)

        versions = reader.list_versions("test_table")
        assert versions == [1, 2, 3]

    def test_list_tables(self, temp_storage, sample_dataframe):
        """Test listing all tables."""
        store, catalog, _ = temp_storage
        writer = TableWriter(store, catalog)
        reader = TableReader(store, catalog)

        writer.write("alpha", sample_dataframe)
        writer.write("beta", sample_dataframe)
        writer.write("gamma", sample_dataframe)

        tables = reader.list_tables()
        assert set(tables) == {"alpha", "beta", "gamma"}

    def test_table_not_found(self, temp_storage):
        """Test reading non-existent table raises error."""
        store, catalog, _ = temp_storage
        reader = TableReader(store, catalog)

        with pytest.raises(IOError):
            reader.read_arrow("nonexistent")


class TestQueryEngine:
    """Tests for QueryEngine."""

    def test_simple_query(self, temp_storage, sample_dataframe):
        """Test basic SQL query."""
        store, catalog, _ = temp_storage
        engine = QueryEngine(store, catalog)

        engine.write_table("users", sample_dataframe)
        result = engine.query("SELECT * FROM users")

        assert result.row_count == 5
        assert "id" in result.column_names

    def test_query_with_filter(self, temp_storage, sample_dataframe):
        """Test SQL query with WHERE clause."""
        store, catalog, _ = temp_storage
        engine = QueryEngine(store, catalog)

        engine.write_table("users", sample_dataframe)
        result = engine.query("SELECT * FROM users WHERE age > 30")

        assert result.row_count == 2  # Charlie (35) and Eve (32)

    def test_query_with_aggregation(self, temp_storage, sample_dataframe):
        """Test SQL query with aggregation."""
        store, catalog, _ = temp_storage
        engine = QueryEngine(store, catalog)

        engine.write_table("users", sample_dataframe)
        result = engine.query("SELECT AVG(age) as avg_age FROM users")

        df = result.to_pandas()
        assert abs(df["avg_age"].iloc[0] - 30.0) < 0.01

    def test_time_travel_query(self, temp_storage, sample_dataframe):
        """Test querying specific version (time travel)."""
        store, catalog, _ = temp_storage
        engine = QueryEngine(store, catalog)

        # Write version 1
        engine.write_table("users", sample_dataframe)

        # Write version 2 with higher ages
        df2 = sample_dataframe.copy()
        df2["age"] = df2["age"] + 10
        engine.write_table("users", df2)

        # Query version 1 (time travel)
        result_v1 = engine.query(
            "SELECT AVG(age) as avg_age FROM users",
            versions={"users": 1}
        )
        avg_v1 = result_v1.to_pandas()["avg_age"].iloc[0]

        # Query version 2 (latest)
        result_v2 = engine.query(
            "SELECT AVG(age) as avg_age FROM users",
            versions={"users": 2}
        )
        avg_v2 = result_v2.to_pandas()["avg_age"].iloc[0]

        # Version 2 should have +10 average age
        assert abs(avg_v2 - avg_v1 - 10.0) < 0.01

    def test_query_latest_by_default(self, temp_storage, sample_dataframe):
        """Test that queries use latest version by default."""
        store, catalog, _ = temp_storage
        engine = QueryEngine(store, catalog)

        # Write version 1 with 5 rows
        engine.write_table("users", sample_dataframe)

        # Write version 2 with 3 rows
        df2 = sample_dataframe.head(3)
        engine.write_table("users", df2)

        # Query without version should use latest (3 rows)
        result = engine.query("SELECT COUNT(*) as cnt FROM users")
        assert result.to_pandas()["cnt"].iloc[0] == 3

    def test_join_query(self, temp_storage):
        """Test SQL query with JOIN."""
        store, catalog, _ = temp_storage
        engine = QueryEngine(store, catalog)

        users = pd.DataFrame({
            "id": [1, 2, 3],
            "name": ["Alice", "Bob", "Charlie"],
        })
        orders = pd.DataFrame({
            "order_id": [101, 102, 103],
            "user_id": [1, 1, 2],
            "amount": [50.0, 30.0, 100.0],
        })

        engine.write_table("users", users)
        engine.write_table("orders", orders)

        result = engine.query("""
            SELECT u.name, SUM(o.amount) as total
            FROM users u
            JOIN orders o ON u.id = o.user_id
            GROUP BY u.name
            ORDER BY total DESC
        """)

        df = result.to_pandas()
        assert df.iloc[0]["name"] == "Bob"  # 100.0
        assert df.iloc[1]["name"] == "Alice"  # 80.0

    def test_result_formats(self, temp_storage, sample_dataframe):
        """Test different result formats."""
        store, catalog, _ = temp_storage
        engine = QueryEngine(store, catalog)

        engine.write_table("users", sample_dataframe)
        result = engine.query("SELECT id, name FROM users WHERE id = 1")

        # Test to_pandas
        df = result.to_pandas()
        assert isinstance(df, pd.DataFrame)
        assert len(df) == 1

        # Test to_dict
        dicts = result.to_dict()
        assert isinstance(dicts, list)
        assert dicts[0]["name"] == "Alice"

        # Test to_arrow
        arrow = result.to_arrow()
        assert isinstance(arrow, pa.Table)

    def test_diff_versions(self, temp_storage, sample_dataframe):
        """Test comparing two versions."""
        store, catalog, _ = temp_storage
        engine = QueryEngine(store, catalog)

        # Write version 1
        engine.write_table("users", sample_dataframe)

        # Write version 2 with additional row
        df2 = pd.concat([sample_dataframe, pd.DataFrame({
            "id": [6],
            "name": ["Frank"],
            "age": [40],
            "score": [90.0],
        })], ignore_index=True)
        engine.write_table("users", df2)

        diff = engine.diff_versions("users", 1, 2)

        assert diff["rows_a"] == 5
        assert diff["rows_b"] == 6
        assert diff["row_diff"] == 1

    def test_diff_versions_with_keys(self, temp_storage, sample_dataframe):
        """Test version diff with key columns."""
        store, catalog, _ = temp_storage
        engine = QueryEngine(store, catalog)

        # Write version 1
        engine.write_table("users", sample_dataframe)

        # Write version 2: remove one, add one
        df2 = sample_dataframe[sample_dataframe["id"] != 3].copy()  # Remove Charlie
        df2 = pd.concat([df2, pd.DataFrame({
            "id": [6],
            "name": ["Frank"],
            "age": [40],
            "score": [90.0],
        })], ignore_index=True)
        engine.write_table("users", df2)

        diff = engine.diff_versions("users", 1, 2, key_columns=["id"])

        assert diff["rows_added"] == 1  # Frank
        assert diff["rows_removed"] == 1  # Charlie

    def test_list_operations(self, temp_storage, sample_dataframe):
        """Test list_tables and list_versions."""
        store, catalog, _ = temp_storage
        engine = QueryEngine(store, catalog)

        engine.write_table("users", sample_dataframe)
        engine.write_table("users", sample_dataframe)
        engine.write_table("orders", sample_dataframe)

        assert "users" in engine.list_tables()
        assert "orders" in engine.list_tables()
        assert engine.list_versions("users") == [1, 2]
        assert engine.list_versions("orders") == [1]

    def test_context_manager(self, temp_storage, sample_dataframe):
        """Test QueryEngine as context manager."""
        store, catalog, _ = temp_storage

        with QueryEngine(store, catalog) as engine:
            engine.write_table("users", sample_dataframe)
            result = engine.query("SELECT * FROM users")
            assert result.row_count == 5
