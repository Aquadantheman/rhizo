# Armillaria Performance Optimization Roadmap

**Created:** January 2026
**Status:** Active Development
**Goal:** Close the performance gap with Delta Lake while preserving architectural advantages

---

## Executive Summary

Armillaria has unique features (cross-table ACID, zero-copy branching, 84% dedup) but trails Delta Lake in raw throughput by 30-50%. This roadmap addresses performance without compromising the architecture.

### Current vs Target Performance

| Metric | Current | Target | Improvement |
|--------|---------|--------|-------------|
| Write throughput | 167-335 MB/s | 500-800 MB/s | 2-3× |
| Read throughput | 166-432 MB/s | 500-800 MB/s | 1.5-2× |
| Branch overhead | 280 bytes | 280 bytes | Maintained |
| Storage dedup | 84% | 84% | Maintained |

---

## Root Cause Analysis

### Identified Bottlenecks

1. **Sequential Chunk I/O** - Each chunk processed one at a time
2. **FFI Overhead** - One Python↔Rust call per chunk
3. **Memory Copies** - Data copied multiple times
4. **Python GIL** - Parquet encoding holds the GIL

### Performance Model

```
Current Write Time:
T = T_arrow + Σ(T_parquet + T_ffi + T_hash + T_disk)
              ↑ Sequential loop over N chunks

Target Write Time:
T = T_arrow + T_parquet_batch + T_ffi_once + (T_hash + T_disk) / num_threads
                                             ↑ Parallel processing
```

---

## Phase 1: Parallel Chunk I/O

**Priority:** HIGHEST
**Expected Gain:** 3-5× throughput
**Risk:** LOW
**Effort:** 4-6 hours

### Overview

Add parallel processing using Rayon for chunk hashing and I/O operations. This is the highest-impact optimization with lowest risk.

### Step-by-Step Implementation

#### Step 1.1: Add Rayon Dependency

**File:** `udr_core/Cargo.toml`

```toml
[dependencies]
rayon = "1.10"  # Add this
```

**Verification:**
```bash
cargo build --all
cargo test --all
```

#### Step 1.2: Implement `put_batch` in Rust

**File:** `udr_core/src/chunk_store/store.rs`

```rust
/// Store multiple chunks in parallel, returning their hashes.
///
/// This is significantly faster than calling `put()` in a loop because:
/// 1. BLAKE3 hashing runs in parallel across cores
/// 2. Disk I/O is batched
/// 3. Single FFI call overhead instead of N calls
pub fn put_batch(&self, chunks: &[&[u8]]) -> Result<Vec<String>, ChunkStoreError> {
    use rayon::prelude::*;

    chunks
        .par_iter()
        .map(|data| self.put(data))
        .collect()
}
```

**Tests to add:**
- `test_put_batch_empty` - Empty input returns empty vec
- `test_put_batch_single` - Single chunk works
- `test_put_batch_multiple` - Multiple chunks all stored
- `test_put_batch_dedup` - Duplicate chunks return same hash
- `test_put_batch_large` - 100+ chunks processed correctly

#### Step 1.3: Implement `get_batch` in Rust

**File:** `udr_core/src/chunk_store/store.rs`

```rust
/// Retrieve multiple chunks in parallel by their hashes.
///
/// Returns results in the same order as input hashes.
/// If any chunk is not found, returns an error.
pub fn get_batch(&self, hashes: &[&str]) -> Result<Vec<Vec<u8>>, ChunkStoreError> {
    use rayon::prelude::*;

    hashes
        .par_iter()
        .map(|hash| self.get(hash))
        .collect()
}

/// Retrieve multiple chunks with verification in parallel.
pub fn get_batch_verified(&self, hashes: &[&str]) -> Result<Vec<Vec<u8>>, ChunkStoreError> {
    use rayon::prelude::*;

    hashes
        .par_iter()
        .map(|hash| self.get_verified(hash))
        .collect()
}
```

