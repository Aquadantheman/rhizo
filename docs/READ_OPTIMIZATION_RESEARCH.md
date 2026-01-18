# Armillaria Read Path Optimization Research

**Created:** January 2026
**Status:** Active Research
**Goal:** Close the read performance gap with Delta Lake through mathematical optimization

---

## Executive Summary

After Phase P.4, Armillaria's **write performance is competitive** with Delta Lake (211 MB/s vs 237 MB/s). However, **read performance lags** (183 MB/s vs 366 MB/s - Delta is ~2x faster).

This document outlines a research-driven approach to read optimization, starting with proven techniques (projection/predicate pushdown) and progressing to bleeding-edge optimizations (adaptive prefetching, learned indexes).

### Current Read Pipeline Analysis

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                        CURRENT READ PIPELINE                                │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  Disk ──► Read bytes ──► Decompress ──► Decode Parquet ──► Arrow ──► Python │
│           ~5ms            ~15ms          ~20ms             ~10ms    ~20ms   │
│                                                                             │
│  Total: ~70ms for 100K rows                                                 │
│  Bottlenecks: Decode ALL columns, FFI overhead                              │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Optimization Roadmap

| Phase | Optimization | Expected Speedup | Complexity | Status |
|-------|-------------|------------------|------------|--------|
| R.1 | Projection Pushdown | 2-5x (query dependent) | Low | **COMPLETE** |
| R.2 | Predicate Pushdown | 10-100x (selective queries) | Medium | Planned |
| R.3 | Dictionary Caching | 1.2-1.5x | Low | Planned |
| R.4 | Adaptive Prefetching | 1.5-2x | High | Research |
| R.5 | Learned Indexes | Variable | Very High | Research |

---

## Phase R.1: Projection Pushdown

### Theory

**Problem:** Current decoder reads ALL columns, even if query only needs 2 of 10.

**Solution:** Tell Parquet reader which columns to decode, skip the rest entirely.

**Mathematical Model:**
```
Current decode time: T = Σ(t_column_i) for all columns
With projection:     T' = Σ(t_column_i) for requested columns only

Speedup = n / k where n = total columns, k = requested columns

Example: 10 columns, query needs 2 → 5x speedup on decode phase
```

### Implementation Plan

#### Step 1.1: Rust Decoder Enhancement

**File:** `udr_core/src/parquet/decoder.rs`

Add method to decode specific columns:

```rust
use parquet::arrow::ProjectionMask;

impl ParquetDecoder {
    /// Decode only specific columns by index.
    ///
    /// This is significantly faster when you only need a subset of columns.
    /// Column indices are 0-based and refer to the schema order.
    pub fn decode_columns(
        &self,
        data: &[u8],
        column_indices: &[usize],
    ) -> Result<RecordBatch, ParquetError> {
        let bytes = Bytes::copy_from_slice(data);
        let builder = ParquetRecordBatchReaderBuilder::try_new(bytes)?;

        // Create projection mask for requested columns
        let parquet_schema = builder.parquet_schema();
        let mask = ProjectionMask::leaves(parquet_schema, column_indices.iter().copied());

        let reader = builder
            .with_projection(mask)
            .with_batch_size(self.batch_size)
            .build()?;

        // ... rest of decode logic
    }

    /// Decode only specific columns by name.
    pub fn decode_columns_by_name(
        &self,
        data: &[u8],
        column_names: &[&str],
    ) -> Result<RecordBatch, ParquetError> {
        // Convert names to indices using schema
        // ...
    }
}
```

#### Step 1.2: Python Bindings

**File:** `udr_python/src/lib.rs`

```rust
#[pymethods]
impl PyParquetDecoder {
    /// Decode only specific columns (by index).
    fn decode_columns<'py>(
        &self,
        py: Python<'py>,
        data: &[u8],
        column_indices: Vec<usize>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let batch = self.inner
            .decode_columns(data, &column_indices)
            .map_err(parquet_err_to_py)?;
        PyRecordBatch::new(batch).into_pyarrow(py)
    }
}
```

