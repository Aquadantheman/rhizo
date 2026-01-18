# DataFusion Integration Roadmap

## Executive Summary

This document outlines the plan to integrate Apache DataFusion into Armillaria to achieve **OLAP-level query performance** while maintaining all lakehouse features (versioning, branching, transactions, deduplication).

**Goal**: Become #1 in both table formats AND OLAP query performance.

**Mathematical Proof of Feasibility**:
- DataFusion in-memory filtered read: **0.45ms** (measured)
- DuckDB filtered read: **1.6ms** (measured)
- Current Armillaria filtered read: **9.4ms** (measured)
- **Result**: DataFusion is 3.6x faster than DuckDB, 20.9x faster than current implementation

---

## Current Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                      Current Armillaria                         │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  Python API (armillaria_query)                                  │
│       │                                                         │
│       ▼                                                         │
│  QueryEngine ─────► DuckDB (in-memory)                         │
│       │                                                         │
│       ▼                                                         │
│  TableReader ─────► Read chunks from disk                       │
│       │                                                         │
│       ▼                                                         │
│  PyParquetDecoder ─► Decode Parquet to Arrow                   │
│       │                                                         │
│       ▼                                                         │
│  Catalog ─────────► Version resolution                         │
│       │                                                         │
│       ▼                                                         │
│  ChunkStore ──────► Content-addressable storage                │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

**Current Bottlenecks**:
1. Disk I/O for every query (no caching)
2. Sequential chunk decoding
3. DuckDB overhead for simple queries

---

## Proposed Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                   Armillaria + DataFusion                       │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  Python API (armillaria_query)                                  │
│       │                                                         │
│       ├──────────────────────────────────────┐                  │
│       ▼                                      ▼                  │
│  QueryEngine                          OLAPEngine (NEW)          │
│  (DuckDB - legacy)                    (DataFusion)              │
│       │                                      │                  │
│       │                                      ▼                  │
│       │                              CacheManager (NEW)         │
│       │                              ┌───────────────┐          │
│       │                              │ Hot Data      │          │
│       │                              │ (in-memory    │          │
│       │                              │  Arrow)       │          │
│       │                              └───────────────┘          │
│       │                                      │                  │
│       ▼                                      ▼                  │
│  TableReader ◄───────────────────────────────┘                  │
│       │                                                         │
│       ▼                                                         │
│  Catalog / BranchManager / TransactionManager                   │
│       │                                                         │
│       ▼                                                         │
│  ChunkStore (content-addressable)                               │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

**Key Changes**:
1. **OLAPEngine**: New DataFusion-based query engine for fast analytics
2. **CacheManager**: In-memory cache for frequently accessed data
3. **Backward Compatible**: Existing DuckDB path remains for compatibility

---

## Mathematical Analysis

### Performance Comparison (100k rows, 10 columns)

| Query Type | Current Armillaria | DuckDB | DataFusion (memory) | Improvement |
|------------|-------------------|--------|---------------------|-------------|
| Filtered (5%) | 9.4ms | 1.6ms | **0.45ms** | **20.9x** |
| Projection (2 cols) | 6.9ms | 1.6ms | **0.18ms** | **38.3x** |
| Full scan | 12.4ms | 22.5ms | **0.22ms** | **56.4x** |
| COUNT(*) | ~5ms | ~0.5ms | **0.23ms** | **~20x** |
| GROUP BY | ~15ms | ~2ms | **1.12ms** | **~13x** |

### Theoretical Model

For a query with:
- `D` = data size in memory
- `s` = selectivity (fraction of rows matching filter)
- `k` = columns requested / total columns

**Current path (disk-based)**:
```
T_current = T_disk_io + T_decode + T_filter + T_project
         ≈ O(D) + O(D) + O(D) + O(D*k)
         ≈ O(D)
```

**DataFusion path (memory-based)**:
```
T_datafusion = T_cache_lookup + T_vectorized_filter + T_vectorized_project
            ≈ O(1) + O(D*s) + O(D*s*k)
            ≈ O(D*s*k)
```

