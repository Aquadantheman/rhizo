# Rhizo Performance Guide

This document summarizes the performance optimizations implemented in Rhizo and how to use them effectively.

## Projection Pushdown
**Speedup: 3-5x** (when reading subset of columns)

Only decode the columns you need. The speedup is approximately `n/k` where `n` is total columns and `k` is requested columns.

```python
# Instead of reading all columns:
table = reader.read_arrow("my_table")

# Read only what you need:
table = reader.read_arrow("my_table", columns=["id", "name", "score"])
```

## Predicate Pushdown
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

## Row-Group Pruning
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

## Parallel Encoding
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

$$\text{Speedup} = \frac{G}{g} \times \frac{n}{k} \times \frac{1}{s}$$

Where:
- $G$ = total row groups
- $g$ = row groups that might contain matches
- $n$ = total columns
- $k$ = requested columns
- $s$ = selectivity (fraction of rows matching)

**Example:** 10 row groups, 1 kept, 10 columns, 2 requested, 5% selectivity

$$\text{Speedup} = \frac{10}{1} \times \frac{10}{2} \times \frac{1}{0.05} = 10 \times 5 \times 20 = 1000\times$$

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
| Read (100K rows) | 0.9ms | 26ms | **32x** |
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

## Arrow Chunk Cache

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
| Typical hit rate | 97.2%+ |

### Best Practices

1. **Keep default settings** for most workloads (100MB cache, enabled)
2. **Increase cache size** for read-heavy analytical workloads
3. **Check hit rate** to validate cache effectiveness
4. **Use with time travel** - historical versions often share chunks with current

---

## Verify These Claims

Every headline number can be reproduced on your machine. No trust required.

### Verify Transaction Latency

**Claim**: 0.001ms local commit — 30,000x faster than remote 2PC, 59x faster than localhost 2PC, 355x faster than durable writes

```bash
# Localhost benchmarks
python benchmarks/real_consensus_benchmark.py

# Cloud benchmarks (requires remote participant servers)
python benchmarks/real_consensus_benchmark.py --remote-2pc host1:9000,host2:9000
```

**What it measures**: Real coordination overhead — both on localhost and over the network. No simulated delays.

**Localhost results** (same machine):

| System | Latency | Speedup | Measured? |
|--------|---------|---------|-----------|
| Rhizo algebraic (ADD) | 0.001ms | baseline | Yes |
| SQLite WAL (NORMAL sync) | 0.045ms | **41x** | Yes |
| Localhost 2PC (3 nodes over TCP) | 0.065ms | **59x** | Yes |
| SQLite WAL (FULL sync / fsync) | 0.386ms | **355x** | Yes |

**Same-region cloud results** (NYC → AWS Virginia, ~18ms RTT):

| System | Latency | Speedup | Measured? |
|--------|---------|---------|-----------|
| Rhizo algebraic (ADD) | 0.001ms | baseline | Yes |
| Remote 2PC (3 machines over network) | 33.3ms | **30,000x** | Yes |

**Cross-continent cloud results** (NYC → AWS Oregon + AWS Ireland, ~92ms + ~107ms RTT):

| System | Latency (mean) | p50 | p95 | p99 | Speedup | N |
|--------|---------------|-----|-----|-----|---------|---|
| Rhizo algebraic (ADD) | 0.001ms | 0.001ms | 0.002ms | 0.002ms | baseline | 500 |
| Remote 2PC (3 machines) | 187.9ms | 188.1ms | 191.2ms | 194.3ms | **160,000x** | 500 |

The localhost 2PC benchmark spawns real OS processes coordinating over TCP sockets. The cloud benchmark connects to `2pc_participant_server.py` instances running on AWS EC2 VMs. Each transaction performs PREPARE (2 round-trips) + COMMIT (2 round-trips) = 4 network round-trips.

**Scaling**: At ~18ms RTT we measured 30,000x. At ~100ms RTT (cross-continent) we measured **160,000x**.

