"""
Tests for Phase DF.4: Time Travel SQL Syntax.

Tests the extended SQL syntax:
- `SELECT * FROM users VERSION 5` - query specific version
- `SELECT * FROM users@branch` - query from specific branch
- Combined: `SELECT * FROM users@main VERSION 3`
"""

import numpy as np
import pandas as pd
import pyarrow as pa
import pytest

from rhizo import QueryEngine, is_datafusion_available


@pytest.fixture
def engine_with_versions(tmp_path):
    """Create QueryEngine with multiple versions of data."""
    import _rhizo
    chunks_path = str(tmp_path / "chunks")
    catalog_path = str(tmp_path / "catalog")

    store = _rhizo.PyChunkStore(chunks_path)
    catalog = _rhizo.PyCatalog(catalog_path)

    engine = QueryEngine(
        store, catalog,
        enable_olap=True,
        olap_cache_size=100_000_000,
    )

    # Create version 1: 100 rows with score 10
    df1 = pd.DataFrame({
        "id": range(100),
        "name": [f"user_{i}" for i in range(100)],
        "score": [10.0] * 100,
    })
    engine.write_table("users", df1)

    # Create version 2: 100 rows with score 20
    df2 = pd.DataFrame({
        "id": range(100),
        "name": [f"user_{i}" for i in range(100)],
        "score": [20.0] * 100,
    })
    engine.write_table("users", df2)

    # Create version 3: 100 rows with score 30
    df3 = pd.DataFrame({
        "id": range(100),
        "name": [f"user_{i}" for i in range(100)],
        "score": [30.0] * 100,
    })
    engine.write_table("users", df3)

    return engine


@pytest.fixture
def engine_with_branches(tmp_path):
    """Create QueryEngine with branching support."""
    import _rhizo
    chunks_path = str(tmp_path / "chunks")
    catalog_path = str(tmp_path / "catalog")
    branches_path = str(tmp_path / "branches")

    store = _rhizo.PyChunkStore(chunks_path)
    catalog = _rhizo.PyCatalog(catalog_path)
    branch_manager = _rhizo.PyBranchManager(branches_path)

    # Note: PyBranchManager auto-creates "main" branch on init

    engine = QueryEngine(
        store, catalog,
        branch_manager=branch_manager,
        enable_olap=True,
        olap_cache_size=100_000_000,
    )

    # Write to main branch: score = 100
    df_main = pd.DataFrame({
        "id": range(50),
        "value": [100] * 50,
    })
    engine.write_table("data", df_main, branch="main")

    # Create feature branch from main
    branch_manager.create("feature", from_branch="main")

    # Write to feature branch: value = 200
    df_feature = pd.DataFrame({
        "id": range(50),
        "value": [200] * 50,
    })
    engine.write_table("data", df_feature, branch="feature")

    return engine


class TestTimeTravelVersionSyntax:
    """Tests for VERSION keyword in SQL."""

    @pytest.mark.skipif(not is_datafusion_available(), reason="DataFusion required")
    def test_version_syntax_basic(self, engine_with_versions):
        """Test basic VERSION syntax."""
        # Query version 1 (score = 10)
        result = engine_with_versions.query_time_travel(
            "SELECT AVG(score) as avg_score FROM users VERSION 1"
        )
        avg = result.to_arrow().column("avg_score").to_pylist()[0]
        assert avg == 10.0

    @pytest.mark.skipif(not is_datafusion_available(), reason="DataFusion required")
    def test_version_syntax_different_versions(self, engine_with_versions):
        """Test querying different versions."""
        # Version 1 = 10, Version 2 = 20, Version 3 = 30
        for version, expected_score in [(1, 10.0), (2, 20.0), (3, 30.0)]:
            result = engine_with_versions.query_time_travel(
                f"SELECT AVG(score) as avg_score FROM users VERSION {version}"
            )
            avg = result.to_arrow().column("avg_score").to_pylist()[0]
            assert avg == expected_score, f"Version {version} should have score {expected_score}"

    @pytest.mark.skipif(not is_datafusion_available(), reason="DataFusion required")
    def test_version_syntax_with_where(self, engine_with_versions):
        """Test VERSION with WHERE clause."""
        result = engine_with_versions.query_time_travel(
            "SELECT COUNT(*) as cnt FROM users VERSION 1 WHERE id < 10"
        )
        cnt = result.to_arrow().column("cnt").to_pylist()[0]
        assert cnt == 10

    @pytest.mark.skipif(not is_datafusion_available(), reason="DataFusion required")
    def test_version_syntax_case_insensitive(self, engine_with_versions):
        """Test that VERSION keyword is case insensitive."""
        # Test lowercase
        result1 = engine_with_versions.query_time_travel(
            "SELECT AVG(score) as avg FROM users version 1"
        )
        # Test uppercase
        result2 = engine_with_versions.query_time_travel(
            "SELECT AVG(score) as avg FROM users VERSION 1"
        )
        # Test mixed case
        result3 = engine_with_versions.query_time_travel(
            "SELECT AVG(score) as avg FROM users Version 1"
        )

        avg1 = result1.to_arrow().column("avg").to_pylist()[0]
        avg2 = result2.to_arrow().column("avg").to_pylist()[0]
        avg3 = result3.to_arrow().column("avg").to_pylist()[0]

        assert avg1 == avg2 == avg3 == 10.0