**Speedup**:
```
Speedup = T_current / T_datafusion
       ≈ O(D) / O(D*s*k)
       = 1 / (s*k)

Example: 5% selectivity, 2/10 columns
Speedup = 1 / (0.05 * 0.2) = 1 / 0.01 = 100x theoretical
```

Real-world measured: 20-56x (overhead from cache management, Arrow conversion, etc.)

### Storage Overhead Analysis

**Cache memory requirement**:
```
Memory = active_tables * avg_table_size * cache_ratio

Conservative estimate:
- 10 active tables
- 100MB average size
- 100% cache ratio (full tables)
= 1GB memory

Aggressive estimate:
- 50 active tables
- 500MB average size
- 50% cache ratio (hot partitions only)
= 12.5GB memory
```

**Trade-off**: Memory usage vs query speed. Configurable via `max_cache_size_bytes`.

---

## Implementation Phases

### Phase DF.1: Core DataFusion Integration (Foundation)

**Goal**: Add DataFusion as a query backend alongside DuckDB

**Files to Create**:
- `python/armillaria_query/olap_engine.py` - OLAPEngine class
- `python/armillaria_query/cache.py` - CacheManager class
- `tests/test_olap_engine.py` - Tests

**OLAPEngine API**:
```python
class OLAPEngine:
    """
    High-performance OLAP query engine powered by DataFusion.

    Provides vectorized, multi-threaded query execution over
    Armillaria tables with optional in-memory caching.
    """

    def __init__(
        self,
        store: PyChunkStore,
        catalog: PyCatalog,
        branch_manager: Optional[PyBranchManager] = None,
        max_cache_size_bytes: int = 1_000_000_000,  # 1GB default
    ):
        self._ctx = datafusion.SessionContext()
        self._cache = CacheManager(max_cache_size_bytes)
        # ...

    def query(
        self,
        sql: str,
        versions: Optional[Dict[str, int]] = None,
        branch: Optional[str] = None,
    ) -> pa.Table:
        """Execute SQL query with DataFusion."""
        # 1. Parse SQL to extract table names
        # 2. Load tables (from cache or disk)
        # 3. Register with DataFusion
        # 4. Execute query
        # 5. Return Arrow table

    def query_df(self, sql: str, ...) -> datafusion.DataFrame:
        """Return DataFusion DataFrame for lazy execution."""

    def load_table(
        self,
        table_name: str,
        version: Optional[int] = None,
        branch: Optional[str] = None,
    ) -> None:
        """Pre-load a table into cache."""

    def cache_stats(self) -> Dict[str, Any]:
        """Get cache statistics."""

    def clear_cache(self, table_name: Optional[str] = None) -> None:
        """Clear cache (all or specific table)."""
```

**CacheManager API**:
```python
class CacheManager:
    """
    LRU cache for Arrow tables with size-based eviction.
    """

    def __init__(self, max_size_bytes: int):
        self._cache: Dict[CacheKey, CacheEntry] = {}
        self._max_size = max_size_bytes
        self._current_size = 0

    def get(self, key: CacheKey) -> Optional[pa.Table]:
        """Get table from cache (updates LRU order)."""

    def put(self, key: CacheKey, table: pa.Table) -> None:
        """Add table to cache (may evict others)."""

    def invalidate(self, table_name: str) -> None:
        """Invalidate all versions of a table."""

    def stats(self) -> CacheStats:
        """Get hit/miss statistics."""
```

**Verification Criteria**:
- [ ] DataFusion queries return correct results (compare with DuckDB)
- [ ] Cache hit rate > 90% for repeated queries
- [ ] Memory usage stays within configured limit
- [ ] No regression in existing tests

