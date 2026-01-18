"""
Tests for Phase DF.4: Changelog SQL Queries.

Tests querying the changelog via SQL:
- SELECT * FROM __changelog
- Filtering by table_name, tx_id, etc.
- Aggregations over changelog data
"""

import pandas as pd
import pyarrow as pa
import pytest

from rhizo import QueryEngine, is_datafusion_available


@pytest.fixture
def engine_with_changelog(tmp_path):
    """Create QueryEngine with transaction manager for changelog queries."""
    import _rhizo
    chunks_path = str(tmp_path / "chunks")
    catalog_path = str(tmp_path / "catalog")
    branches_path = str(tmp_path / "branches")
    tx_log_path = str(tmp_path / "tx_log")

    store = _rhizo.PyChunkStore(chunks_path)
    catalog = _rhizo.PyCatalog(catalog_path)
    branch_manager = _rhizo.PyBranchManager(branches_path)
    # PyTransactionManager takes paths as strings
    tx_manager = _rhizo.PyTransactionManager(tx_log_path, catalog_path, branches_path)

    engine = QueryEngine(
        store, catalog,
        branch_manager=branch_manager,
        transaction_manager=tx_manager,
        enable_olap=True,
        olap_cache_size=100_000_000,
    )

    # Create some transactions to populate changelog
    # Must use engine.transaction() context to record in changelog
    # Using different table names to avoid write conflicts

    df = pd.DataFrame({"id": [1, 2, 3], "value": [10, 20, 30]})

    # Transaction 1: Create users table
    with engine.transaction() as tx:
        tx.write_table("users", df)

    # Transaction 2: Create orders table
    with engine.transaction() as tx:
        tx.write_table("orders", df)

    # Transaction 3: Create products table
    with engine.transaction() as tx:
        tx.write_table("products", df)

    # Transaction 4: Create inventory table
    with engine.transaction() as tx:
        tx.write_table("inventory", df)

    return engine


class TestChangelogQueries:
    """Tests for changelog SQL queries."""

    @pytest.mark.skipif(not is_datafusion_available(), reason="DataFusion required")
    def test_select_all_changelog(self, engine_with_changelog):
        """Test basic SELECT * FROM __changelog."""
        result = engine_with_changelog.query_changelog(
            "SELECT * FROM __changelog"
        )
        # We created 4 transactions, one per table
        assert result.row_count == 4
        assert "tx_id" in result.column_names
        assert "table_name" in result.column_names
        assert "new_version" in result.column_names

    @pytest.mark.skipif(not is_datafusion_available(), reason="DataFusion required")
    def test_filter_by_table_name(self, engine_with_changelog):
        """Test filtering changelog by table_name."""
        result = engine_with_changelog.query_changelog(
            "SELECT * FROM __changelog WHERE table_name = 'users'"
        )
        # 1 change to users (create)
        assert result.row_count == 1

        # Verify all rows are for users table
        df = result.to_pandas()
        assert all(df["table_name"] == "users")

    @pytest.mark.skipif(not is_datafusion_available(), reason="DataFusion required")
    def test_filter_by_orders_table(self, engine_with_changelog):
        """Test filtering changelog for orders table."""
        result = engine_with_changelog.query_changelog(
            "SELECT * FROM __changelog WHERE table_name = 'orders'"
        )
        # 1 change to orders (create only)
        assert result.row_count == 1

    @pytest.mark.skipif(not is_datafusion_available(), reason="DataFusion required")
    def test_count_changes_per_table(self, engine_with_changelog):
        """Test aggregating changes per table."""
        result = engine_with_changelog.query_changelog("""
            SELECT table_name, COUNT(*) as change_count
            FROM __changelog
            GROUP BY table_name
            ORDER BY table_name
        """)

        df = result.to_pandas()
        assert len(df) == 4  # users, orders, products, inventory

        # All should have 1 change each
        assert all(df["change_count"] == 1)

    @pytest.mark.skipif(not is_datafusion_available(), reason="DataFusion required")
    def test_filter_new_tables(self, engine_with_changelog):
        """Test finding only new table creations."""
        result = engine_with_changelog.query_changelog("""
            SELECT table_name, new_version
            FROM __changelog
            WHERE is_new_table = true
        """)

        # All 4 tables were created (version 1)
        assert result.row_count == 4

        df = result.to_pandas()
        tables = set(df["table_name"])
        assert tables == {"users", "orders", "products", "inventory"}

        # All should have version 1
        assert all(df["new_version"] == 1)

    @pytest.mark.skipif(not is_datafusion_available(), reason="DataFusion required")
    def test_order_by_tx_id(self, engine_with_changelog):
        """Test ordering changelog by transaction ID."""
        result = engine_with_changelog.query_changelog("""
            SELECT tx_id, table_name, new_version
            FROM __changelog
            ORDER BY tx_id DESC
        """)

        df = result.to_pandas()
        tx_ids = df["tx_id"].tolist()

        # Verify descending order
        assert tx_ids == sorted(tx_ids, reverse=True)

    @pytest.mark.skipif(not is_datafusion_available(), reason="DataFusion required")
    def test_filter_by_tx_id(self, engine_with_changelog):
        """Test filtering by transaction ID."""
        # First get latest tx_id
        result = engine_with_changelog.query_changelog(
            "SELECT MAX(tx_id) as max_tx FROM __changelog"
        )
        max_tx = result.to_arrow().column("max_tx").to_pylist()[0]

        # Now filter for only this tx
        result2 = engine_with_changelog.query_changelog(
            f"SELECT * FROM __changelog WHERE tx_id = {max_tx}"
        )
        assert result2.row_count == 1

    @pytest.mark.skipif(not is_datafusion_available(), reason="DataFusion required")
    def test_changelog_with_limit_param(self, engine_with_changelog):
        """Test using limit parameter to restrict changelog entries."""
        result = engine_with_changelog.query_changelog(
            "SELECT * FROM __changelog",
            limit=2,
        )
        # Limited to 2 entries at the source
        assert result.row_count == 2