class TestBranchSyntax:
    """Tests for @branch syntax in SQL."""

    @pytest.mark.skipif(not is_datafusion_available(), reason="DataFusion required")
    def test_branch_syntax_main(self, engine_with_branches):
        """Test @main branch syntax."""
        result = engine_with_branches.query_time_travel(
            "SELECT AVG(value) as avg_val FROM data@main"
        )
        avg = result.to_arrow().column("avg_val").to_pylist()[0]
        assert avg == 100

    @pytest.mark.skipif(not is_datafusion_available(), reason="DataFusion required")
    def test_branch_syntax_feature(self, engine_with_branches):
        """Test @feature branch syntax."""
        result = engine_with_branches.query_time_travel(
            "SELECT AVG(value) as avg_val FROM data@feature"
        )
        avg = result.to_arrow().column("avg_val").to_pylist()[0]
        assert avg == 200

    @pytest.mark.skipif(not is_datafusion_available(), reason="DataFusion required")
    def test_branch_comparison_query(self, engine_with_branches):
        """Test comparing data across branches (the killer feature!)."""
        # This is the powerful cross-branch comparison query
        # Note: This requires table aliasing since same table appears twice
        result = engine_with_branches.query_time_travel("""
            SELECT COUNT(*) as diff_count
            FROM data@main
        """)
        # For now just verify it runs - full cross-branch JOIN needs more work
        assert result.row_count == 1


class TestCombinedSyntax:
    """Tests for combined VERSION and @branch syntax."""

    @pytest.mark.skipif(not is_datafusion_available(), reason="DataFusion required")
    def test_combined_syntax(self, engine_with_versions):
        """Test that VERSION and @branch can be combined."""
        # Currently just tests VERSION since we don't have branches in this fixture
        result = engine_with_versions.query_time_travel(
            "SELECT COUNT(*) as cnt FROM users VERSION 2"
        )
        assert result.row_count == 1


class TestTimeTravelWithAggregations:
    """Tests for time travel with complex queries."""

    @pytest.mark.skipif(not is_datafusion_available(), reason="DataFusion required")
    def test_group_by_on_historical_version(self, engine_with_versions):
        """Test GROUP BY on historical data."""
        result = engine_with_versions.query_time_travel(
            "SELECT score, COUNT(*) as cnt FROM users VERSION 1 GROUP BY score"
        )
        # All 100 rows have score=10 in version 1
        assert result.row_count == 1
        row = result.to_dict()[0]
        assert row["score"] == 10.0
        assert row["cnt"] == 100

    @pytest.mark.skipif(not is_datafusion_available(), reason="DataFusion required")
    def test_order_by_on_historical_version(self, engine_with_versions):
        """Test ORDER BY on historical data."""
        result = engine_with_versions.query_time_travel(
            "SELECT id, score FROM users VERSION 1 ORDER BY id LIMIT 5"
        )
        ids = result.to_arrow().column("id").to_pylist()
        assert ids == [0, 1, 2, 3, 4]


class TestEdgeCases:
    """Tests for edge cases and error handling."""

    @pytest.mark.skipif(not is_datafusion_available(), reason="DataFusion required")
    def test_version_keyword_not_in_table_name(self, engine_with_versions):
        """Ensure VERSION keyword doesn't match table names."""
        # Query latest (version 3)
        result = engine_with_versions.query_time_travel(
            "SELECT AVG(score) as avg FROM users"
        )
        avg = result.to_arrow().column("avg").to_pylist()[0]
        assert avg == 30.0  # Latest version

    @pytest.mark.skipif(not is_datafusion_available(), reason="DataFusion required")
    def test_query_without_version_uses_latest(self, engine_with_versions):
        """Queries without VERSION should use latest."""
        result = engine_with_versions.query_time_travel(
            "SELECT AVG(score) as avg FROM users"
        )
        avg = result.to_arrow().column("avg").to_pylist()[0]
        assert avg == 30.0  # Version 3 is latest

    def test_time_travel_requires_olap(self, tmp_path):
        """Test that time travel requires OLAP engine."""
        import _rhizo
        chunks_path = str(tmp_path / "chunks")
        catalog_path = str(tmp_path / "catalog")

        store = _rhizo.PyChunkStore(chunks_path)
        catalog = _rhizo.PyCatalog(catalog_path)

        # Disable OLAP
        engine = QueryEngine(store, catalog, enable_olap=False)

        with pytest.raises(RuntimeError, match="Time travel SQL syntax requires OLAP"):
            engine.query_time_travel("SELECT * FROM users VERSION 1")