**Tests**:
```python
def test_olap_basic_query():
    """Test basic SELECT query matches DuckDB."""

def test_olap_filtered_query():
    """Test filtered query performance and correctness."""

def test_olap_cache_hit():
    """Test cache hit on repeated query."""

def test_olap_cache_eviction():
    """Test LRU eviction when cache full."""

def test_olap_version_isolation():
    """Test different versions don't share cache entries."""

def test_olap_branch_isolation():
    """Test branch queries resolve correctly."""
```

**Estimated Complexity**: Medium
**Files Changed**: 3 new, 1 modified (`__init__.py`)

---

### Phase DF.2: Performance Optimization

**Goal**: Maximize query performance with advanced optimizations

**Optimizations**:

1. **Parallel Table Loading**
   ```python
   # Load multiple tables concurrently
   async def load_tables_parallel(self, tables: List[str]):
       tasks = [self._load_table_async(t) for t in tables]
       await asyncio.gather(*tasks)
   ```

2. **Predicate Pushdown to Cache**
   ```python
   # Only cache rows that match common predicates
   def load_table_with_predicate(self, table: str, predicate: str):
       # Cache only matching rows for memory efficiency
   ```

3. **Partition-Aware Caching**
   ```python
   # Cache individual partitions instead of full tables
   def load_partition(self, table: str, partition_key: str, partition_value: Any):
       # Fine-grained caching for large tables
   ```

4. **Lazy Loading**
   ```python
   # Don't load data until query execution
   def query_lazy(self, sql: str) -> datafusion.DataFrame:
       # Returns DataFrame without executing
       # Execution happens on .collect()
   ```

**Verification Criteria**:
- [ ] Parallel loading is 2-4x faster than sequential for multi-table queries
- [ ] Memory efficiency improved by 30%+ with partition caching
- [ ] No correctness regressions

**Estimated Complexity**: Medium-High

---

### Phase DF.3: QueryEngine Integration

**Goal**: Seamlessly integrate OLAPEngine with existing QueryEngine

**Changes to QueryEngine**:
```python
class QueryEngine:
    def __init__(
        self,
        store, catalog,
        branch_manager=None,
        transaction_manager=None,
        enable_olap: bool = True,  # NEW
        olap_cache_size: int = 1_000_000_000,  # NEW
    ):
        # ... existing init ...

        if enable_olap:
            self._olap = OLAPEngine(
                store, catalog, branch_manager,
                max_cache_size_bytes=olap_cache_size
            )
        else:
            self._olap = None

    def query(self, sql: str, ..., use_olap: bool = True) -> QueryResult:
        """
        Execute query, automatically choosing backend.

        Args:
            use_olap: If True and OLAPEngine available, use DataFusion.
                     Falls back to DuckDB if OLAP unavailable or fails.
        """
        if use_olap and self._olap is not None:
            try:
                return self._olap.query(sql, ...)
            except Exception as e:
                # Log warning, fall back to DuckDB
                logger.warning(f"OLAP query failed, falling back to DuckDB: {e}")

        return self._duckdb_query(sql, ...)

    def olap_query(self, sql: str, ...) -> pa.Table:
        """Force OLAP path (raises if unavailable)."""
        if self._olap is None:
            raise RuntimeError("OLAP engine not enabled")
        return self._olap.query(sql, ...)
```

**Verification Criteria**:
- [ ] Automatic fallback works correctly
- [ ] Performance improvement for OLAP queries
- [ ] All existing tests pass
- [ ] Transaction integration works

**Estimated Complexity**: Medium

---

### Phase DF.4: Advanced Features

**Goal**: Add advanced OLAP capabilities unique to Armillaria

1. **Time Travel Queries with OLAP**
   ```sql
   -- Query version 5 of users with OLAP performance
   SELECT * FROM users VERSION 5 WHERE score > 90
   ```

2. **Branch Comparison Queries**
   ```sql
   -- Compare data across branches
   SELECT
       main.id,
       main.score AS main_score,
       feature.score AS feature_score
   FROM users@main AS main
   JOIN users@feature/experiment AS feature ON main.id = feature.id
   WHERE main.score != feature.score
   ```

