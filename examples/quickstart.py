#!/usr/bin/env python3
"""
Rhizo Quickstart - Get started in 2 minutes.

This example shows the basics:
1. Write a DataFrame
2. Read it back
3. Query with SQL
4. Time travel (bonus!)

Run:
    python examples/quickstart.py
"""

import os
import sys
import tempfile

# Add python package to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'python'))

import pandas as pd
import _rhizo
from rhizo import QueryEngine


def main():
    print(r"""
  ____  _     _
 |  _ \| |__ (_)_______
 | |_) | '_ \| |_  / _ \
 |  _ <| | | | |/ / (_) |
 |_| \_\_| |_|_/___\___/

    Quickstart
    """)

    # Create temporary storage
    temp_dir = tempfile.mkdtemp(prefix="rhizo_quickstart_")
    print(f"Storage: {temp_dir}\n")

    # Initialize Rhizo
    store = _rhizo.PyChunkStore(os.path.join(temp_dir, "chunks"))
    catalog = _rhizo.PyCatalog(os.path.join(temp_dir, "catalog"))
    engine = QueryEngine(store, catalog)

    # =================================================================
    # 1. WRITE DATA
    # =================================================================
    print("1. Writing data...")

    users = pd.DataFrame({
        "id": [1, 2, 3],
        "name": ["Alice", "Bob", "Charlie"],
        "score": [85, 92, 78],
    })

    engine.write_table("users", users)
    print(f"   Wrote {len(users)} rows to 'users' table\n")

    # =================================================================
    # 2. READ DATA
    # =================================================================
    print("2. Reading data...")

    result = engine.query("SELECT * FROM users")
    print(result.to_pandas())
    print()

    # =================================================================
    # 3. SQL QUERIES
    # =================================================================
    print("3. SQL query...")

    result = engine.query("SELECT name, score FROM users WHERE score > 80")
    print(result.to_pandas())
    print()

    # =================================================================
    # 4. TIME TRAVEL (automatic versioning!)
    # =================================================================
    print("4. Time travel...")

    # Update the data (creates version 2)
    users_v2 = pd.DataFrame({
        "id": [1, 2, 3, 4],
        "name": ["Alice", "Bob", "Charlie", "Diana"],
        "score": [90, 92, 85, 95],  # Alice and Charlie improved!
    })
    engine.write_table("users", users_v2)
    print("   Updated table (version 2)\n")

    # Query current version
    print("   Current data:")
    current = engine.query("SELECT * FROM users")
    print(current.to_pandas())
    print()

    # Query previous version
    print("   Previous version (time travel):")
    previous = engine.query("SELECT * FROM users", versions={"users": 1})
    print(previous.to_pandas())
    print()

    # =================================================================
    # DONE!
    # =================================================================
    print("=" * 50)
    print("That's it! You've learned:")
    print("  - Writing DataFrames to Rhizo")
    print("  - Reading tables back")
    print("  - SQL queries")
    print("  - Time travel to previous versions")
    print()
    print("Next steps:")
    print("  - changelog_demo.py      - CDC and streaming")
    print("  - zero_copy_branching_demo.py - Git-like branches")
    print("  - cross_table_transaction_demo.py - Multi-table ACID")
    print("=" * 50)


if __name__ == "__main__":
    main()
