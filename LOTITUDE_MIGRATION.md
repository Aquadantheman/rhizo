# Lotitude → UDR Migration Plan

## Executive Summary

Migrate Lotitude's ETL pipeline from file-based parquet storage to UDR, gaining:
- **Atomic pipeline runs** - All-or-nothing compound refreshes
- **Version history** - Query any historical state
- **Change tracking** - Know exactly what changed per parcel
- **Deduplication** - 60-80% storage savings on R2
- **Branching** - A/B test scoring algorithms safely

---

## Current vs. UDR Architecture

### Current
```
Data Sources → Compounds (77 parquets) → Master → DuckDB → API
                     ↓
              data/compounds/nyc/
                     ↓
              data/master/nyc.parquet (in-place overwrite)
                     ↓
              R2 upload (full copy every night)
```

### With UDR
```
Data Sources → UDR Compounds (versioned) → UDR Master → DuckDB → API
                     ↓
              Content-addressable chunks
                     ↓
              Atomic commits with version history
                     ↓
              R2 sync (delta only - new chunks)
```

---

## Migration Phases

### Phase A: Proof of Concept (1-2 compounds)

**Goal:** Prove UDR works with Lotitude data

**Steps:**
1. Pick 2 compounds: one small (`city_owned`, 257KB), one medium (`dob_permits`, 3MB)
2. Create UDR storage alongside existing files
3. Modify compound runner to write to both systems
4. Verify data integrity matches

**Code Change (minimal):**
```python
# In compound runner, after building parquet:
import udr
from udr_query import QueryEngine

# Initialize UDR (once per pipeline run)
store = udr.PyChunkStore("data/udr/chunks")
catalog = udr.PyCatalog("data/udr/catalog")
engine = QueryEngine(store, catalog)

# Write compound to UDR
engine.write_table(f"nyc_{compound_name}", df)  # e.g., "nyc_dob_permits"
```

**Validation:**
```python
# Compare UDR data to original parquet
original = pd.read_parquet(f"data/compounds/nyc/{compound}.parquet")
udr_data = engine.query(f"SELECT * FROM nyc_{compound}").to_pandas()
assert original.equals(udr_data)
```

**Success Criteria:**
- [ ] Both compounds write successfully
- [ ] Data matches original parquets
- [ ] Time travel works (can query version 1 after version 2 written)

---

### Phase B: Full Compound Migration

**Goal:** All 77 compounds in UDR

**Steps:**
1. Batch migrate existing parquets to UDR (one-time import)
2. Update orchestrator to write to UDR
3. Keep parquet files as backup during transition
4. Validate master build works from UDR compounds

**One-Time Import Script:**
```python
import glob
import pandas as pd
from pathlib import Path

compounds_dir = Path("data/compounds/nyc")
for parquet_file in compounds_dir.glob("*.parquet"):
    compound_name = parquet_file.stem
    df = pd.read_parquet(parquet_file)
    engine.write_table(f"nyc_{compound_name}", df)
    print(f"Imported {compound_name}: {len(df)} rows")
```

**Success Criteria:**
- [ ] All 77 compounds imported
- [ ] Deduplication achieved (measure storage savings)
- [ ] Query performance acceptable

---

### Phase C: Transactional Pipeline

**Goal:** Atomic compound refreshes with rollback

**Key Change:**
```python
# Before: Individual compound writes (no atomicity)
for compound in compounds_to_refresh:
    run_compound(compound)  # Can fail mid-way

# After: Transactional compound writes
with engine.transaction() as tx:
    for compound in compounds_to_refresh:
        df = fetch_compound_data(compound)
        tx.write_table(f"nyc_{compound}", df)
    # All compounds commit atomically
# If any fails, all rollback
```