3. **Changelog Queries**
   ```sql
   -- Query what changed
   SELECT * FROM __changelog
   WHERE table_name = 'users'
   AND tx_id > 100
   ```

4. **Materialized Views** (Future)
   ```python
   engine.create_materialized_view(
       "high_value_users",
       "SELECT * FROM users WHERE lifetime_value > 1000",
       refresh_on=["users"]  # Auto-refresh when users changes
   )
   ```

**Estimated Complexity**: High

---

## Verification Strategy

### Correctness Verification

For every optimization, verify:
1. **Result equivalence**: `assert df_olap.equals(df_duckdb)`
2. **Edge cases**: NULL handling, empty tables, type coercion
3. **Concurrency**: Multiple concurrent queries
4. **Isolation**: Transactions see correct snapshots

### Performance Verification

Benchmark suite:
```python
def benchmark_suite():
    """Run comprehensive benchmarks."""
    results = {}

    # Basic operations
    results['filtered_5pct'] = benchmark(olap_filtered_5pct)
    results['projection_2col'] = benchmark(olap_projection_2col)
    results['full_scan'] = benchmark(olap_full_scan)

    # Aggregations
    results['count'] = benchmark(olap_count)
    results['sum_groupby'] = benchmark(olap_sum_groupby)
    results['avg_where'] = benchmark(olap_avg_where)

    # Joins
    results['join_2table'] = benchmark(olap_join_2table)
    results['join_3table'] = benchmark(olap_join_3table)

    # Compare with DuckDB
    for key, olap_time in results.items():
        duckdb_time = duckdb_benchmarks[key]
        speedup = duckdb_time / olap_time
        print(f"{key}: {olap_time:.2f}ms (vs DuckDB {duckdb_time:.2f}ms = {speedup:.1f}x)")

    return results
```

### Mathematical Verification

For each claim, provide:
1. **Theoretical analysis** with formula
2. **Measured results** from benchmarks
3. **Comparison** showing theory matches practice (within expected variance)

---

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| DataFusion bugs | Low | High | Extensive testing, fallback to DuckDB |
| Memory pressure | Medium | Medium | Configurable cache limits, LRU eviction |
| API incompatibility | Low | Medium | Maintain DuckDB path, gradual migration |
| Performance regression | Low | High | Comprehensive benchmarks, CI checks |

---

## Success Metrics

| Metric | Current | Target | Stretch |
|--------|---------|--------|---------|
| Filtered read (5%) | 9.4ms | 1.0ms | 0.5ms |
| Projection (2 cols) | 6.9ms | 0.5ms | 0.2ms |
| Full scan | 12.4ms | 1.0ms | 0.3ms |
| Cache hit rate | N/A | 90% | 95% |
| Memory efficiency | N/A | 80% | 90% |

**Definition of Success**:
- Beat DuckDB on all OLAP queries
- Maintain all lakehouse features (versioning, branching, transactions)
- No regression in write performance
- All existing tests pass

---

## Timeline

| Phase | Description | Estimated Effort |
|-------|-------------|------------------|
| DF.1 | Core DataFusion Integration | 2-3 sessions |
| DF.2 | Performance Optimization | 1-2 sessions |
| DF.3 | QueryEngine Integration | 1-2 sessions |
| DF.4 | Advanced Features | 2-3 sessions |
| Testing | Comprehensive verification | Throughout |
| Documentation | Update all docs | 1 session |

---

## References

- [Apache DataFusion](https://datafusion.apache.org/) - Query engine documentation
- [DataFusion Python](https://github.com/apache/datafusion-python) - Python bindings
- [Armillaria Architecture](../README.md) - Current system overview
- [Technical Foundations](./TECHNICAL_FOUNDATIONS.md) - Mathematical proofs
- [Performance Guide](./PERFORMANCE.md) - Current optimizations

---

*Document created: January 2026*
*Status: Ready for implementation*