**Tests to add:**
- `test_get_batch_empty` - Empty input returns empty vec
- `test_get_batch_single` - Single hash works
- `test_get_batch_multiple` - Multiple hashes all retrieved
- `test_get_batch_not_found` - Missing hash returns error
- `test_get_batch_verified` - Verification works in parallel

#### Step 1.4: Add Python Bindings

**File:** `udr_python/src/lib.rs`

```rust
/// Store multiple chunks in parallel.
///
/// Args:
///     chunks: List of byte arrays to store
///
/// Returns:
///     List of hashes in same order as input
fn put_batch(&self, chunks: Vec<Vec<u8>>) -> PyResult<Vec<String>> {
    let refs: Vec<&[u8]> = chunks.iter().map(|c| c.as_slice()).collect();
    self.inner.put_batch(&refs).map_err(chunk_err_to_py)
}

/// Retrieve multiple chunks in parallel.
///
/// Args:
///     hashes: List of chunk hashes to retrieve
///
/// Returns:
///     List of chunk data in same order as input
fn get_batch(&self, hashes: Vec<String>) -> PyResult<Vec<Vec<u8>>> {
    let refs: Vec<&str> = hashes.iter().map(|s| s.as_str()).collect();
    self.inner.get_batch(&refs).map_err(chunk_err_to_py)
}

/// Retrieve multiple chunks with verification in parallel.
fn get_batch_verified(&self, hashes: Vec<String>) -> PyResult<Vec<Vec<u8>>> {
    let refs: Vec<&str> = hashes.iter().map(|s| s.as_str()).collect();
    self.inner.get_batch_verified(&refs).map_err(chunk_err_to_py)
}
```

**Update type stubs:** `python/armillaria.pyi`

#### Step 1.5: Update TableWriter

**File:** `python/armillaria_query/writer.py`

Change from:
```python
for chunk in chunks:
    parquet_bytes = self._to_parquet_bytes(chunk)
    chunk_hash = self.store.put(parquet_bytes)
    chunk_hashes.append(chunk_hash)
```

To:
```python
# Serialize all chunks first
parquet_chunks = [self._to_parquet_bytes(chunk) for chunk in chunks]

# Store all chunks in parallel (single FFI call)
chunk_hashes = self.store.put_batch(parquet_chunks)
```

#### Step 1.6: Update TableReader

**File:** `python/armillaria_query/reader.py`

Change from:
```python
for chunk_hash in metadata.chunk_hashes:
    chunk_data = self._fetch_chunk(chunk_hash)
    arrow_table = self._parquet_to_arrow(chunk_data)
    yield arrow_table
```

To:
```python
# Fetch all chunks in parallel (single FFI call)
if self.verify_integrity:
    all_chunk_data = self.store.get_batch_verified(metadata.chunk_hashes)
else:
    all_chunk_data = self.store.get_batch(metadata.chunk_hashes)

# Parse Parquet (still sequential, but could parallelize with ThreadPoolExecutor)
for chunk_data in all_chunk_data:
    arrow_table = self._parquet_to_arrow(chunk_data)
    yield arrow_table
```

#### Step 1.7: Verification

```bash
# Run all Rust tests
cargo test --all

# Run all Python tests
pytest tests/ -v

# Run benchmarks
python examples/comparison_benchmark.py
python examples/full_competition_benchmark.py
```

### Success Criteria

- [x] All 160 Rust tests pass (added 11 batch + 7 mmap tests)
- [x] All 168 Python tests pass (added 11 batch + 5 mmap tests)
- [x] TableWriter uses put_batch for parallel chunk storage
- [x] TableReader uses get_batch for parallel chunk retrieval
- [x] No regression in deduplication or branching

**Status: COMPLETE** (January 2026)

---

## Phase 2: Memory-Mapped Reads