**Phased Pipeline Transactions:**
```python
# Phase 1: Source compounds (transactional)
with engine.transaction() as tx:
    for compound in source_compounds:
        tx.write_table(f"nyc_{compound}", fetch_data(compound))

# Phase 2: Derived compounds (transactional)
with engine.transaction() as tx:
    for compound in derived_compounds:
        tx.write_table(f"nyc_{compound}", compute_derived(compound))

# Phase 3: Master build (transactional)
with engine.transaction() as tx:
    master_df = build_master_from_udr(engine)
    tx.write_table("nyc_master", master_df)
```

**Success Criteria:**
- [ ] Failed phase doesn't corrupt data
- [ ] Can rollback to previous good state
- [ ] Pipeline recovery is automatic

---

### Phase D: Change Tracking & Time Travel

**Goal:** Answer "what changed?" questions

**New Capabilities:**
```python
# Get changes since last successful run
changes = engine.get_changes(since_tx_id=last_checkpoint)
for entry in changes:
    print(f"Transaction {entry['tx_id']}:")
    for change in entry['changes']:
        print(f"  {change['table_name']}: v{change['old_version']} → v{change['new_version']}")

# Time travel: Query last week's scores
result = engine.query(
    "SELECT bbl, opportunity_score, distress_score FROM nyc_master",
    versions={"nyc_master": last_week_version}
)

# Diff: What parcels changed scores?
diff = engine.diff_versions("nyc_master", last_week_version, current_version,
                            key_columns=["bbl"])
print(f"Parcels with score changes: {len(diff['rows_modified'])}")
```

**Parcel Changelog (new feature for Lotitude):**
```python
# Track score history for individual parcels
def get_parcel_history(bbl: str, versions: list[int]) -> pd.DataFrame:
    history = []
    for v in versions:
        row = engine.query(
            f"SELECT * FROM nyc_master WHERE bbl = '{bbl}'",
            versions={"nyc_master": v}
        ).to_pandas()
        row["version"] = v
        history.append(row)
    return pd.concat(history)

# "Show me how this parcel's score evolved"
history = get_parcel_history("1000010001", versions=[1, 2, 3, 4, 5])
```

**Success Criteria:**
- [ ] Can query any historical version
- [ ] Can diff between versions
- [ ] Change log available for debugging

---

### Phase E: Branching for Algorithm Testing

**Goal:** A/B test scoring algorithms safely

**Workflow:**
```python
# Create experiment branch
engine.create_branch("experiment/new-scoring-weights")
engine.checkout("experiment/new-scoring-weights")

# Modify scoring weights and rebuild master
with engine.transaction() as tx:
    master_df = build_master_with_new_weights(engine)
    tx.write_table("nyc_master", master_df)

# Compare results
main_scores = engine.query(
    "SELECT AVG(opportunity_score) as avg_opp FROM nyc_master",
    branch="main"
).to_pandas()

exp_scores = engine.query(
    "SELECT AVG(opportunity_score) as avg_opp FROM nyc_master",
    branch="experiment/new-scoring-weights"
).to_pandas()

print(f"Main avg: {main_scores['avg_opp'].iloc[0]:.1f}")
print(f"Experiment avg: {exp_scores['avg_opp'].iloc[0]:.1f}")

# If experiment is better, merge
engine.checkout("main")
engine.merge_branch("experiment/new-scoring-weights", into="main")
```

**Success Criteria:**
- [ ] Can create branches instantly
- [ ] Experiment doesn't affect production
- [ ] Merge brings changes to production

---

### Phase F: R2 Sync Optimization

**Goal:** Delta sync instead of full upload

**Current:**
```
Every night: Upload full 500MB+ tar.gz to R2
```

**With UDR:**
```
Every night: Upload only new chunks (typically <50MB)
```

**Implementation:**
```python
# R2 becomes a chunk store backend
class R2ChunkStore:
    """Content-addressable storage backed by R2"""

    def put(self, data: bytes) -> str:
        hash = blake3(data).hexdigest()
        if not self.exists(hash):
            self.r2.put(f"chunks/{hash[:2]}/{hash}", data)
        return hash

    def get(self, hash: str) -> bytes:
        return self.r2.get(f"chunks/{hash[:2]}/{hash}")
```

