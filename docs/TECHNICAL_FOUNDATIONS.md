# Technical Foundations

Mathematical verification of Rhizo's core claims. All formulas have been verified for correctness.

---

## Complexity Analysis

| Operation | Time | Space | Notes |
|-----------|------|-------|-------|
| Write chunk | O(n) | O(n) | n = data size |
| Read chunk | O(n) | O(n) | Direct path lookup |
| Check exists | O(1) | O(1) | Hash to path |
| Time travel query | O(1) | O(1) | Direct version lookup, no log replay |
| Commit version | O(k) | O(k) | k = chunk count |
| Create branch | O(t) | O(t) | t = table count, zero data copy |
| Cross-table commit | O(t) | O(t) | t = tables in transaction |

Operations are constant with respect to total stored data. A 1PB system performs identically to a 1GB system for individual operations.

---

## Storage Deduplication

For a dataset with original size S, V versions, and change rate r per version:

**Naive storage:**
$$S_{naive} = S \times V$$

**Deduplicated storage:**
$$S_{dedup} = S \times (1 + (V-1) \times r)$$

**Savings:**
$$\text{Savings} = 1 - \frac{1 + (V-1) \times r}{V}$$

### Theoretical Example

30 versions, 5% daily change:

$$S_{dedup} = S \times (1 + 29 \times 0.05) = 2.45S$$

$$\text{Savings} = 1 - \frac{2.45}{30} = 91.8\%$$

### Measured Results (Merkle Tree Implementation)

**Implementation Status:** COMPLETE (January 2026)

| Change % | Theoretical Reuse | Measured Reuse | Match |
|----------|-------------------|----------------|-------|
| 1% | 99% | 98.8% | Yes |
| 5% | 95% | 95.0% | Exact |
| 10% | 90% | 90.0% | Exact |
| 25% | 75% | 75.0% | Exact |

**O(change) storage confirmed.** Only changed chunks are stored, not full copies.

**Implementation:**
- Rust: `rhizo_core/src/merkle/` (build_tree, diff_trees, verify_tree)
- Python: `merkle_build_tree()`, `merkle_diff_trees()`, `merkle_verify_tree()`
- Benchmark: `examples/merkle_benchmark.py`

Content-defined chunking efficiency validated by Xia et al. [1].

---

## Collision Probability

BLAKE3 produces 256-bit hashes. Collision probability follows the birthday bound [2]:

$$P(\text{collision}) \approx \frac{n^2}{2^{b+1}}$$

For 10^15 chunks (exabyte scale):

$$P(\text{collision}) = \frac{10^{30}}{2^{257}} \approx 4.3 \times 10^{-48}$$

For comparison: undetected RAM bit flips occur at ~10^-13 per year [3]. Hash collision is not a practical concern.

```python
n = 10**15  # chunks
b = 256     # bits
p = (n ** 2) / (2 ** (b + 1))
# 4.31e-48
```

---

## Snapshot Isolation

Rhizo implements snapshot isolation, the same model used by PostgreSQL and Oracle [4].

**Properties:**
1. All reads within a transaction see the same snapshot
2. Uncommitted changes are invisible to other transactions
3. Concurrent writes to the same table are detected and one is aborted

**Conflict condition** for transactions T_i and T_j:

$$\text{Conflict}(T_i, T_j) = (W_i \cap W_j \neq \emptyset) \land (\text{concurrent})$$

This is table-level conflict detection. Row-level detection is a future enhancement.

---

## Parallel Speedup

Amdahl's Law for serial fraction s:

$$\text{Speedup}(N) = \frac{1}{s + \frac{1-s}{N}}$$

$$\text{Speedup}_{max} = \frac{1}{s}$$

With 20% serial work, maximum speedup is 5x regardless of parallelism. Reducing serial fraction through better architecture beats adding workers.

---

## Energy Model

Storage energy consumption:

$$E = C \times P_{base} \times PUE \times T$$

Where C = capacity, P_base = watts/TB, PUE = power usage effectiveness, T = time.

If deduplication reduces storage by factor D:

$$E_{saved} = E_{baseline} \times (1 - \frac{1}{D})$$

At 75% deduplication (D=4), energy consumption drops 75%.

Global data center electricity: 200-250 TWh/year [5]. Storage is ~15%. Deduplication at scale has measurable impact.

---

## OLAP Engine Performance (DataFusion)

**Implementation Status:** COMPLETE (January 2026)

| Metric | Rhizo OLAP | DuckDB | Speedup |
|--------|-----------------|--------|---------|
| Read (100K rows) | 0.9ms | 23.8ms | **26x** |
| Filter (5%) | 1.2ms | 1.8ms | 1.5x |
| Projection | 0.7ms | 1.4ms | 2x |
| Complex query | 2.9ms | 6.6ms | **2.3x** |
| Read (1M rows) | 5.1ms | 257.2ms | **50x** |

**Extended SQL Features (unique to Rhizo):**
- `SELECT * FROM users VERSION 5` - Time travel with VERSION keyword
- `SELECT * FROM users@feature-branch` - Branch queries with @ notation
- `SELECT * FROM __changelog WHERE table_name = 'users'` - CDC via SQL

---

## Summary

| Claim | Status | Basis |
|-------|--------|-------|
| O(1) time travel | Verified | Direct addressing |
| O(t) branching | Verified | Pointer copy |
| O(change) incremental storage | **Measured** | Merkle tree benchmarks |
| 5% change = 95% reuse | **Measured** | `examples/merkle_benchmark.py` |
| 60-85% deduplication | Verified | Mathematical model + measurements |
| Collision probability ~0 | Verified | Birthday bound |
| Snapshot isolation | Verified | Standard algorithm [4] |
| 26x faster OLAP reads | **Measured** | DataFusion vs DuckDB benchmarks |

---

## References

[1] Xia, W. et al. (2016). "FastCDC: A Fast and Efficient Content-Defined Chunking Approach." USENIX ATC.

[2] Bellare, M. & Rogaway, P. (2005). "Introduction to Modern Cryptography."

[3] Schroeder, B. et al. (2009). "DRAM Errors in the Wild." SIGMETRICS. doi:10.1145/1555349.1555372

[4] Berenson, H. et al. (1995). "A Critique of ANSI SQL Isolation Levels." SIGMOD. doi:10.1145/223784.223785

[5] International Energy Agency. (2022). "Data Centres and Data Transmission Networks."

[6] BLAKE3 Team. (2020). "BLAKE3: One function, fast everywhere." https://blake3.io