**Priority:** MEDIUM
**Expected Gain:** Infrastructure for future zero-copy optimizations
**Risk:** LOW
**Effort:** 2-3 hours
**Status: COMPLETE** (January 2026)

### Overview

Use memory-mapped files for reading chunks, eliminating the copy from kernel to userspace.

### Implementation

#### Step 2.1: Add memmap2 Dependency

**File:** `udr_core/Cargo.toml`

```toml
[dependencies]
memmap2 = "0.9"
```

#### Step 2.2: Add `get_mmap` Method

**File:** `udr_core/src/chunk_store/store.rs`

```rust
use memmap2::Mmap;

/// Get a memory-mapped view of a chunk.
///
/// This is faster than `get()` because:
/// 1. No copy from kernel to userspace
/// 2. OS handles caching automatically
/// 3. Can be used with zero-copy Arrow parsing
pub fn get_mmap(&self, hash: &str) -> Result<Mmap, ChunkStoreError> {
    let path = self.hash_to_path(hash)?;
    let file = std::fs::File::open(&path)
        .map_err(|e| if e.kind() == std::io::ErrorKind::NotFound {
            ChunkStoreError::NotFound(hash.to_string())
        } else {
            ChunkStoreError::Io(e)
        })?;

    // SAFETY: We're only reading the file, and it's immutable once written
    unsafe { Mmap::map(&file) }.map_err(ChunkStoreError::Io)
}
```

#### Step 2.3: Python Bindings

Memory-mapped files are tricky across FFI. Two options:

**Option A (Simpler):** Return bytes, Python uses buffer protocol
```rust
fn get_mmap(&self, hash: &str) -> PyResult<Py<PyBytes>> {
    let mmap = self.inner.get_mmap(hash).map_err(chunk_err_to_py)?;
    Python::with_gil(|py| Ok(PyBytes::new(py, &mmap).into()))
}
```

**Option B (Zero-copy):** Use PyBuffer (more complex, higher performance)

### Success Criteria

- [x] `get_mmap` and `get_mmap_batch` implemented in Rust with 7 tests
- [x] Python bindings for mmap methods with 5 tests
- [x] TableReader supports `use_mmap` flag
- [x] All 328 tests pass (160 Rust + 168 Python)

**Note:** Current implementation copies mmap to Python bytes. True zero-copy
via PyBuffer protocol is future work that would enable actual throughput gains.

---

## Phase 3: Batch Parquet Parsing

**Priority:** MEDIUM
**Expected Gain:** 1.2-1.5× for multi-chunk tables
**Risk:** LOW
**Effort:** 2-3 hours

### Overview

Use Python's ThreadPoolExecutor to parallelize Parquet parsing while staying in Python.

### Implementation

**File:** `python/armillaria_query/reader.py`

```python
from concurrent.futures import ThreadPoolExecutor

def read_all(self, table_name: str, version: Optional[int] = None) -> pa.Table:
    metadata = self.get_metadata(table_name, version)

    # Fetch all chunks in parallel from Rust
    all_chunk_data = self.store.get_batch(metadata.chunk_hashes)

    # Parse Parquet in parallel using thread pool
    with ThreadPoolExecutor(max_workers=4) as executor:
        arrow_tables = list(executor.map(self._parquet_to_arrow, all_chunk_data))

    return pa.concat_tables(arrow_tables)
```

### Success Criteria

- [ ] Multi-chunk read throughput improved by 1.2×
- [ ] All tests pass
- [ ] Thread safety verified

---

## Phase 4: Native Arrow/Parquet (Optional)

**Priority:** LOW (high effort)
**Expected Gain:** 1.5× for Parquet-heavy workloads
**Risk:** MEDIUM
**Effort:** 8-16 hours

### Overview

Move Parquet encoding/decoding to Rust using arrow-rs and parquet crates. This eliminates Python GIL contention.

### Implementation Sketch