**Why coordination-free commits are faster**: Algebraic operations (ADD, MAX, UNION) satisfy commutativity and associativity, mathematically guaranteeing convergence without coordination. See [TECHNICAL_FOUNDATIONS.md](TECHNICAL_FOUNDATIONS.md#algebraic-classification-for-conflict-free-merge).

---

### Verify Energy Efficiency

**Claim**: 2.2e-11 kWh per transaction, 97,943x less than consensus

```bash
pip install codecarbon  # Required for energy measurement
python benchmarks/energy_benchmark.py
```

**What it measures**: Actual energy consumption via CodeCarbon (CPU power draw × time) for local commits vs simulated consensus operations.

**Expected output**:
```
5. ENERGY COMPARISON: RHIZO vs CONSENSUS
  Rhizo energy/tx:     0.000000000022 kWh
  Consensus energy/tx: 0.000002118639 kWh
  SAVINGS RATIO:       95100x less energy
```

**Why it's valid**: Energy = Power × Time. By eliminating consensus round-trips (100ms → 0.02ms), both latency and energy drop proportionally. Network idle power and CPU wait states are eliminated entirely.

---

### Verify Branch Overhead

**Claim**: 140 bytes per branch, 450,000x smaller than Delta Lake

```bash
python benchmarks/comprehensive_benchmark.py
```

**What it measures**: Disk space added when creating a branch in Rhizo (JSON pointer file) vs Delta Lake (full directory copy, since Delta has no native branching).

**Expected output**:
```
BRANCHING COMPARISON
Rhizo (zero-copy):  0.1ms, overhead: 139 bytes
Delta (full copy):  46.9ms, overhead: 63.11 MB
Rhizo uses 453956x less storage
```

**Why it's valid**: Rhizo branches are pointers to existing table versions (~140 bytes JSON). Delta Lake has no native branching, requiring a full copy of all data files. The ratio scales with data size—larger tables = larger gap.

---

### Verify OLAP Performance

**Claim**: 0.9ms queries, 32x faster than DuckDB

```bash
python benchmarks/comprehensive_benchmark.py
```

**What it measures**: Query latency for 100K row table with warm Arrow cache (Rhizo) vs warm in-memory DuckDB.

**Expected output**:
```
RHIZO OLAP SPEEDUP ANALYSIS
Rhizo OLAP vs Standalone DuckDB:
  read           : 0.9ms vs 26.0ms = 32.2x faster
  filtered       : 0.9ms vs 1.6ms = 1.8x faster
  projection     : 0.6ms vs 1.9ms = 3.2x faster
  complex        : 2.6ms vs 3.4ms = 1.3x faster
```

**Why it's valid**: Rhizo's content-addressed Arrow cache stores decoded RecordBatches. Cache hits skip both disk I/O and Parquet decoding. DuckDB re-parses even "warm" tables. The gap widens with repeated queries on versioned/branched data (same chunks = cache hits).

---

### Run All Benchmarks

```bash
# Full benchmark suite (requires Delta Lake, DuckDB)
pip install deltalake duckdb

# Comprehensive comparison (OLAP, JOINs, branching, dedup)
python benchmarks/comprehensive_benchmark.py

# Transaction latency and convergence
python benchmarks/distributed_benchmark.py

# Energy measurement (requires codecarbon)
pip install codecarbon
python benchmarks/energy_benchmark.py

# Empirical validation against real systems
python benchmarks/real_consensus_benchmark.py
```

Results are saved to `benchmarks/*_RESULTS.json` with timestamps for reproducibility.

---

## Benchmark Methodology

This section explains the measurement conditions behind Rhizo's headline performance claims.

### Algebraic Operation Speedup

All headline speedups are measured against real systems on the same machine (`benchmarks/real_consensus_benchmark.py`):

| System | Latency | Measured? | Speedup vs Rhizo |
|--------|---------|-----------|------------------|
| Rhizo algebraic (ADD) | 0.001ms | Yes | baseline |
| SQLite WAL (NORMAL sync) | 0.045ms | Yes | **41x** slower |
| Localhost 2PC (3 nodes, TCP) | 0.065ms | Yes | **59x** slower |
| SQLite WAL (FULL sync / fsync) | 0.386ms | Yes | **355x** slower |

The localhost 2PC benchmark runs a real two-phase commit protocol between 3 OS processes communicating over TCP sockets. It measures the actual cost of coordination: socket round-trips, process context switches, and protocol overhead.

**Why Rhizo is faster**: Algebraic operations (semilattices like MAX/MIN/UNION, abelian groups like ADD) are mathematically proven to converge without coordination. They commit locally with zero network or protocol overhead.

See [TECHNICAL_FOUNDATIONS.md](TECHNICAL_FOUNDATIONS.md#algebraic-classification-for-conflict-free-merge) for the mathematical proofs.

**Cross-region projection**: Real consensus systems add 50-150ms of network RTT on top of the measured protocol overhead. Adding typical RTT to the measured 2PC overhead gives projected speedups of 46,000-138,000x. The previously cited "33,000x" figure (from `benchmarks/distributed_benchmark.py`) used a hardcoded 100ms baseline without measured protocol overhead; the actual projected speedup at 100ms RTT is ~92,000x.

### OLAP Cache Performance

The OLAP speedup (32x vs DuckDB) compares:
- **Rhizo OLAP**: DataFusion with content-addressed Arrow cache (warm)
- **DuckDB**: In-memory table

**Why Rhizo is faster even when both are "warm"**:
1. **Zero conversion**: Arrow tables stay in Arrow format throughout
2. **Content-addressed sharing**: Same data = same hash = shared cache across versions/branches
3. **No invalidation needed**: Immutable chunks guarantee cache correctness mathematically

**Measured results (100K rows)**:

| Scenario | DuckDB | Rhizo OLAP | Speedup |
|----------|--------|------------|---------|
| Cold vs Cold | 25ms | 10ms | 2.6x |
| Warm vs Warm | 23ms | 1.2ms | **19x** |
| Full scan (warm) | 45ms | 0.6ms | **75x** |

The 32x headline number represents typical warm-cache performance. Cold-cache performance shows a more modest 2-3x advantage.

### Running Benchmarks

To reproduce these results:

```bash
# Empirical validation against real systems
python benchmarks/real_consensus_benchmark.py

# Comprehensive OLAP comparison
python benchmarks/comprehensive_benchmark.py

# Energy measurements (requires codecarbon)
python benchmarks/energy_benchmark.py
```

---

## Configuration

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `RHIZO_VERIFY_INTEGRITY` | `true` | Enable chunk hash verification on read. Set to `false` for faster reads in trusted environments. |
| `RHIZO_LOG_LEVEL` | `WARNING` | Logging level: `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` |

### Integrity Verification

By default, Rhizo verifies chunk integrity on every read using BLAKE3 hashing. This provides:
- **Corruption detection**: Any storage corruption is caught immediately
- **Tamper evidence**: Modifications break the hash→content link

**Mathematical foundation**: BLAKE3 collision probability is 4.3×10⁻⁴⁸ at exabyte scale, which is 10³⁵× less likely than undetected RAM bit flips. The only practical risk is storage corruption, not hash collision.

**Performance impact**: ~70% read throughput (verification requires rehashing all data).

To disable for performance in trusted environments:
```bash
export RHIZO_VERIFY_INTEGRITY=false
```

Or per-instance:
```python
db = rhizo.open("./mydata", verify_integrity=False)
```

### Logging

Rhizo uses Python's standard logging module with structured output. Default level is `WARNING` (quiet).

**Enable debug logging**:
```bash
export RHIZO_LOG_LEVEL=DEBUG
python your_script.py
```

**Log format**: `%(asctime)s [%(levelname)s] %(name)s: %(message)s`

**What gets logged**:
- `WARNING`: OLAP initialization failures (DuckDB fallback), subscriber errors
- `DEBUG`: OLAP query fallbacks, table registration/deregistration

---

## Limits & Safety

Rhizo includes configurable limits to prevent resource exhaustion attacks and ensure predictable memory usage.

### Python Writer Limits

| Limit | Default | Description |
|-------|---------|-------------|
| `max_table_size_bytes` | 10 GB | Maximum Arrow table size before write |
| `max_columns` | 1,000 | Maximum column count |

**Mathematical basis**: 10GB table with ~2x Arrow/Parquet encoding overhead = 20-30GB peak RAM usage, suitable for typical server configurations (16-64GB RAM).

Override limits per-instance:
```python
writer = TableWriter(
    store, catalog,
    max_table_size_bytes=50 * 1024 * 1024 * 1024,  # 50 GB
    max_columns=5000
)
```

### Rust Decoder Limits

| Limit | Value | Description |
|-------|-------|-------------|
| `MAX_DECODE_SIZE` | 100 GB | Maximum Parquet file size |
| `MAX_BATCH_SIZE` | 1M rows | Maximum rows per decode batch |

These limits are compile-time constants in `rhizo_core`. They prevent:
- OOM from maliciously crafted oversized files
- Integer overflow in row count arithmetic
- Excessive per-batch memory usage

### Industry Comparison

| System | Default File Limit | Row Group Size |
|--------|-------------------|----------------|
| Delta Lake | Unlimited | 128 MB |
| Apache Iceberg | Unlimited | 128 MB |
| Apache Parquet | Unlimited | 256 MB |
| **Rhizo** | 10 GB (configurable) | 64 MB |

Rhizo's explicit limits provide defense-in-depth that other systems leave to external infrastructure.

### Exception Types

Custom exceptions provide type-safe error handling:

```python
from rhizo.exceptions import TableNotFoundError, SizeLimitExceededError

try:
    db.read("nonexistent")
except TableNotFoundError as e:
    print(f"Table not found: {e.table_name}")

try:
    writer.write("huge_table", oversized_df)
except SizeLimitExceededError as e:
    print(f"Exceeded {e.maximum:,} bytes limit")
```

All custom exceptions inherit from standard types (IOError, ValueError) for backwards compatibility.

---

## Security Hardening

### Error Message Sanitization

All error messages from Rust to Python are sanitized to prevent information leakage:

```python
# What the user sees (sanitized):
# "File not found: <path>/chunk_abc123.parquet"

# What is NOT exposed:
# "File not found: C:\Users\admin\data\rhizo\chunks\chunk_abc123.parquet"
```

Sanitization covers:
- Windows absolute paths (`C:\...`, `D:\...`)
- Unix absolute paths (`/home/...`, `/var/...`)
- All 15+ error conversion functions in PyO3 bindings

### SQL Table Extraction

The query engine extracts table names from SQL for automatic version resolution. This extraction is hardened against:

| Attack Vector | Protection |
|---------------|------------|
| Quoted identifiers | Properly parsed: `"my_table"`, `` `my_table` `` |
| Schema-qualified | Handled: `schema.table` extracts `table` |
| String literals | Removed before extraction |
| SQL keywords | 80+ keywords excluded |
| CTEs/Subqueries | Properly handled |

### Security Test Coverage

The `tests/test_security.py` suite includes 112 tests:

```python
# Parametrized fuzzing with 57+ malicious inputs
@pytest.mark.parametrize("malicious_name", [
    "../etc/passwd",
    "'; DROP TABLE users; --",
    "\x00",
    "a" * 10000,
    # ... 53 more
])
def test_table_name_validation_rejects_malicious(malicious_name):
    with pytest.raises(ValueError):
        _validate_table_name(malicious_name)
```

Categories tested:
- Path traversal (Unix and Windows)
- SQL injection attempts
- Null byte injection
- Buffer overflow (oversized inputs)
- Unicode normalization attacks
- Shell command injection
- Size limit enforcement
