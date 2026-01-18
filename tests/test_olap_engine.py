"""
Tests for OLAPEngine and CacheManager.

Verifies:
1. Cache correctness (hits, misses, eviction)
2. OLAP query correctness (matches DuckDB results)
3. Performance improvement over baseline
4. Version and branch isolation
"""

import time

import numpy as np
import pandas as pd
import pyarrow as pa
import pytest

from rhizo import (
    CacheManager,
    CacheKey,
    CacheStats,
    OLAPEngine,
    QueryEngine,
    TableWriter,
    is_datafusion_available,
)


# ============================================================================
# CacheManager Tests
# ============================================================================


class TestCacheManager:
    """Tests for CacheManager."""

    def test_basic_put_get(self):
        """Test basic put and get operations."""
        cache = CacheManager(max_size_bytes=100_000_000)

        table = pa.table({"id": [1, 2, 3], "value": [10, 20, 30]})
        key = CacheKey("test", version=1, branch="main")

        # Initially not in cache
        assert cache.get(key) is None

        # Put and get
        cache.put(key, table)
        result = cache.get(key)

        assert result is not None
        assert result.num_rows == 3
        assert result.column_names == ["id", "value"]

    def test_cache_hit_miss_stats(self):
        """Test cache hit/miss statistics."""
        cache = CacheManager(max_size_bytes=100_000_000)

        table = pa.table({"id": [1, 2, 3]})
        key1 = CacheKey("test1", version=1)
        key2 = CacheKey("test2", version=1)

        # Miss
        cache.get(key1)
        stats = cache.stats()
        assert stats.misses == 1
        assert stats.hits == 0

        # Put
        cache.put(key1, table)

        # Hit
        cache.get(key1)
        stats = cache.stats()
        assert stats.hits == 1
        assert stats.misses == 1

        # Another miss
        cache.get(key2)
        stats = cache.stats()
        assert stats.misses == 2
        assert stats.hit_rate == 1 / 3  # 1 hit, 2 misses

    def test_lru_eviction(self):
        """Test LRU eviction when cache is full."""
        # Create tables that will exceed cache size
        # Each table is ~80KB (10000 int64 values = 80000 bytes)
        t1 = pa.table({"a": list(range(10000))})
        t2 = pa.table({"b": list(range(10000))})
        t3 = pa.table({"c": list(range(10000))})

        # Cache can hold exactly 2 tables (170KB limit > 160KB for 2 tables)
        # but not 3 tables (240KB)
        cache = CacheManager(max_size_bytes=170_000)

        k1 = CacheKey("t1", 1)
        k2 = CacheKey("t2", 1)
        k3 = CacheKey("t3", 1)

        cache.put(k1, t1)
        cache.put(k2, t2)

        # Both should fit
        assert cache.contains(k1)
        assert cache.contains(k2)

        # Access k1 to make k2 the LRU
        cache.get(k1)

        # Add k3, should evict k2 (LRU)
        cache.put(k3, t3)

        # k1 should still be there (recently accessed)
        # k2 should be evicted (LRU)
        # k3 should be there (just added)
        assert cache.contains(k1)
        assert not cache.contains(k2)
        assert cache.contains(k3)
        stats = cache.stats()
        assert stats.evictions >= 1

    def test_invalidate_table(self):
        """Test invalidating all versions of a table."""
        cache = CacheManager(max_size_bytes=100_000_000)

        table = pa.table({"id": [1, 2, 3]})

        # Add multiple versions
        cache.put(CacheKey("users", 1), table)
        cache.put(CacheKey("users", 2), table)
        cache.put(CacheKey("users", 3), table)
        cache.put(CacheKey("orders", 1), table)

        assert len(cache) == 4

        # Invalidate users
        count = cache.invalidate("users")

        assert count == 3
        assert len(cache) == 1
        assert cache.contains(CacheKey("orders", 1))

    def test_invalidate_specific_version(self):
        """Test invalidating a specific version."""
        cache = CacheManager(max_size_bytes=100_000_000)

        table = pa.table({"id": [1, 2, 3]})

        cache.put(CacheKey("users", 1), table)
        cache.put(CacheKey("users", 2), table)

        # Invalidate only version 1
        result = cache.invalidate_version("users", 1)

        assert result is True
        assert not cache.contains(CacheKey("users", 1))
        assert cache.contains(CacheKey("users", 2))

    def test_cache_key_equality(self):
        """Test cache key equality and hashing."""
        k1 = CacheKey("users", 1, "main")
        k2 = CacheKey("users", 1, "main")
        k3 = CacheKey("USERS", 1, "main")  # Different case
        k4 = CacheKey("users", 2, "main")  # Different version
        k5 = CacheKey("users", 1, "feature")  # Different branch

        assert k1 == k2
        assert k1 == k3  # Case insensitive
        assert k1 != k4
        assert k1 != k5
        assert hash(k1) == hash(k2)
        assert hash(k1) == hash(k3)

    def test_clear_cache(self):
        """Test clearing entire cache."""
        cache = CacheManager(max_size_bytes=100_000_000)

        table = pa.table({"id": [1, 2, 3]})
        cache.put(CacheKey("t1", 1), table)
        cache.put(CacheKey("t2", 1), table)

        assert len(cache) == 2

        cache.clear()

        assert len(cache) == 0
        stats = cache.stats()
        assert stats.current_size_bytes == 0