**Storage Savings:**
- Current: ~500MB × 365 days = ~180GB/year
- With UDR: ~50MB delta × 365 days + dedup = ~10-20GB/year
- Estimated savings: **90%+**

---

## Integration Points

### Orchestrator Changes

```python
# etl/refresh/orchestrator.py

# Add UDR initialization
def init_udr(city: str):
    store = udr.PyChunkStore(f"data/udr/{city}/chunks")
    catalog = udr.PyCatalog(f"data/udr/{city}/catalog")
    branches = udr.PyBranchManager(f"data/udr/{city}/branches")
    tx_manager = udr.PyTransactionManager(
        f"data/udr/{city}/transactions",
        f"data/udr/{city}/catalog",
        f"data/udr/{city}/branches"
    )
    return QueryEngine(store, catalog,
                       branch_manager=branches,
                       transaction_manager=tx_manager)

# Replace file writes with UDR writes
def write_compound_output(engine, compound_name: str, df: pd.DataFrame):
    engine.write_table(f"nyc_{compound_name}", df)
```

### Metadata Integration

```python
# Current metadata (keep for compatibility):
data/metadata/nyc_refresh_state.json

# New: UDR provides automatic versioning
# No need to manually track last_fetch_utc, output_hash, etc.
# Query changelog instead:
latest_tx = engine.latest_tx_id()
changes = engine.get_changes(since_tx_id=previous_tx)
```

### API Integration

```python
# api/core/database.py

# Current: Load parquet into DuckDB
conn.execute(f"CREATE TABLE master AS SELECT * FROM '{parquet_path}'")

# With UDR: Load from UDR (still uses DuckDB under the hood)
engine = QueryEngine(store, catalog)
# QueryEngine already uses DuckDB - same query performance
result = engine.query("SELECT * FROM nyc_master WHERE ...")
```

---

## Timeline Recommendation

| Phase | Effort | Risk | Dependencies |
|-------|--------|------|--------------|
| A: POC (2 compounds) | 1-2 days | Low | None |
| B: Full migration | 2-3 days | Medium | Phase A validated |
| C: Transactions | 1-2 days | Low | Phase B complete |
| D: Change tracking | 1 day | Low | Phase C complete |
| E: Branching | 1 day | Low | Phase D complete |
| F: R2 optimization | 2-3 days | Medium | Phase B complete |

**Total: ~10-14 days of focused work**

---

## Quick Start (Phase A)

```bash
# In lotitude repo (zoniverse folder)
cd ../unifieddataruntime
pip install -e .

# Back to lotitude
cd ../zoniverse

# Create UDR storage
mkdir -p data/udr/nyc/{chunks,catalog,branches,transactions}

# Test with one compound
python -c "
import pandas as pd
import udr
from udr_query import QueryEngine

# Init UDR
store = udr.PyChunkStore('data/udr/nyc/chunks')
catalog = udr.PyCatalog('data/udr/nyc/catalog')
engine = QueryEngine(store, catalog)

# Import a compound
df = pd.read_parquet('data/compounds/nyc/city_owned.parquet')
engine.write_table('nyc_city_owned', df)
print(f'Wrote {len(df)} rows')

# Query it back
result = engine.query('SELECT COUNT(*) as cnt FROM nyc_city_owned')
print(result.to_pandas())
"
```

---

## Questions to Decide

1. **Storage location:** `data/udr/` in zoniverse repo, or separate repo?
2. **Parallel systems:** Keep parquets during transition, or switch fully?
3. **R2 strategy:** UDR for local + parquet for R2, or UDR chunks to R2?
4. **Branching workflow:** Who can create branches? Auto-cleanup policy?

---

*Created: January 2026*
