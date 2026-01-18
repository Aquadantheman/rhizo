#!/usr/bin/env python3
"""
Armillaria Demo: One Platform to Replace Them All

This demo shows how Armillaria unifies capabilities that typically require
multiple separate systems stacked together.

The Traditional Stack:
    - Delta Lake/Iceberg/Hudi: Versioned tables (single-table only)
    - lakeFS: Git-like branching (bolted on top)
    - Apache Spark: Query engine (heavy infrastructure)
    - External checksums: Data integrity (manual process)
    - Saga patterns: Cross-table transactions (DIY or prayer)

The Armillaria Stack:
    - Armillaria: All of the above. In one library. No infrastructure.

Run: python examples/unified_platform_demo.py
"""

import os
import sys
import tempfile
import shutil
import time

import pandas as pd
import numpy as np

# Add python directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'python'))

import rhizo
import _rhizo
from rhizo import QueryEngine


def print_header(text: str) -> None:
    """Print a section header."""
    print(f"\n{'='*60}")
    print(f"  {text}")
    print('='*60)


def print_check(text: str) -> None:
    """Print a checkmark item."""
    print(f"  [+] {text}")


def print_cross(text: str) -> None:
    """Print a cross item."""
    print(f"  [-] {text}")


def wait_for_input(prompt: str = "Press Enter to continue...") -> None:
    """Wait for user input, skip if non-interactive."""
    try:
        input(prompt)
    except EOFError:
        pass


def format_bytes(size_bytes: int) -> str:
    """Format bytes as human-readable string."""
    size = float(size_bytes)
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def get_directory_size(path: str) -> int:
    """Get total size of directory in bytes."""
    total = 0
    for dirpath, dirnames, filenames in os.walk(path):
        for f in filenames:
            fp = os.path.join(dirpath, f)
            total += os.path.getsize(fp)
    return total