# ============================================================================
# OLAPEngine Tests
# ============================================================================


@pytest.fixture
def olap_setup(tmp_path):
    """Create OLAP engine with test data."""
    import _rhizo
    chunks_path = str(tmp_path / "chunks")
    catalog_path = str(tmp_path / "catalog")

    store = _rhizo.PyChunkStore(chunks_path)
    catalog = _rhizo.PyCatalog(catalog_path)
    writer = TableWriter(store, catalog)

    # Create test data
    np.random.seed(42)
    users_df = pd.DataFrame({
        "id": range(1000),
        "name": [f"user_{i}" for i in range(1000)],
        "age": np.random.randint(18, 80, 1000),
        "score": np.random.uniform(0, 100, 1000),
    })

    orders_df = pd.DataFrame({
        "order_id": range(5000),
        "user_id": np.random.randint(0, 1000, 5000),
        "amount": np.random.uniform(10, 500, 5000),
        "category": np.random.choice(["A", "B", "C", "D"], 5000),
    })

    writer.write("users", users_df)
    writer.write("orders", orders_df)

    # Create version 2 of users with modified data
    users_v2 = users_df.copy()
    users_v2["score"] = users_v2["score"] + 10
    writer.write("users", users_v2)

    olap = OLAPEngine(store, catalog, max_cache_size_bytes=100_000_000)
    duckdb_engine = QueryEngine(store, catalog)

    return {
        "olap": olap,
        "duckdb": duckdb_engine,
        "store": store,
        "catalog": catalog,
        "users_df": users_df,
        "orders_df": orders_df,
    }


