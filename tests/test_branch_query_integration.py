"""
Tests for QueryEngine branch integration.

Run with: pytest tests/test_branch_query_integration.py -v
"""

import os
import tempfile
import shutil

import pytest
import pandas as pd

import _rhizo
from rhizo import QueryEngine


@pytest.fixture
def temp_storage():
    """Create temporary storage directories for testing."""
    base_dir = tempfile.mkdtemp(prefix="rhizo_branch_query_test_")
    chunks_dir = os.path.join(base_dir, "chunks")
    catalog_dir = os.path.join(base_dir, "catalog")
    branches_dir = os.path.join(base_dir, "branches")

    store = _rhizo.PyChunkStore(chunks_dir)
    catalog = _rhizo.PyCatalog(catalog_dir)
    branches = _rhizo.PyBranchManager(branches_dir)

    yield store, catalog, branches, base_dir

    # Cleanup
    shutil.rmtree(base_dir, ignore_errors=True)


class TestQueryEngineBranchIntegration:
    """Tests for QueryEngine with branch support."""

    def test_engine_with_branch_manager(self, temp_storage):
        """Test creating engine with branch manager."""
        store, catalog, branches, _ = temp_storage

        engine = QueryEngine(store, catalog, branch_manager=branches)

        assert engine.branch_manager is not None
        assert engine.current_branch == "main"

    def test_engine_without_branch_manager(self, temp_storage):
        """Test engine works without branch manager (backward compatible)."""
        store, catalog, branches, _ = temp_storage

        engine = QueryEngine(store, catalog)

        assert engine.branch_manager is None
        assert engine.current_branch == "main"

    def test_write_updates_branch_head(self, temp_storage):
        """Test that write_table updates branch head."""
        store, catalog, branches, _ = temp_storage

        engine = QueryEngine(store, catalog, branch_manager=branches)

        df = pd.DataFrame({
            "id": [1, 2, 3],
            "name": ["Alice", "Bob", "Charlie"],
        })

        result = engine.write_table("users", df)

        # Branch head should be updated
        version = branches.get_table_version("main", "users")
        assert version == result.version

    def test_checkout_branch(self, temp_storage):
        """Test switching branches."""
        store, catalog, branches, _ = temp_storage

        engine = QueryEngine(store, catalog, branch_manager=branches)

        # Create a feature branch
        branches.create("feature/test")

        # Checkout the branch
        engine.checkout("feature/test")
        assert engine.current_branch == "feature/test"

        # Checkout back to main
        engine.checkout("main")
        assert engine.current_branch == "main"

    def test_checkout_nonexistent_branch(self, temp_storage):
        """Test checkout fails for nonexistent branch."""
        store, catalog, branches, _ = temp_storage

        engine = QueryEngine(store, catalog, branch_manager=branches)

        with pytest.raises(IOError, match="Branch not found"):
            engine.checkout("nonexistent")

    def test_checkout_without_branch_manager(self, temp_storage):
        """Test checkout fails without branch manager."""
        store, catalog, branches, _ = temp_storage

        engine = QueryEngine(store, catalog)  # No branch manager

        with pytest.raises(RuntimeError, match="branch_manager not configured"):
            engine.checkout("main")

    def test_create_branch_via_engine(self, temp_storage):
        """Test creating branch through engine."""
        store, catalog, branches, _ = temp_storage

        engine = QueryEngine(store, catalog, branch_manager=branches)

        branch = engine.create_branch("feature/new", description="New feature")

        assert branch.name == "feature/new"
        assert "feature/new" in engine.list_branches()

    def test_query_on_specific_branch(self, temp_storage):
        """Test querying a specific branch without checkout."""
        store, catalog, branches, _ = temp_storage

        engine = QueryEngine(store, catalog, branch_manager=branches)

        # Write to main
        df_main = pd.DataFrame({"id": [1], "value": [100]})
        engine.write_table("data", df_main)

        # Create feature branch and write different data
        engine.create_branch("feature/test")
        engine.checkout("feature/test")

        df_feature = pd.DataFrame({"id": [1], "value": [200]})
        engine.write_table("data", df_feature)

        # Query main branch (without checkout)
        result_main = engine.query("SELECT value FROM data", branch="main")
        assert result_main.to_pandas()["value"].iloc[0] == 100

        # Query feature branch
        result_feature = engine.query("SELECT value FROM data", branch="feature/test")
        assert result_feature.to_pandas()["value"].iloc[0] == 200

    def test_branch_isolation_via_engine(self, temp_storage):
        """Test that changes on one branch don't affect another."""
        store, catalog, branches, _ = temp_storage

        engine = QueryEngine(store, catalog, branch_manager=branches)

        # Write initial data to main
        df_v1 = pd.DataFrame({"id": [1, 2], "score": [80, 90]})
        engine.write_table("scores", df_v1)

        # Create feature branch
        engine.create_branch("feature/scoring")
        engine.checkout("feature/scoring")

        # Modify on feature branch
        df_v2 = pd.DataFrame({"id": [1, 2], "score": [85, 95]})
        engine.write_table("scores", df_v2)

        # Main should still have original values
        result_main = engine.query("SELECT AVG(score) as avg FROM scores", branch="main")
        assert result_main.to_pandas()["avg"].iloc[0] == 85.0  # (80+90)/2

        # Feature should have new values
        result_feature = engine.query("SELECT AVG(score) as avg FROM scores", branch="feature/scoring")
        assert result_feature.to_pandas()["avg"].iloc[0] == 90.0  # (85+95)/2

    def test_diff_branches_via_engine(self, temp_storage):
        """Test comparing branches through engine."""
        store, catalog, branches, _ = temp_storage

        engine = QueryEngine(store, catalog, branch_manager=branches)

        # Write to main
        df = pd.DataFrame({"id": [1]})
        engine.write_table("users", df)

        # Create feature branch and modify
        engine.create_branch("feature/test")
        engine.checkout("feature/test")

        df2 = pd.DataFrame({"id": [1, 2]})
        engine.write_table("users", df2)

        # Compare branches
        diff = engine.diff_branches("feature/test", "main")

        assert diff["has_conflicts"] is True
        assert len(diff["modified"]) == 1
        assert diff["modified"][0][0] == "users"

    def test_merge_branch_via_engine(self, temp_storage):
        """Test merging branches through engine."""
        store, catalog, branches, _ = temp_storage

        engine = QueryEngine(store, catalog, branch_manager=branches)

        # Write to main
        df = pd.DataFrame({"id": [1]})
        engine.write_table("users", df)

        # Create feature branch and add new table
        engine.create_branch("feature/orders")
        engine.checkout("feature/orders")

        orders_df = pd.DataFrame({"order_id": [100, 101]})
        engine.write_table("orders", orders_df)

        # Merge back to main
        engine.checkout("main")
        engine.merge_branch("feature/orders", into="main")

        # Main should now have orders
        version = branches.get_table_version("main", "orders")
        assert version is not None

    def test_version_overrides_branch(self, temp_storage):
        """Test that explicit version parameter takes priority over branch."""
        store, catalog, branches, _ = temp_storage

        engine = QueryEngine(store, catalog, branch_manager=branches)

        # Write v1
        df_v1 = pd.DataFrame({"id": [1], "value": [100]})
        engine.write_table("data", df_v1)

        # Write v2
        df_v2 = pd.DataFrame({"id": [1], "value": [200]})
        engine.write_table("data", df_v2)

        # Create branch pointing to v2
        engine.create_branch("feature/test")

        # Query with explicit version=1 should override branch
        result = engine.query(
            "SELECT value FROM data",
            versions={"data": 1},
            branch="feature/test"
        )
        assert result.to_pandas()["value"].iloc[0] == 100


