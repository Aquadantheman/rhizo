# UDR Killer Application Demos

## Strategic Goal

Demonstrate capabilities that **Delta Lake, Iceberg, and Hudi cannot do** (or require significant external infrastructure to achieve). These demos should make data engineers say: "Wait, I can't do that with my current stack."

---

## Demo 1: Cross-Table ACID Transactions

**The Problem Delta/Iceberg Can't Solve:**
Delta Lake and Iceberg only support single-table transactions. To update `customers`, `orders`, and `audit_log` atomically, you need external coordination (Spark transactions, manual saga patterns, or hope).

**UDR Demo:**
```python
# Atomic update across 3 tables - impossible in Delta Lake
with engine.transaction() as tx:
    # Debit customer balance
    tx.write_table("customers", updated_customers)

    # Create order
    tx.write_table("orders", new_order)

    # Log for compliance
    tx.write_table("audit_log", audit_entry)

    # ALL THREE commit atomically, or ALL THREE rollback
```

**Key Message:** "UDR gives you database-grade ACID across your entire data lake."

**Script:** `examples/cross_table_transaction_demo.py`

---

## Demo 2: Zero-Copy Data Branching

**The Problem Delta/Iceberg Can't Solve:**
To experiment with data transformations safely, you currently:
1. Copy the entire dataset (expensive, slow)
2. Run experiments on a sample (not representative)
3. YOLO on production (risky)

**UDR Demo:**
```python
# Create branch - INSTANT, zero storage cost
engine.create_branch("experiment/new-scoring-model")

# Measure storage before
storage_before = get_directory_size("./data")

# Run experimental transformations on branch
engine.checkout("experiment/new-scoring-model")
engine.write_table("scores", experimental_scores)  # 100MB of changes

# Compare storage
storage_after = get_directory_size("./data")
print(f"Storage increase: {storage_after - storage_before} bytes")
# Output: ~100MB (only the diff, not a full copy!)

# Compare results between branches
main_result = engine.query("SELECT AVG(score) FROM scores", branch="main")
exp_result = engine.query("SELECT AVG(score) FROM scores", branch="experiment/new-scoring-model")

# Happy? Merge. Not happy? Delete branch.
engine.merge_branch("experiment/new-scoring-model", into="main")
```

**Key Message:** "Experiment on production data without copying it. Git for your data."

**Script:** `examples/zero_copy_branching_demo.py`

---

## Demo 3: Stream Processing Without Kafka

**The Problem Delta/Iceberg Can't Solve:**
To process changes incrementally, you need:
- Kafka (or similar) for change capture
- Debezium for CDC
- Flink/Spark Structured Streaming for processing
- Separate batch and stream codepaths

**UDR Demo:**
```python
# BATCH: What is the current state?
result = engine.query("SELECT * FROM orders WHERE status = 'pending'")

# STREAM: What changed since my last check?
changes = engine.get_changes(since_tx_id=last_checkpoint)

# CONTINUOUS: Process changes as they happen
for event in engine.subscribe(tables=["orders"]):
    process_order_change(event)

# Same data. Same API. No Kafka.
```

**Incremental ETL Pattern:**
```python
# Run every hour via cron - only processes NEW changes
checkpoint = load_checkpoint()  # e.g., tx_id=42

changes = engine.get_changes(since_tx_id=checkpoint)
for entry in changes:
    process_changes(entry)

save_checkpoint(changes[-1]['tx_id'])  # e.g., tx_id=47
```

**Key Message:** "Unified batch and stream. One API. No Kafka required."

**Script:** `examples/changelog_demo.py` (already exists!)

---

## Demo 4: Corruption-Proof Storage

**The Problem Delta/Iceberg Can't Solve:**
Parquet files can be corrupted silently. Delta/Iceberg checksums are per-file, not content-addressed. You might read corrupted data without knowing.

**UDR Demo:**
```python
# Store data - automatically hashed with BLAKE3
engine.write_table("critical_data", df)

# Later: verify integrity
try:
    data = store.get_verified(chunk_hash)  # Cryptographic verification
    print("Data integrity verified!")
except HashMismatchError as e:
    print(f"CORRUPTION DETECTED: expected {e.expected}, got {e.actual}")

# Bonus: automatic deduplication
# Write same data twice → stored once
hash1 = store.put(data)
hash2 = store.put(data)  # Same content
assert hash1 == hash2  # Same hash, no duplicate storage
```