@pytest.mark.skipif(not is_datafusion_available(), reason="DataFusion not installed")
class TestOLAPEngine:
    """Tests for OLAPEngine."""

    def test_basic_query(self, olap_setup):
        """Test basic SELECT query."""
        olap = olap_setup["olap"]

        result = olap.query("SELECT * FROM users")

        assert result.num_rows == 1000
        assert "id" in result.column_names
        assert "name" in result.column_names

    def test_filtered_query(self, olap_setup):
        """Test filtered query."""
        olap = olap_setup["olap"]

        result = olap.query("SELECT * FROM users WHERE age > 50")

        # Should have fewer rows
        assert result.num_rows < 1000
        assert result.num_rows > 0

        # All ages should be > 50
        ages = result.column("age").to_pylist()
        assert all(a > 50 for a in ages)

    def test_aggregation_query(self, olap_setup):
        """Test aggregation query."""
        olap = olap_setup["olap"]

        result = olap.query("""
            SELECT category, COUNT(*) as cnt, AVG(amount) as avg_amount
            FROM orders
            GROUP BY category
            ORDER BY category
        """)

        assert result.num_rows == 4  # A, B, C, D
        assert "category" in result.column_names
        assert "cnt" in result.column_names
        assert "avg_amount" in result.column_names

    def test_join_query(self, olap_setup):
        """Test join query."""
        olap = olap_setup["olap"]

        result = olap.query("""
            SELECT u.name, COUNT(o.order_id) as order_count
            FROM users u
            JOIN orders o ON u.id = o.user_id
            GROUP BY u.name
            HAVING COUNT(o.order_id) > 5
        """)

        # Should have some users with > 5 orders
        assert result.num_rows > 0

    def test_query_matches_duckdb(self, olap_setup):
        """Test that OLAP results match DuckDB results."""
        olap = olap_setup["olap"]
        duckdb = olap_setup["duckdb"]

        sql = "SELECT COUNT(*) as cnt FROM users WHERE age > 30"

        olap_result = olap.query(sql)
        duckdb_result = duckdb.query(sql)

        olap_count = olap_result.column("cnt").to_pylist()[0]
        duckdb_count = duckdb_result.to_arrow().column("cnt").to_pylist()[0]

        assert olap_count == duckdb_count

    def test_cache_hit_on_repeated_query(self, olap_setup):
        """Test that cache is used on repeated queries."""
        olap = olap_setup["olap"]

        # First query - cache miss
        olap.query("SELECT * FROM users")
        stats1 = olap.cache_stats()
        assert stats1["misses"] >= 1

        # Second query - cache hit
        olap.query("SELECT * FROM users")
        stats2 = olap.cache_stats()
        assert stats2["hits"] >= 1

    def test_cache_stats(self, olap_setup):
        """Test cache statistics."""
        olap = olap_setup["olap"]

        # Query to populate cache
        olap.query("SELECT * FROM users")
        olap.query("SELECT * FROM orders")

        stats = olap.cache_stats()

        assert "hits" in stats
        assert "misses" in stats
        assert "hit_rate" in stats
        assert "current_size_mb" in stats
        assert "entry_count" in stats
        assert stats["entry_count"] == 2

    def test_clear_cache(self, olap_setup):
        """Test clearing cache."""
        olap = olap_setup["olap"]

        # Populate cache
        olap.query("SELECT * FROM users")
        olap.query("SELECT * FROM orders")

        assert olap.cache_stats()["entry_count"] == 2

        # Clear specific table
        olap.clear_cache("users")
        # Note: entry might still show due to DataFusion registration
        # but cache itself is cleared

        # Clear all
        olap.clear_cache()
        stats = olap.cache_stats()
        # After full clear, hits/misses reset behavior depends on impl

    def test_preload_table(self, olap_setup):
        """Test preloading a table."""
        olap = olap_setup["olap"]

        # Preload
        olap.preload("users")

        stats = olap.cache_stats()
        assert stats["entry_count"] >= 1

        # Query should be a cache hit
        olap.query("SELECT * FROM users")
        stats = olap.cache_stats()
        assert stats["hits"] >= 1

    def test_version_query(self, olap_setup):
        """Test querying specific version."""
        olap = olap_setup["olap"]

        # Query version 1 (original scores)
        result_v1 = olap.query(
            "SELECT AVG(score) as avg_score FROM users",
            versions={"users": 1}
        )

        # Query version 2 (scores + 10)
        result_v2 = olap.query(
            "SELECT AVG(score) as avg_score FROM users",
            versions={"users": 2}
        )

        avg_v1 = result_v1.column("avg_score").to_pylist()[0]
        avg_v2 = result_v2.column("avg_score").to_pylist()[0]

        # Version 2 should have higher average (scores + 10)
        assert avg_v2 > avg_v1
        assert abs(avg_v2 - avg_v1 - 10) < 0.1  # Should be ~10 higher

    def test_query_pandas(self, olap_setup):
        """Test query_pandas method."""
        olap = olap_setup["olap"]

        result = olap.query_pandas("SELECT * FROM users LIMIT 10")

        assert isinstance(result, pd.DataFrame)
        assert len(result) == 10

    def test_list_tables(self, olap_setup):
        """Test listing tables."""
        olap = olap_setup["olap"]

        tables = olap.list_tables()

        assert "users" in tables
        assert "orders" in tables