#### Step 1.3: TableReader Integration

**File:** `python/armillaria_query/reader.py`

```python
def read_arrow(
    self,
    table_name: str,
    version: Optional[int] = None,
    columns: Optional[List[str]] = None,  # NEW: column projection
) -> pa.Table:
    """
    Read a table version as an Arrow Table.

    Args:
        columns: If specified, only read these columns (projection pushdown)
    """
    # ... existing code ...

    if columns and self._native_decoder:
        # Get column indices from schema
        column_indices = self._get_column_indices(schema, columns)
        # Use projection pushdown
        batch = self._native_decoder.decode_columns(data, column_indices)
    else:
        # Full decode
        batch = self._native_decoder.decode(data)
```

### Actual Benchmark Results (Implemented!)

**Test data:** 100,000 rows, 10 columns, Zstd compression

| Query Type | Columns Used | Time (ms) | Actual Speedup | Theoretical |
|------------|--------------|-----------|----------------|-------------|
| SELECT * | 10/10 | 16.37ms | 1.0x | 1.0x |
| SELECT id, value | 2/10 | 7.94ms | **2.1x** | 5.0x |
| SELECT id | 1/10 | 3.21ms | **5.1x** | 10.0x |
| SELECT 5 cols | 5/10 | 9.96ms | **1.6x** | 2.0x |
| SELECT 3 cols | 3/10 | 6.04ms | **2.7x** | 3.3x |

**Analysis:** Actual speedups are significant! The 1-column case achieves 5.1x speedup (51% of theoretical max). The gap from theoretical is due to:
- Overhead in creating projection mask
- File metadata still read fully
- Fixed costs (decompression setup, Arrow schema creation)

### Test Cases

1. `test_projection_single_column` - Decode one column
2. `test_projection_multiple_columns` - Decode subset
3. `test_projection_all_columns` - Full decode (baseline)
4. `test_projection_preserves_data` - Data integrity check
5. `test_projection_invalid_index` - Error handling
6. `test_projection_by_name` - Name-based projection

---

## Phase R.2: Predicate Pushdown

### Theory

**Problem:** Even with projection, we read ALL rows, then filter in DuckDB.

**Solution:** Use Parquet row group statistics to skip entire row groups that can't match.

**Mathematical Model:**
```
Parquet file structure:
┌─────────────────────────────────────┐
│ Row Group 1: rows 0-99,999          │
│   min(value) = 0.1, max(value) = 0.5│
├─────────────────────────────────────┤
│ Row Group 2: rows 100,000-199,999   │
│   min(value) = 0.4, max(value) = 0.9│
├─────────────────────────────────────┤
│ Row Group 3: rows 200,000-299,999   │
│   min(value) = 0.8, max(value) = 1.0│
└─────────────────────────────────────┘

Query: SELECT * WHERE value > 0.95

With predicate pushdown:
- Row Group 1: max=0.5 < 0.95 → SKIP (no rows can match)
- Row Group 2: max=0.9 < 0.95 → SKIP (no rows can match)
- Row Group 3: max=1.0 > 0.95 → READ (might have matches)

Result: Read 1/3 of data → 3x speedup
```

**Selectivity Model:**
```
Let s = selectivity (fraction of rows matching)
Let g = number of row groups
Let r = rows per row group

Expected row groups to read ≈ g * (1 - (1-s)^r)

For highly selective queries (s << 1):
  Speedup ≈ g / (g * s * r) = 1 / (s * r)

Example: 1% selectivity, 100K rows/group
  Speedup ≈ 1 / (0.01 * 100000) = 100x theoretical max
```

### Implementation Considerations

1. **Statistics Storage:** Parquet already stores min/max per row group
2. **Predicate Types:**
   - Equality: `column = value`
   - Range: `column > value`, `column BETWEEN a AND b`
   - IN: `column IN (v1, v2, v3)`
3. **Compound Predicates:** AND/OR logic for multiple conditions

### API Design

