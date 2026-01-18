# Rhizo Performance Guide

This document summarizes the performance optimizations implemented in Rhizo and how to use them effectively.

## Performance Phases

### Phase R.1: Projection Pushdown
**Speedup: 3-5x** (when reading subset of columns)

Only decode the columns you need. The speedup is approximately `n/k` where `n` is total columns and `k` is requested columns.

```python
# Instead of reading all columns:
table = reader.read_arrow("my_table")

# Read only what you need:
table = reader.read_arrow("my_table", columns=["id", "name", "score"])
```

### Phase R.2: Predicate Pushdown
**Speedup: ~2-9x** (depending on selectivity and row-group distribution)

Filter rows during decoding rather than after. Uses min/max statistics to skip entire row groups.

```python
from rhizo import Filter

# Filter during read, not after:
table = reader.read_arrow(
    "my_table",
    filters=[Filter("age").gt(50), Filter("status").eq("active")]
)
```

### Phase R.3: Row-Group Pruning
**Speedup: 8-10x** (when filter can eliminate row groups)

Automatically enabled with predicate pushdown. Works best when:
- Data is partitioned (sorted or clustered by filter columns)
- Files have multiple row groups
- Filter eliminates most row groups

Check pruning effectiveness:
```python
from rhizo import PyParquetDecoder, PyPredicateFilter

decoder = PyParquetDecoder()
filter = PyPredicateFilter("id", "gt", 9000)
total, pruned, kept = decoder.get_pruning_stats(data, [filter])
print(f"Pruned {pruned}/{total} row groups ({100*pruned/total:.0f}%)")
```

### Phase R.4: Parallel Encoding
**Speedup: 3-4x** (for multi-chunk writes)

Automatically enabled when writing data that splits into multiple chunks. Uses Rayon for parallel Parquet encoding.

```python
# Large writes automatically parallelize:
writer = TableWriter(store, catalog, chunk_size_rows=100_000)
writer.write("big_table", million_row_df)  # Encoded in parallel
```

## Benchmark Results

### Write Performance (1M rows, 10 chunks)
| Method | Time | Speedup |
|--------|------|---------|
| Sequential encoding | 330ms | baseline |
| Parallel encoding (Rayon) | 86ms | **3.84x** |

### Read Performance (100k rows, 10 columns, 10 row groups)
| Method | Time | Speedup |
|--------|------|---------|
| Full decode | 11.5ms | baseline |
| 2 columns (projection) | 3.7ms | **3.1x** |
| 1 column (projection) | 2.2ms | **5.3x** |
| Filter + row-group pruning | 1.3ms | **8.8x** |
| Combined (2 cols + filter) | 0.7ms | **17.2x** |

## Mathematical Model

The theoretical speedup for combined optimizations:

```
Speedup = (G/g) x (n/k) x (1/s)

Where:
  G = total row groups
  g = row groups that might contain matches
  n = total columns
  k = requested columns
  s = selectivity (fraction of rows matching)
```

**Example:** 10 row groups, 1 kept, 10 columns, 2 requested, 5% selectivity
```
Speedup = (10/1) x (10/2) x (1/0.05) = 10 x 5 x 20 = 1000x
```

Real-world results are lower due to fixed overhead (metadata parsing, Arrow conversion, etc.)

## Best Practices

1. **Always specify columns** when you don't need all of them
2. **Use filters at read time**, not after loading
3. **Sort data by commonly filtered columns** to improve row-group pruning
4. **Set appropriate chunk sizes** for parallel encoding benefit
5. **Use native Rust encoder/decoder** (enabled by default)

## Filter Operations

The `Filter` class supports these operations:

| Method | SQL Equivalent |
|--------|----------------|
| `.eq(value)` | `column = value` |
| `.ne(value)` | `column != value` |
| `.lt(value)` | `column < value` |
| `.le(value)` | `column <= value` |
| `.gt(value)` | `column > value` |
| `.ge(value)` | `column >= value` |

Multiple filters are ANDed together:
```python
# age > 50 AND status = 'active'
filters=[Filter("age").gt(50), Filter("status").eq("active")]
```

## OLAP Engine (DataFusion)

For analytical workloads, Rhizo includes a high-performance OLAP engine powered by DataFusion.

### Enable OLAP

```python
engine = QueryEngine(
    store, catalog,
    enable_olap=True,
    olap_cache_size=100_000_000  # 100MB cache
)
```

### Performance

| Metric | OLAP Engine | DuckDB | Speedup |
|--------|-------------|--------|---------|
| Read (100K rows) | 0.9ms | 23.8ms | **26x** |
| Filter (5%) | 1.2ms | 1.8ms | 1.5x |
| Projection | 0.7ms | 1.4ms | 2x |
| Complex query | 2.9ms | 6.6ms | **2.3x** |
| Read (1M rows) | 5.1ms | 257.2ms | **50x** |

### Extended SQL Syntax

```python
# Time travel with VERSION keyword
result = engine.query_time_travel("SELECT * FROM users VERSION 5")

# Branch queries with @ notation
result = engine.query_time_travel("SELECT * FROM users@feature-branch")

# Changelog queries
result = engine.query_changelog("SELECT * FROM __changelog WHERE table_name = 'users'")
```

### When to Use OLAP vs DuckDB

| Use Case | Recommendation |
|----------|----------------|
| Repeated analytical queries | OLAP (caching helps) |
| Ad-hoc exploration | DuckDB |
| Time travel with VERSION syntax | OLAP |
| Branch comparison queries | OLAP |
| Changelog/CDC queries | OLAP |

## Architecture

```
Python DataFrame
      |
      v
TableWriter --[parallel encode]--> Parquet chunks --> ChunkStore
      |
      v
    Catalog (version metadata)

TableReader --[row-group pruning]--> Kept row groups
      |          |
      |          v
      |    [projection pushdown]
      |          |
      |          v
      |    [predicate pushdown]
      |          |
      v          v
Arrow RecordBatch (filtered, projected)
      |
      v
OLAP Engine (DataFusion) --[LRU cache]--> Fast repeated queries
```

## Arrow Chunk Cache (Phase P.5)

**Speedup: 15x** for repeated reads

The Arrow chunk cache stores decoded Arrow RecordBatches, eliminating both disk I/O and Parquet decoding on cache hits.

### Enable Caching (enabled by default)

```python
reader = TableReader(
    store, catalog,
    enable_chunk_cache=True,      # Default: True
    chunk_cache_size_mb=100       # Default: 100MB
)

# Check cache performance
stats = reader.cache_stats()
print(f"Hit rate: {stats.hit_rate:.1%}")
print(f"Utilization: {stats.utilization:.1%}")

# Clear cache if needed
reader.clear_cache()
```

### Why It Works

Rhizo's content-addressed storage makes caching highly effective:
- **Immutable chunks**: Same hash = same content forever (no invalidation needed)
- **Shared across tables**: Common data between tables hits cache
- **Shared across versions**: Unchanged data between versions hits cache
- **Shared across branches**: Branched data that wasn't modified hits cache

### Performance

| Metric | Value |
|--------|-------|
| Cache hit read | 0.24ms |
| Uncached read | 3.6ms |
| Speedup | **15x** |
| Typical hit rate | 91%+ |

### Best Practices

1. **Keep default settings** for most workloads (100MB cache, enabled)
2. **Increase cache size** for read-heavy analytical workloads
3. **Check hit rate** to validate cache effectiveness
4. **Use with time travel** - historical versions often share chunks with current