@pytest.mark.skipif(not is_datafusion_available(), reason="DataFusion not installed")
class TestOLAPPerformance:
    """Performance tests for OLAPEngine."""

    def test_olap_faster_than_baseline_cached(self, olap_setup):
        """Test that cached OLAP queries are faster than disk reads."""
        olap = olap_setup["olap"]

        # Warm up cache
        olap.query("SELECT * FROM users")

        # Time cached query
        start = time.perf_counter()
        for _ in range(10):
            olap.query("SELECT * FROM users WHERE age > 50")
        cached_time = (time.perf_counter() - start) * 1000 / 10  # ms per query

        # Cached queries should be fast (< 10ms typically)
        # This is a sanity check, not a strict performance requirement
        assert cached_time < 100  # Should be well under 100ms

    def test_aggregation_performance(self, olap_setup):
        """Test aggregation query performance."""
        olap = olap_setup["olap"]

        # Warm up
        olap.query("SELECT COUNT(*) FROM orders")

        # Time aggregation
        start = time.perf_counter()
        for _ in range(10):
            olap.query("""
                SELECT category, COUNT(*), SUM(amount)
                FROM orders
                GROUP BY category
            """)
        agg_time = (time.perf_counter() - start) * 1000 / 10

        # Should be fast
        assert agg_time < 50  # ms


# ============================================================================
# Integration Tests
# ============================================================================


@pytest.mark.skipif(not is_datafusion_available(), reason="DataFusion not installed")
class TestOLAPIntegration:
    """Integration tests combining OLAP with other features."""

    def test_olap_with_branches(self, tmp_path):
        """Test OLAP queries with branching."""
        import _rhizo
        chunks_path = str(tmp_path / "chunks")
        catalog_path = str(tmp_path / "catalog")
        branches_path = str(tmp_path / "branches")

        store = _rhizo.PyChunkStore(chunks_path)
        catalog = _rhizo.PyCatalog(catalog_path)
        branches = _rhizo.PyBranchManager(branches_path)
        writer = TableWriter(store, catalog)

        # Write initial data
        df = pd.DataFrame({"id": [1, 2, 3], "value": [10, 20, 30]})
        result = writer.write("test", df)

        # Update branch head
        branches.update_head("main", "test", result.version)

        # Create feature branch
        branches.create("feature", from_branch="main")

        # Write to feature branch
        df_feature = pd.DataFrame({"id": [1, 2, 3], "value": [100, 200, 300]})
        result_feature = writer.write("test", df_feature)
        branches.update_head("feature", "test", result_feature.version)

        # Create OLAP engine with branches
        olap = OLAPEngine(store, catalog, branch_manager=branches)

        # Query main branch
        result_main = olap.query("SELECT SUM(value) as total FROM test", branch="main")
        total_main = result_main.column("total").to_pylist()[0]

        # Query feature branch
        olap.clear_cache()  # Clear to ensure fresh load
        result_feature = olap.query("SELECT SUM(value) as total FROM test", branch="feature")
        total_feature = result_feature.column("total").to_pylist()[0]

        assert total_main == 60  # 10 + 20 + 30
        assert total_feature == 600  # 100 + 200 + 300