class TestChangelogSchema:
    """Tests for changelog schema and column types."""

    @pytest.mark.skipif(not is_datafusion_available(), reason="DataFusion required")
    def test_changelog_columns_exist(self, engine_with_changelog):
        """Test all expected columns exist."""
        result = engine_with_changelog.query_changelog(
            "SELECT * FROM __changelog LIMIT 1"
        )

        expected_columns = {
            "tx_id", "epoch_id", "committed_at", "branch",
            "table_name", "old_version", "new_version", "is_new_table"
        }
        assert set(result.column_names) == expected_columns

    @pytest.mark.skipif(not is_datafusion_available(), reason="DataFusion required")
    def test_branch_column(self, engine_with_changelog):
        """Test branch column has correct values."""
        result = engine_with_changelog.query_changelog(
            "SELECT DISTINCT branch FROM __changelog"
        )

        df = result.to_pandas()
        assert "main" in df["branch"].tolist()


class TestChangelogErrorHandling:
    """Tests for changelog error handling."""

    def test_changelog_requires_olap(self, tmp_path):
        """Test changelog queries require OLAP engine."""
        import _rhizo
        chunks_path = str(tmp_path / "chunks")
        catalog_path = str(tmp_path / "catalog")

        store = _rhizo.PyChunkStore(chunks_path)
        catalog = _rhizo.PyCatalog(catalog_path)

        # No OLAP
        engine = QueryEngine(store, catalog, enable_olap=False)

        with pytest.raises(RuntimeError, match="Changelog queries require OLAP"):
            engine.query_changelog("SELECT * FROM __changelog")

    @pytest.mark.skipif(not is_datafusion_available(), reason="DataFusion required")
    def test_changelog_requires_tx_manager(self, tmp_path):
        """Test changelog queries require transaction manager."""
        import _rhizo
        chunks_path = str(tmp_path / "chunks")
        catalog_path = str(tmp_path / "catalog")

        store = _rhizo.PyChunkStore(chunks_path)
        catalog = _rhizo.PyCatalog(catalog_path)

        # OLAP but no tx_manager
        engine = QueryEngine(store, catalog, enable_olap=True)

        with pytest.raises(RuntimeError, match="Changelog queries require transaction_manager"):
            engine.query_changelog("SELECT * FROM __changelog")