```rust
pub struct PredicateFilter {
    column: String,
    op: FilterOp,
    value: ScalarValue,
}

pub enum FilterOp {
    Eq,
    Ne,
    Lt,
    Le,
    Gt,
    Ge,
    In(Vec<ScalarValue>),
}

impl ParquetDecoder {
    pub fn decode_with_filter(
        &self,
        data: &[u8],
        filters: &[PredicateFilter],
    ) -> Result<RecordBatch, ParquetError>;
}
```

### Implementation Path for R.2

1. **Use `parquet` crate's `RowFilter`** - Built-in row group pruning
2. **Expose filter API through Python** - `decode_with_filter(data, filters)`
3. **Integrate with QueryEngine** - Parse SQL WHERE clause to filters
4. **Combine with projection** - Maximum optimization

### Competitive Advantage

| Feature | Armillaria | Delta Lake | Iceberg |
|---------|------------|------------|---------|
| Projection Pushdown | **DONE (R.1)** | Yes | Yes |
| Predicate Pushdown | **Planned (R.2)** | Yes | Yes |
| Combined with Dedup | **Unique** | No | No |
| Combined with Branching | **Unique** | No | No |

The combination of pushdown optimizations WITH content-addressable deduplication is unique to Armillaria.

---

## Phase R.3: Dictionary Caching

### Theory

Parquet uses dictionary encoding for low-cardinality columns (e.g., "category" with values A, B, C, D).

**Current:** Dictionary decoded fresh for each chunk read.
**Optimization:** Cache dictionaries across reads of the same table.

```
Dictionary structure:
  Index → Value mapping
  0 → "Category_A"
  1 → "Category_B"
  2 → "Category_C"

Data stored as: [0, 1, 0, 2, 1, 0, ...] (indices only)

Cache hit: Skip dictionary decode, just apply cached mapping
```

**Expected Gain:** 1.2-1.5x for tables with many dictionary-encoded columns.

---

## Phase R.4: Adaptive Prefetching (Research)

### Information-Theoretic Foundation

**Goal:** Predict which chunks will be accessed and prefetch them before they're needed.

**Model:** Use Shannon entropy to quantify uncertainty about next access.

```
H(next_chunk | history) = -Σ P(c|h) log₂ P(c|h)

Where:
- H = entropy (bits of uncertainty)
- c = candidate chunk
- h = access history
```

**Optimal Prefetch Strategy:**

Given prefetch budget B (in bytes) and candidates C:
```
maximize Σ P(access(c)) × benefit(c)
subject to Σ size(c) ≤ B

Where benefit(c) = latency_saved(c) if cache_hit else 0
```

This is a variant of the **knapsack problem** - solvable with dynamic programming.

### Predictive State Representation (PSR)

Instead of storing full access history, compress into a predictive state:

```rust
pub struct PredictiveState {
    // Compressed representation of access patterns
    transition_matrix: HashMap<ChunkId, HashMap<ChunkId, f64>>,
    // Decay factor for temporal patterns
    decay: f64,
    // Current state estimate
    state: Vec<f64>,
}

impl PredictiveState {
    pub fn update(&mut self, accessed_chunk: ChunkId) {
        // Update transition probabilities
        // Apply temporal decay
    }

    pub fn predict_next(&self, budget: usize) -> Vec<ChunkId> {
        // Solve knapsack for optimal prefetch set
    }
}
```

### Research Questions

1. **What history length is optimal?** Trade-off between accuracy and memory.
2. **How to handle concept drift?** Access patterns change over time.
3. **Multi-table patterns?** Joins create cross-table access patterns.

---

## Phase R.5: Learned Indexes (Research)

### Concept

Replace hash-based chunk lookup with a learned model that predicts chunk location.

**Traditional:** `hash(content) → filename` (O(1) but cache-unfriendly)
**Learned:** `ML_model(features) → approximate_location → binary_search`

### Why It Might Help

1. **Spatial locality:** Similar data tends to be accessed together
2. **Temporal locality:** Recently written data accessed soon after
3. **Semantic locality:** Related tables accessed in patterns

### Implementation Sketch

