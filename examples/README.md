# Examples

Interactive demos showcasing Rhizo's unique capabilities.

## Getting Started

Start here if you're new to Rhizo:

```bash
python examples/quickstart.py
```

This 2-minute demo covers: write, read, SQL queries, and time travel.

## Feature Demos

Each demo is self-contained and interactive. Run any of them directly:

| Demo | What You'll Learn |
|------|-------------------|
| `quickstart.py` | Basic operations - write, read, SQL, time travel |
| `time_travel_demo.py` | Query any historical version instantly |
| `zero_copy_branching_demo.py` | Git-like branches with zero storage overhead |
| `cross_table_transaction_demo.py` | Atomic commits across multiple tables |
| `changelog_demo.py` | CDC, streaming, and incremental processing |
| `corruption_proof_demo.py` | Tamper-evident storage with BLAKE3 |
| `unified_platform_demo.py` | Full platform tour - replace your data stack |

## Running Demos

```bash
# From repo root
python examples/quickstart.py
python examples/time_travel_demo.py
python examples/zero_copy_branching_demo.py
# etc.
```

## What Makes Rhizo Different

These demos highlight capabilities that Delta Lake, Iceberg, and Hudi don't have:

- **Zero-copy branching** - Create data branches instantly, no duplication
- **Cross-table ACID** - Atomic transactions spanning multiple tables
- **Built-in CDC** - Query what changed without external systems
- **Content-addressable storage** - Automatic deduplication + integrity verification
- **O(1) time travel** - Direct version lookup, no log replay