def main():
    print(r"""
     _                _ _ _            _
    / \   _ __ _ __ _(_) | | __ _ _ __(_) __ _
   / _ \ | '__| '_ ` _ \ | | / _` | '__| |/ _` |
  / ___ \| |  | | | | | | | | (_| | |  | | (_| |
 /_/   \_\_|  |_| |_| |_|_|_|\__,_|_|  |_|\__,_|

    Unified Data Platform Demo
    "Replace the whole stack. Keep the simplicity."
    """)

    # Create temporary storage
    base_dir = tempfile.mkdtemp(prefix="rhizo_unified_demo_")
    print(f"Storage: {base_dir}")

    try:
        # Initialize Armillaria with ALL features
        store = _rhizo.PyChunkStore(os.path.join(base_dir, "chunks"))
        catalog = _rhizo.PyCatalog(os.path.join(base_dir, "catalog"))
        branches = _rhizo.PyBranchManager(os.path.join(base_dir, "branches"))
        tx_manager = _rhizo.PyTransactionManager(
            os.path.join(base_dir, "transactions"),
            os.path.join(base_dir, "catalog"),
            os.path.join(base_dir, "branches")
        )

        engine = QueryEngine(
            store, catalog,
            branch_manager=branches,
            transaction_manager=tx_manager,
            verify_integrity=True  # Built-in integrity checking
        )

        # ============================================================
        print_header("THE TRADITIONAL DATA STACK")
        # ============================================================

        print("""
What you typically need for modern data engineering:

1. TABLE VERSIONING (Delta Lake / Iceberg / Hudi)
   - Time travel, schema evolution, ACID... for ONE table
   - Cross-table atomicity? Not included.
   - Requires: Spark cluster, cloud object store

2. DATA BRANCHING (lakeFS)
   - Git-like branching for data lakes
   - Separate system bolted on top
   - Requires: lakeFS server, additional metadata store

3. QUERY ENGINE (Apache Spark / Trino / Presto)
   - Distributed query processing
   - Requires: Cluster management, resource allocation

4. DATA INTEGRITY (External checksums / HDFS)
   - Manual checksum generation and verification
   - Hope you remember to check them

5. CROSS-TABLE TRANSACTIONS (DIY)
   - Saga pattern implementations
   - Manual compensation logic
   - Prayer-based error handling

Total infrastructure: 5+ systems, multiple servers, complex ops
        """)

        wait_for_input("Press Enter to see the Armillaria alternative...")

        # ============================================================
        print_header("THE ARMILLARIA ALTERNATIVE")
        # ============================================================

        print("""
Armillaria: A single library that includes:
        """)
        print_check("Table versioning with time travel")
        print_check("Zero-copy branching (like Git)")
        print_check("Cross-table ACID transactions")
        print_check("Content-addressable storage with integrity verification")
        print_check("SQL query interface")
        print_check("Python-native API")
        print()
        print("Required infrastructure: pip install rhizo")
        print("Cluster needed: No")
        print("External services: None")

        # ============================================================
        print_header("LIVE DEMO: All Features Working Together")
        # ============================================================

        print("\n--- Creating an e-commerce data model ---\n")

        # Initial data
        customers = pd.DataFrame({
            "id": [1, 2, 3],
            "name": ["Alice", "Bob", "Charlie"],
            "balance": [1000.0, 750.0, 500.0],
            "tier": ["gold", "silver", "bronze"]
        })

        products = pd.DataFrame({
            "id": [101, 102, 103],
            "name": ["Widget", "Gadget", "Gizmo"],
            "price": [99.99, 149.99, 49.99],
            "stock": [100, 50, 200]
        })

        orders = pd.DataFrame({
            "id": [1001],
            "customer_id": [1],
            "product_id": [101],
            "quantity": [2],
            "total": [199.98],
            "status": ["completed"]
        })

        # Write initial data (versioned automatically)
        engine.write_table("customers", customers)
        engine.write_table("products", products)
        engine.write_table("orders", orders)

        print("Created tables: customers, products, orders")
        print(f"Storage used: {format_bytes(get_directory_size(base_dir))}")

        # ============================================================
        print_header("FEATURE 1: Cross-Table ACID Transaction")
        # ============================================================

        print("""
Scenario: Bob buys 3 Gadgets ($449.97 total)

This requires atomic updates to:
- customers (debit balance)
- products (reduce stock)
- orders (create record)

Delta Lake/Iceberg: 3 separate commits. Hope nothing fails!
Armillaria: One transaction. All or nothing.
        """)

        wait_for_input("Press Enter to execute the transaction...")

        with engine.transaction() as tx:
            # 1. Debit customer
            updated_customers = pd.DataFrame({
                "id": [1, 2, 3],
                "name": ["Alice", "Bob", "Charlie"],
                "balance": [1000.0, 300.03, 500.0],  # Bob: 750 - 449.97
                "tier": ["gold", "silver", "bronze"]
            })
            tx.write_table("customers", updated_customers)

            # 2. Update inventory
            updated_products = pd.DataFrame({
                "id": [101, 102, 103],
                "name": ["Widget", "Gadget", "Gizmo"],
                "price": [99.99, 149.99, 49.99],
                "stock": [100, 47, 200]  # Gadget: 50 - 3
            })
            tx.write_table("products", updated_products)

            # 3. Create order
            new_orders = pd.DataFrame({
                "id": [1001, 1002],
                "customer_id": [1, 2],
                "product_id": [101, 102],
                "quantity": [2, 3],
                "total": [199.98, 449.97],
                "status": ["completed", "completed"]
            })
            tx.write_table("orders", new_orders)

        print("\nTransaction committed atomically!")
        print("  - customers: Bob's balance updated")
        print("  - products: Gadget stock reduced")
        print("  - orders: New order created")

        # Verify
        result = engine.query("SELECT name, balance FROM customers WHERE id = 2")
        bob = result.to_pandas().iloc[0]
        print(f"\nVerification: Bob's balance is now ${bob['balance']:.2f}")

        # ============================================================
        print_header("FEATURE 2: Zero-Copy Branching")
        # ============================================================

        print("""
Scenario: Test a new loyalty discount algorithm

Traditional approach: Copy entire dataset, run experiment, delete copy
Armillaria approach: Branch (instant), experiment, merge or delete
        """)

        wait_for_input("Press Enter to create an experiment branch...")

        storage_before = get_directory_size(base_dir)
        start = time.time()
        engine.create_branch("experiment/loyalty-discount")
        branch_time = (time.time() - start) * 1000
        storage_after = get_directory_size(base_dir)

        print("\nBranch 'experiment/loyalty-discount' created")
        print(f"  Time: {branch_time:.2f} ms")
        print(f"  Storage overhead: {format_bytes(storage_after - storage_before)}")
        print("  (That's just a pointer - data is shared!)")

        # Make changes on experiment branch
        engine.checkout("experiment/loyalty-discount")

        # Apply 20% discount to gold tier
        discounted_products = pd.DataFrame({
            "id": [101, 102, 103],
            "name": ["Widget", "Gadget", "Gizmo"],
            "price": [79.99, 119.99, 39.99],  # 20% off
            "stock": [100, 47, 200]
        })
        engine.write_table("products", discounted_products)

        print("\nOn experiment branch: Applied 20% discount to all products")

        # Compare branches
        main_price = engine.query(
            "SELECT price FROM products WHERE id = 101",
            branch="main"
        ).to_pandas()['price'].iloc[0]

        exp_price = engine.query(
            "SELECT price FROM products WHERE id = 101",
            branch="experiment/loyalty-discount"
        ).to_pandas()['price'].iloc[0]

        print(f"\nWidget price on 'main':                ${main_price:.2f}")
        print(f"Widget price on 'experiment/loyalty':  ${exp_price:.2f}")
        print("Branches are fully isolated!")

        engine.checkout("main")

        # ============================================================
        print_header("FEATURE 3: Time Travel (Version History)")
        # ============================================================

        print("""
Scenario: Auditor needs to see data as it was before the transaction

With Armillaria, every write is versioned. Just query the past.
        """)

        wait_for_input("Press Enter to query historical data...")

        versions = engine.list_versions("customers")
        print(f"\nCustomers table versions: {versions}")

        # Query version 1 (before Bob's purchase)
        old_data = engine.query(
            "SELECT name, balance FROM customers",
            versions={"customers": 1}
        ).to_pandas()

        current_data = engine.query(
            "SELECT name, balance FROM customers"
        ).to_pandas()

        print("\nCustomers at version 1 (initial):")
        print(old_data.to_string(index=False))

        print("\nCustomers at current version:")
        print(current_data.to_string(index=False))

        # ============================================================
        print_header("FEATURE 4: Built-In Integrity Verification")
        # ============================================================

        print("""
Every chunk is named by its BLAKE3 hash.
Every read verifies the hash automatically.
Corruption is impossible to hide.

(We demonstrated this in detail in the corruption_proof_demo.py)
        """)

        print("\nIntegrity verification: ENABLED")
        print("Hash algorithm: BLAKE3")
        print("Verification: Automatic on every read")

        # ============================================================
        print_header("FEATURE 5: SQL Query Interface")
        # ============================================================

        print("""
Full SQL support with DuckDB backend.
Joins, aggregations, CTEs - it all works.
        """)

        wait_for_input("Press Enter to run an analytical query...")

        # Complex query joining multiple tables
        result = engine.query("""
            SELECT
                c.name as customer,
                c.tier,
                COUNT(o.id) as order_count,
                SUM(o.total) as total_spent
            FROM customers c
            LEFT JOIN orders o ON c.id = o.customer_id
            GROUP BY c.name, c.tier
            ORDER BY total_spent DESC
        """)

        print("\nCustomer spending analysis:")
        print(result.to_pandas().to_string(index=False))

        # ============================================================
        print_header("STORAGE EFFICIENCY SUMMARY")
        # ============================================================

        final_storage = get_directory_size(base_dir)

        print(f"""
Final storage breakdown:

  Total storage used: {format_bytes(final_storage)}

  What we stored:
  - 3 tables with multiple versions
  - 2 branches (main + experiment)
  - Transaction history
  - Full audit trail

  Content-addressable deduplication means:
  - Unchanged data is never duplicated
  - Identical rows share the same chunks
  - Branches share all unchanged data
        """)

        # ============================================================
        print_header("COMPARISON: Stack Complexity")
        # ============================================================

        print("""
TRADITIONAL APPROACH                    ARMILLARIA APPROACH
----------------------                  -------------------

Delta Lake or Iceberg                   pip install rhizo
  + Spark Cluster                       engine = QueryEngine(...)
  + lakeFS Server
  + S3/GCS/ADLS
  + Manual checksums
  + Saga implementations

Infrastructure: Complex                 Infrastructure: None
Setup time: Days                        Setup time: Seconds
Expertise needed: DataOps team          Expertise needed: Python basics
Cross-table ACID: No                    Cross-table ACID: Yes
Native branching: No (lakeFS add-on)    Native branching: Yes
Integrity checks: Manual                Integrity checks: Automatic

        """)

        # ============================================================
        print_header("CONCLUSION")
        # ============================================================

        print("""
Armillaria provides:

1. VERSIONED TABLES
   Time travel, schema tracking, audit history

2. CROSS-TABLE ACID
   True atomic transactions across tables
   (What Delta Lake and Iceberg can't do)

3. ZERO-COPY BRANCHING
   Git-like workflow for data experimentation
   (Native, not bolted on)

4. CONTENT-ADDRESSABLE STORAGE
   Automatic deduplication and integrity verification
   (Built in, not afterthought)

5. SIMPLE API
   Pure Python, no clusters, no infrastructure
   (Just pip install and go)

This is the largest organism on Earth.
Now it's for your data.

         |
        /|\\
       / | \\
      /  |  \\
     /   |   \\
    /    |    \\
   /     |     \\
  /      |      \\
---------+--------- (underground network)
        """)

    finally:
        # Cleanup
        shutil.rmtree(base_dir, ignore_errors=True)
        print(f"\nCleaned up: {base_dir}")


if __name__ == "__main__":
    main()