```python
class LearnedChunkIndex:
    def __init__(self):
        self.model = train_model(historical_accesses)

    def locate(self, chunk_hash: str) -> str:
        # Predict approximate location
        predicted_bucket = self.model.predict(hash_to_features(chunk_hash))
        # Binary search within predicted range
        return binary_search(predicted_bucket, chunk_hash)
```

### Academic References

1. "The Case for Learned Index Structures" (Kraska et al., 2018)
2. "SageDB: A Learned Database System" (Kraska et al., 2019)
3. "Tsunami: A Learned Multi-dimensional Index" (Ding et al., 2020)

---

## Competitive Analysis

### After Phase R.1 + R.2 (Achievable)

| Metric | Armillaria | Delta Lake | Gap |
|--------|------------|------------|-----|
| Full scan read | 183 MB/s | 366 MB/s | 2x slower |
| Projected read (2/10 cols) | ~500 MB/s | ~500 MB/s | **Parity** |
| Selective read (1% rows) | ~5000 MB/s | ~5000 MB/s | **Parity** |
| + Deduplication | **Yes** | No | **Advantage** |
| + Zero-copy branching | **Yes** | No | **Advantage** |

### Unique Value Proposition

Armillaria with R.1+R.2 offers:
1. **Competitive read performance** for analytical queries
2. **84% storage deduplication** (Delta can't do this)
3. **280-byte branching** (Delta: 14.70 MB)
4. **Cross-table ACID** (Delta can't do this)

---

## Testing Strategy

### Regression Prevention

Every optimization phase includes:
1. **Baseline benchmark** before changes
2. **Unit tests** for new functionality
3. **Integration tests** for end-to-end paths
4. **Benchmark comparison** after changes
5. **Documentation update**

### Benchmark Suite

```python
def benchmark_read_optimization():
    """Comprehensive read benchmark suite."""

    # Test data: 100K rows, 10 columns
    df = generate_test_data(rows=100_000, columns=10)

    results = {}

    # Baseline: Full scan
    results['full_scan'] = time_read("SELECT * FROM test")

    # Projection: 2 columns
    results['projection_2'] = time_read("SELECT id, value FROM test")

    # Projection: 1 column
    results['projection_1'] = time_read("SELECT value FROM test")

    # Predicate: 10% selectivity
    results['predicate_10pct'] = time_read("SELECT * FROM test WHERE value > 0.9")

    # Predicate: 1% selectivity
    results['predicate_1pct'] = time_read("SELECT * FROM test WHERE value > 0.99")

    # Combined: projection + predicate
    results['combined'] = time_read("SELECT id FROM test WHERE value > 0.99")

    return results
```

---

## Implementation Timeline

| Phase | Description | Estimated Effort | Checkpoint |
|-------|-------------|------------------|------------|
| R.1.1 | Rust decoder projection | 2-3 hours | Rust tests pass |
| R.1.2 | Python bindings | 1-2 hours | Python tests pass |
| R.1.3 | TableReader integration | 1-2 hours | Integration tests pass |
| R.1.4 | Benchmarks & docs | 1-2 hours | Docs updated |
| R.2.1 | Predicate filter types | 2-3 hours | Rust tests pass |
| R.2.2 | Row group statistics | 2-3 hours | Stats working |
| R.2.3 | Integration | 2-3 hours | End-to-end working |

---

## Success Metrics

### Phase R.1 Success
- [x] Projection reduces decode time proportionally to columns selected (5.1x for 1 col)
- [x] No regression in full-scan performance (16.37ms baseline preserved)
- [x] All existing tests pass (38 Python tests, 181 Rust tests)
- [x] Documented with examples

### Phase R.2 Success
- [ ] Selective queries skip row groups correctly
- [ ] 10x+ speedup for 1% selectivity queries
- [ ] Statistics preserved through deduplication
- [ ] Documented with examples

### Overall Success
- [ ] Read performance competitive with Delta Lake for typical queries
- [ ] Maintain all architectural advantages (dedup, branching, ACID)
- [ ] Research documented for future optimization

---

*Document version: January 2026*
*Author: Research collaboration on bleeding-edge data infrastructure optimization*