class TestQueryEngineBranchlessBackwardCompat:
    """Tests to verify backward compatibility without branches."""

    def test_query_without_branch_manager(self, temp_storage):
        """Test queries work without branch manager."""
        store, catalog, branches, _ = temp_storage

        engine = QueryEngine(store, catalog)  # No branch manager

        df = pd.DataFrame({"id": [1, 2, 3]})
        engine.write_table("test", df)

        result = engine.query("SELECT COUNT(*) as cnt FROM test")
        assert result.to_pandas()["cnt"].iloc[0] == 3

    def test_write_without_branch_manager(self, temp_storage):
        """Test writes work without branch manager."""
        store, catalog, branches, _ = temp_storage

        engine = QueryEngine(store, catalog)  # No branch manager

        df = pd.DataFrame({"id": [1]})
        result = engine.write_table("test", df)

        assert result.version == 1
        assert result.table_name == "test"

    def test_time_travel_without_branch_manager(self, temp_storage):
        """Test time travel still works without branch manager."""
        store, catalog, branches, _ = temp_storage

        engine = QueryEngine(store, catalog)  # No branch manager

        # Write v1
        df_v1 = pd.DataFrame({"id": [1], "value": [100]})
        engine.write_table("data", df_v1)

        # Write v2
        df_v2 = pd.DataFrame({"id": [1], "value": [200]})
        engine.write_table("data", df_v2)

        # Time travel to v1
        result = engine.query("SELECT value FROM data", versions={"data": 1})
        assert result.to_pandas()["value"].iloc[0] == 100

        # Query latest (v2)
        result = engine.query("SELECT value FROM data")
        assert result.to_pandas()["value"].iloc[0] == 200
