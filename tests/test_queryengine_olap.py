"""
Tests for QueryEngine OLAP integration.

Verifies:
1. OLAP is automatically enabled when DataFusion is available
2. Queries use OLAP by default with fallback to DuckDB
3. olap_query forces OLAP path
4. Cache invalidation on writes
5. Backward compatibility (existing tests should pass)
"""

import numpy as np
import pandas as pd
import pyarrow as pa
import pytest

from rhizo import QueryEngine, is_datafusion_available


@pytest.fixture
def olap_engine(tmp_path):
    """Create QueryEngine with OLAP enabled."""
    import _rhizo
    chunks_path = str(tmp_path / "chunks")
    catalog_path = str(tmp_path / "catalog")

    store = _rhizo.PyChunkStore(chunks_path)
    catalog = _rhizo.PyCatalog(catalog_path)

    engine = QueryEngine(
        store, catalog,
        enable_olap=True,
        olap_cache_size=100_000_000,  # 100MB
    )

    # Create test data
    np.random.seed(42)
    df = pd.DataFrame({
        "id": range(1000),
        "name": [f"user_{i}" for i in range(1000)],
        "score": np.random.uniform(0, 100, 1000),
        "category": np.random.choice(["A", "B", "C"], 1000),
    })
    engine.write_table("users", df)

    return engine


@pytest.fixture
def no_olap_engine(tmp_path):
    """Create QueryEngine with OLAP disabled."""
    import _rhizo
    chunks_path = str(tmp_path / "chunks")
    catalog_path = str(tmp_path / "catalog")

    store = _rhizo.PyChunkStore(chunks_path)
    catalog = _rhizo.PyCatalog(catalog_path)

    engine = QueryEngine(
        store, catalog,
        enable_olap=False,
    )

    # Create test data
    df = pd.DataFrame({
        "id": range(100),
        "value": range(100),
    })
    engine.write_table("test", df)

    return engine


class TestOLAPEnabled:
    """Tests for OLAP-enabled QueryEngine."""

    @pytest.mark.skipif(not is_datafusion_available(), reason="DataFusion not installed")
    def test_olap_enabled_property(self, olap_engine):
        """Test that OLAP is enabled when DataFusion is available."""
        assert olap_engine.olap_enabled is True

    def test_olap_disabled_property(self, no_olap_engine):
        """Test that OLAP is disabled when explicitly disabled."""
        assert no_olap_engine.olap_enabled is False

    @pytest.mark.skipif(not is_datafusion_available(), reason="DataFusion not installed")
    def test_query_uses_olap_by_default(self, olap_engine):
        """Test that query() uses OLAP by default."""
        # Query should succeed using OLAP
        result = olap_engine.query("SELECT * FROM users LIMIT 10")
        assert result.row_count == 10

        # Check that OLAP cache was used
        stats = olap_engine.olap_stats()
        assert stats["enabled"] is True
        assert stats["misses"] >= 1  # First query is a miss

    @pytest.mark.skipif(not is_datafusion_available(), reason="DataFusion not installed")
    def test_query_with_use_olap_false(self, olap_engine):
        """Test that query() can force DuckDB path."""
        # Clear OLAP cache to check it's not used
        olap_engine.olap_clear_cache()
        initial_stats = olap_engine.olap_stats()
        initial_misses = initial_stats["misses"]

        # Query with OLAP disabled
        result = olap_engine.query("SELECT * FROM users LIMIT 10", use_olap=False)
        assert result.row_count == 10

        # OLAP cache should not have been touched
        final_stats = olap_engine.olap_stats()
        assert final_stats["misses"] == initial_misses

    @pytest.mark.skipif(not is_datafusion_available(), reason="DataFusion not installed")
    def test_olap_query_method(self, olap_engine):
        """Test olap_query() method."""
        result = olap_engine.olap_query("SELECT COUNT(*) as cnt FROM users")
        assert result.num_rows == 1
        assert result.column("cnt").to_pylist()[0] == 1000

    def test_olap_query_raises_when_disabled(self, no_olap_engine):
        """Test that olap_query() raises when OLAP is disabled."""
        with pytest.raises(RuntimeError, match="OLAP engine not available"):
            no_olap_engine.olap_query("SELECT * FROM test")

    @pytest.mark.skipif(not is_datafusion_available(), reason="DataFusion not installed")
    def test_olap_stats(self, olap_engine):
        """Test olap_stats() returns correct data."""
        # Run a query to populate stats
        olap_engine.query("SELECT * FROM users")
        olap_engine.query("SELECT * FROM users")  # Second query should hit cache

        stats = olap_engine.olap_stats()

        assert "enabled" in stats
        assert "hits" in stats
        assert "misses" in stats
        assert "hit_rate" in stats
        assert "current_size_mb" in stats
        assert stats["enabled"] is True
        assert stats["hits"] >= 1

    def test_olap_stats_when_disabled(self, no_olap_engine):
        """Test olap_stats() when OLAP is disabled."""
        stats = no_olap_engine.olap_stats()

        assert stats["enabled"] is False
        assert stats["hits"] == 0
        assert stats["misses"] == 0

    @pytest.mark.skipif(not is_datafusion_available(), reason="DataFusion not installed")
    def test_olap_clear_cache(self, olap_engine):
        """Test olap_clear_cache() method."""
        # Populate cache
        olap_engine.query("SELECT * FROM users")

        stats = olap_engine.olap_stats()
        assert stats["entry_count"] >= 1

        # Clear cache
        olap_engine.olap_clear_cache()

        # Cache should be empty (stats may retain history)
        # Re-query should be a miss
        olap_engine.query("SELECT * FROM users")

    @pytest.mark.skipif(not is_datafusion_available(), reason="DataFusion not installed")
    def test_olap_preload(self, olap_engine):
        """Test olap_preload() method."""
        # Clear cache
        olap_engine.olap_clear_cache()

        # Preload
        olap_engine.olap_preload("users")

        # Query should be a cache hit
        olap_engine.query("SELECT * FROM users")

        stats = olap_engine.olap_stats()
        assert stats["hits"] >= 1