**Key Message:** "Every read is verified. Corruption is impossible to hide."

**Script:** `examples/integrity_demo.py`

---

## Demo 5: Time Travel Debugging

**The Problem (Delta/Iceberg can do this, but UDR is cleaner):**
Delta/Iceberg have time travel, but UDR's version-centric model is more intuitive.

**UDR Demo:**
```python
# Query any historical version
result_v1 = engine.query("SELECT * FROM users", versions={"users": 1})
result_v5 = engine.query("SELECT * FROM users", versions={"users": 5})

# Diff between versions - find exactly what changed
diff = engine.diff_versions("users", 1, 5, key_columns=["user_id"])
print(f"Rows added: {diff['rows_added']}")
print(f"Rows removed: {diff['rows_removed']}")
print(f"Rows modified: {diff['rows_modified']}")

# Find when a bug was introduced
for version in range(1, 10):
    result = engine.query(
        "SELECT COUNT(*) FROM users WHERE balance < 0",
        versions={"users": version}
    )
    if result.to_pandas()['count'].iloc[0] > 0:
        print(f"Bug introduced in version {version}")
        break
```

**Key Message:** "Debug data issues by querying any point in history."

**Script:** `examples/time_travel_demo.py` (enhance existing)

---

## Demo 6: The "Replace Everything" Demo

**The Grand Vision Demo:**
Show a complete workflow that would normally require PostgreSQL + Kafka + Spark + S3:

```python
import udr
from udr_query import QueryEngine

# One system for everything
engine = QueryEngine(...)

# TRANSACTIONAL: ACID writes like a database
with engine.transaction() as tx:
    tx.write_table("customers", customer_df)
    tx.write_table("orders", order_df)

# ANALYTICAL: SQL queries like a data warehouse
result = engine.query("""
    SELECT c.name, SUM(o.amount) as total
    FROM customers c
    JOIN orders o ON c.id = o.customer_id
    GROUP BY c.name
    ORDER BY total DESC
""")

# STREAMING: Change notifications like Kafka
for event in engine.subscribe(tables=["orders"]):
    send_notification(event)

# VERSIONED: Branches like Git
engine.create_branch("feature/new-pricing")
engine.checkout("feature/new-pricing")
# ... experiment ...
engine.merge_branch("feature/new-pricing", into="main")

# TIME TRAVEL: Query history like a time machine
historical = engine.query("SELECT * FROM orders", versions={"orders": 1})
```

**Key Message:** "One system. Transactional + Analytical + Streaming + Versioned."

**Script:** `examples/unified_demo.py`

---

## Priority Order

1. **Demo 1: Cross-Table Transactions** - Biggest differentiator from Delta/Iceberg
2. **Demo 3: Stream Without Kafka** - Eliminates infrastructure complexity
3. **Demo 2: Zero-Copy Branching** - Unique to UDR
4. **Demo 6: Replace Everything** - The vision pitch
5. **Demo 4: Corruption-Proof** - Trust and safety angle
6. **Demo 5: Time Travel** - Useful but not unique

---

## Demo Development Status

| Demo | Script | Status |
|------|--------|--------|
| Cross-Table Transactions | `cross_table_transaction_demo.py` | To create |
| Zero-Copy Branching | `zero_copy_branching_demo.py` | To create |
| Stream Without Kafka | `changelog_demo.py` | Exists! |
| Corruption-Proof Storage | `integrity_demo.py` | To create |
| Time Travel Debugging | `time_travel_demo.py` | Exists, enhance |
| Replace Everything | `unified_demo.py` | To create |

---

## Target Audience

1. **Data Engineers** currently using Delta Lake/Iceberg
   - Pain: Managing multiple systems, no cross-table transactions
   - Hook: "ACID across your entire data lake"

2. **Platform Teams** building internal data infrastructure
   - Pain: Operating Kafka, managing CDC pipelines
   - Hook: "Streaming without the infrastructure"

3. **ML Engineers** doing feature engineering
   - Pain: Can't safely experiment on production data
   - Hook: "Git-style branches for your feature store"

4. **Startups** building data products
   - Pain: Too many systems to operate
   - Hook: "One system instead of five"

---

*Created: January 2026*