```rust
// In Cargo.toml
arrow = "50"
parquet = "50"

// New methods
fn put_arrow(&self, arrow_ipc: &[u8]) -> PyResult<Vec<String>> {
    // Decode Arrow IPC format
    // Encode as Parquet
    // Store chunks
}

fn get_arrow(&self, hashes: Vec<String>) -> PyResult<Vec<u8>> {
    // Get chunks
    // Decode Parquet
    // Encode as Arrow IPC
}
```

### Decision Point

Only implement if Phase 1-3 don't achieve target performance. The complexity is high and Python PyArrow is already well-optimized.

---

## Phase 5: Predicate Pushdown (Future)

**Priority:** LOW
**Expected Gain:** Variable (query-dependent)
**Risk:** MEDIUM
**Effort:** 16+ hours

### Overview

Store column statistics in catalog, skip chunks that don't match query predicates.

### Prerequisites

- Chunk-level min/max statistics
- Query plan inspection
- Catalog schema changes

### Decision Point

Implement when users have queries that scan large tables with selective filters.

---

## Testing Strategy

### Regression Prevention

1. **Before any change:** Run full test suite
   ```bash
   cargo test --all && pytest tests/ -v
   ```

2. **After each step:** Run affected tests
   ```bash
   cargo test chunk_store  # For Rust changes
   pytest tests/test_core.py  # For Python changes
   ```

3. **After phase completion:** Run benchmarks
   ```bash
   python examples/comparison_benchmark.py
   ```

### New Tests to Add

| Phase | Test File | Tests |
|-------|-----------|-------|
| 1 | `udr_core/src/chunk_store/store.rs` | 10 new tests for batch methods |
| 1 | `tests/test_core.py` | 5 new tests for Python batch methods |
| 2 | `udr_core/src/chunk_store/store.rs` | 3 new tests for mmap |
| 3 | `tests/test_query_layer.py` | 2 new tests for parallel parsing |

### Benchmark Tracking

Track these metrics after each phase:

| Metric | Baseline | Phase 1 | Phase 2 | Phase 3 |
|--------|----------|---------|---------|---------|
| Write 100K rows | | | | |
| Read 100K rows | | | | |
| Write 500K rows | | | | |
| Read 500K rows | | | | |

---

## Risk Mitigation

| Risk | Mitigation |
|------|------------|
| Rayon thread overhead | Benchmark with small chunks; add minimum chunk threshold |
| Memory pressure from batch | Add optional chunk_limit parameter |
| Breaking existing API | Keep original methods, add batch as new methods |
| Test flakiness from parallelism | Use deterministic thread counts in tests |

---

## Success Metrics

### Phase 1 Complete When:

- [ ] `put_batch` and `get_batch` implemented and tested
- [ ] Python bindings working
- [ ] TableWriter/TableReader updated
- [ ] All 295 tests passing
- [ ] Write throughput ≥ 400 MB/s (2× improvement)
- [ ] Read throughput ≥ 350 MB/s (1.5× improvement)

### Full Roadmap Complete When:

- [ ] Write throughput ≥ 500 MB/s
- [ ] Read throughput ≥ 500 MB/s
- [ ] Competitive with Delta Lake on raw I/O
- [ ] Maintains all architectural advantages

---

## Appendix: Benchmark Commands

```bash
# Quick benchmark
python -c "
from examples.comparison_benchmark import *
import tempfile
import pandas as pd
import numpy as np

df = pd.DataFrame({'id': range(100000), 'value': np.random.random(100000)})
with tempfile.TemporaryDirectory() as d:
    r = benchmark_armillaria_write(df, d)
    print(f'Write: {r.throughput_mb_s:.1f} MB/s')
"

# Full benchmark suite
python examples/comparison_benchmark.py
python examples/full_competition_benchmark.py
python examples/merkle_benchmark.py
```

---

*Document version: January 2026*
*Last updated: Phase 1 in progress*
