#!/usr/bin/env python3
"""
Armillaria Demo: Cross-Table ACID Transactions

This demo shows something Delta Lake, Iceberg, and Hudi CANNOT do:
atomic transactions across multiple tables.

The Problem:
    With Delta Lake/Iceberg, you can only commit to ONE table at a time.
    To update customers, orders, and audit_log atomically, you need:
    - External coordination (Spark transactions)
    - Manual saga patterns
    - Or just hope nothing fails mid-way

The Armillaria Solution:
    True cross-table ACID. All tables commit together, or all rollback.
    No external infrastructure. No hope required.

Run: python examples/cross_table_transaction_demo.py
"""

import os
import sys
import tempfile
import shutil

import pandas as pd

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


def print_table(name: str, engine: QueryEngine) -> None:
    """Print table contents."""
    try:
        result = engine.query(f"SELECT * FROM {name}")
        df = result.to_pandas()
        print(f"\n{name}:")
        print(df.to_string(index=False))
    except Exception:
        print(f"\n{name}: (not found or empty)")


def wait_for_input(prompt: str = "Press Enter to continue...") -> None:
    """Wait for user input, skip if non-interactive."""
    try:
        input(prompt)
    except EOFError:
        pass  # Non-interactive mode


def main():
    print(r"""
     _                _ _ _            _
    / \   _ __ _ __ _(_) | | __ _ _ __(_) __ _
   / _ \ | '__| '_ ` _ \ | | / _` | '__| |/ _` |
  / ___ \| |  | | | | | | | | (_| | |  | | (_| |
 /_/   \_\_|  |_| |_| |_|_|_|\__,_|_|  |_|\__,_|

    Cross-Table ACID Transactions Demo
    "What Delta Lake and Iceberg can't do."
    """)

    # Create temporary storage
    base_dir = tempfile.mkdtemp(prefix="rhizo_tx_demo_")
    print(f"Storage: {base_dir}")

    try:
        # Initialize Armillaria
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
            transaction_manager=tx_manager
        )

        # ============================================================
        print_header("SETUP: Initial Data")
        # ============================================================

        # Create initial customers
        customers = pd.DataFrame({
            "id": [1, 2, 3],
            "name": ["Alice", "Bob", "Charlie"],
            "balance": [1000.00, 500.00, 750.00]
        })
        engine.write_table("customers", customers)

        # Create empty orders table
        orders = pd.DataFrame({
            "id": [1],
            "customer_id": [1],
            "amount": [0.00],
            "status": ["placeholder"]
        }).iloc[0:0]  # Empty with schema

        # Actually, let's start with one order
        orders = pd.DataFrame({
            "id": [1],
            "customer_id": [1],
            "amount": [50.00],
            "status": ["completed"]
        })
        engine.write_table("orders", orders)

        # Create audit log
        audit = pd.DataFrame({
            "id": [1],
            "action": ["system_init"],
            "details": ["Initial setup"],
            "timestamp": ["2024-01-01 00:00:00"]
        })
        engine.write_table("audit_log", audit)

        print("\nInitial state:")
        print_table("customers", engine)
        print_table("orders", engine)
        print_table("audit_log", engine)

        # ============================================================
        print_header("DEMO 1: Successful Cross-Table Transaction")
        # ============================================================

        print("""
Scenario: Alice (id=1) places an order for $200.
We need to atomically:
  1. Debit Alice's balance
  2. Create the order record
  3. Log the action for compliance

In Delta Lake/Iceberg, these would be 3 separate commits.
If step 2 fails, step 1 already committed. Data inconsistency!
        """)

        wait_for_input("Press Enter to execute the transaction...")

        with engine.transaction() as tx:
            print("\n[Transaction started]")

            # 1. Debit customer balance
            updated_customers = pd.DataFrame({
                "id": [1, 2, 3],
                "name": ["Alice", "Bob", "Charlie"],
                "balance": [800.00, 500.00, 750.00]  # Alice: 1000 -> 800
            })
            tx.write_table("customers", updated_customers)
            print("  - Debited Alice's balance: $1000 -> $800")

            # 2. Create order
            new_orders = pd.DataFrame({
                "id": [1, 2],
                "customer_id": [1, 1],
                "amount": [50.00, 200.00],
                "status": ["completed", "pending"]
            })
            tx.write_table("orders", new_orders)
            print("  - Created order #2 for $200")

            # 3. Audit log
            new_audit = pd.DataFrame({
                "id": [1, 2],
                "action": ["system_init", "order_placed"],
                "details": ["Initial setup", "Alice placed order #2 for $200"],
                "timestamp": ["2024-01-01 00:00:00", "2024-01-15 10:30:00"]
            })
            tx.write_table("audit_log", new_audit)
            print("  - Logged action to audit trail")

            # Read-your-writes: can see uncommitted data within transaction
            count = tx.query("SELECT COUNT(*) as cnt FROM orders")
            print(f"  - Orders in transaction (read-your-writes): {count.to_pandas()['cnt'].iloc[0]}")

            print("\n[Transaction committing...]")

        print("[Transaction COMMITTED - all 3 tables updated atomically!]")

        print("\nState after successful transaction:")
        print_table("customers", engine)
        print_table("orders", engine)
        print_table("audit_log", engine)

        # ============================================================
        print_header("DEMO 2: Failed Transaction (Automatic Rollback)")
        # ============================================================

        print("""
Scenario: Bob (id=2) tries to place an order for $999.
But something goes wrong mid-transaction!

In Delta Lake/Iceberg: Partial commit disaster.
In Armillaria: Everything rolls back. Data stays consistent.
        """)

        wait_for_input("Press Enter to execute the failing transaction...")

        try:
            with engine.transaction() as tx:
                print("\n[Transaction started]")

                # 1. Debit Bob's balance (would overdraft, but we do it anyway for demo)
                updated_customers = pd.DataFrame({
                    "id": [1, 2, 3],
                    "name": ["Alice", "Bob", "Charlie"],
                    "balance": [800.00, -499.00, 750.00]  # Bob would be negative!
                })
                tx.write_table("customers", updated_customers)
                print("  - Debited Bob's balance: $500 -> -$499 (overdraft!)")

                # 2. Create order
                new_orders = pd.DataFrame({
                    "id": [1, 2, 3],
                    "customer_id": [1, 1, 2],
                    "amount": [50.00, 200.00, 999.00],
                    "status": ["completed", "pending", "pending"]
                })
                tx.write_table("orders", new_orders)
                print("  - Created order #3 for $999")

                # 3. Simulate failure! (business logic check)
                print("  - Checking business rules...")

                # Check for overdraft
                balance = tx.query("SELECT balance FROM customers WHERE id = 2")
                bob_balance = balance.to_pandas()['balance'].iloc[0]

                if bob_balance < 0:
                    print(f"  - ERROR: Bob's balance would be ${bob_balance}")
                    raise ValueError("Insufficient funds! Cannot overdraft.")

                # This won't execute
                tx.write_table("audit_log", pd.DataFrame({"should": ["not happen"]}))

        except ValueError as e:
            print(f"\n[Transaction ROLLED BACK: {e}]")

        print("\nState after failed transaction (unchanged!):")
        print_table("customers", engine)
        print_table("orders", engine)
        print_table("audit_log", engine)

        # Verify Bob's balance is still $500
        result = engine.query("SELECT balance FROM customers WHERE id = 2")
        bob_balance = result.to_pandas()['balance'].iloc[0]
        print(f"\nBob's balance: ${bob_balance} (unchanged - rollback worked!)")

        # ============================================================
        print_header("DEMO 3: The Delta Lake / Iceberg Comparison")
        # ============================================================

        print("""
What you'd have to do WITHOUT Armillaria:

Delta Lake / Iceberg (single-table transactions):
```python
# Three separate commits - NOT atomic!
spark.sql("UPDATE customers SET balance = balance - 200 WHERE id = 1")
# ^ Committed! If next line fails, money is gone but no order exists

spark.sql("INSERT INTO orders VALUES (2, 1, 200, 'pending')")
# ^ Committed! If next line fails, no audit trail

spark.sql("INSERT INTO audit_log VALUES ('order_placed', ...)")
# ^ Finally committed... hopefully
```

With external coordination (complex!):
```python
# Option 1: Saga pattern (manual compensation)
try:
    update_customer()
    try:
        create_order()
        try:
            write_audit()
        except:
            delete_order()  # Compensate
            raise
    except:
        restore_customer_balance()  # Compensate
        raise
except:
    # Hope you got the compensation right!
```

Armillaria (built-in cross-table ACID):
```python
with engine.transaction() as tx:
    tx.write_table("customers", updated)
    tx.write_table("orders", new_order)
    tx.write_table("audit_log", entry)
    # All commit together, or all rollback. Done.
```
        """)

        # ============================================================
        print_header("SUMMARY")
        # ============================================================

        print("""
What we demonstrated:

1. ATOMIC MULTI-TABLE COMMITS
   - Updated 3 tables in one transaction
   - All committed together

2. AUTOMATIC ROLLBACK
   - When an error occurred, ALL changes were rolled back
   - Data stayed consistent

3. READ-YOUR-WRITES
   - Within a transaction, you see your uncommitted changes
   - Enables business logic checks before commit

4. NO EXTERNAL INFRASTRUCTURE
   - No Kafka, no Spark transactions, no saga patterns
   - Just Python and Armillaria

This is what Delta Lake and Iceberg cannot do.
This is why Armillaria exists.
        """)

    finally:
        # Cleanup
        shutil.rmtree(base_dir, ignore_errors=True)
        print(f"\nCleaned up: {base_dir}")


if __name__ == "__main__":
    main()