class TestOLAPFallback:
    """Tests for OLAP fallback to DuckDB."""

    @pytest.mark.skipif(not is_datafusion_available(), reason="DataFusion not installed")
    def test_fallback_on_params(self, olap_engine):
        """Test that parameterized queries use DuckDB (OLAP doesn't support params)."""
        # This should work via DuckDB fallback since params aren't supported in OLAP
        result = olap_engine.query(
            "SELECT * FROM users WHERE id < ?",
            params=[10],
        )
        assert result.row_count == 10


class TestCacheInvalidation:
    """Tests for cache invalidation on writes."""

    @pytest.mark.skipif(not is_datafusion_available(), reason="DataFusion not installed")
    def test_write_invalidates_olap_cache(self, olap_engine):
        """Test that write_table invalidates OLAP cache."""
        # Populate cache
        olap_engine.query("SELECT * FROM users")
        stats1 = olap_engine.olap_stats()
        initial_entries = stats1["entry_count"]
        assert initial_entries >= 1

        # Write new version
        new_df = pd.DataFrame({
            "id": range(500),
            "name": [f"new_user_{i}" for i in range(500)],
            "score": [50.0] * 500,
            "category": ["X"] * 500,
        })
        olap_engine.write_table("users", new_df)

        # Query should reflect new data (not cached old data)
        result = olap_engine.query("SELECT COUNT(*) as cnt FROM users")
        count = result.to_arrow().column("cnt").to_pylist()[0]
        assert count == 500  # Should be new count, not old 1000


class TestBackwardCompatibility:
    """Tests for backward compatibility."""

    def test_engine_works_without_olap_params(self, tmp_path):
        """Test that QueryEngine works without OLAP params (old API)."""
        import _rhizo
        chunks_path = str(tmp_path / "chunks")
        catalog_path = str(tmp_path / "catalog")

        store = _rhizo.PyChunkStore(chunks_path)
        catalog = _rhizo.PyCatalog(catalog_path)

        # Old-style initialization (should still work)
        engine = QueryEngine(store, catalog)

        df = pd.DataFrame({"x": [1, 2, 3]})
        engine.write_table("test", df)

        result = engine.query("SELECT * FROM test")
        assert result.row_count == 3

    @pytest.mark.skipif(not is_datafusion_available(), reason="DataFusion not installed")
    def test_query_results_match(self, olap_engine):
        """Test that OLAP and DuckDB return same results."""
        sql = "SELECT category, COUNT(*) as cnt FROM users GROUP BY category ORDER BY category"

        # OLAP result
        olap_result = olap_engine.query(sql, use_olap=True)

        # DuckDB result
        duckdb_result = olap_engine.query(sql, use_olap=False)

        # Results should match
        assert olap_result.row_count == duckdb_result.row_count
        assert olap_result.column_names == duckdb_result.column_names

        # Compare data
        olap_df = olap_result.to_pandas()
        duckdb_df = duckdb_result.to_pandas()
        pd.testing.assert_frame_equal(olap_df, duckdb_df)
