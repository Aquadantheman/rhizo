#!/usr/bin/env python3
"""
Unified Batch/Stream Demo - Showcase UDR's changelog and subscription capabilities.

This demo shows the key innovation of UDR: unified batch and stream semantics.
Instead of separate systems for "what is the data?" and "what changed?",
UDR provides both through a single, consistent interface.

The Five Patterns Demonstrated:
1. Batch Query: "What is the current state?"
2. Change Query: "What changed since X?"
3. Polling Subscriber: Non-blocking change detection
4. Background Subscriber: Event-driven processing
5. Checkpoint/Resume: Incremental processing pattern

Run:
    python examples/changelog_demo.py
"""

import os
import sys
import shutil
import tempfile
import time
import threading

# Add python package to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'python'))

import pandas as pd

# Import UDR components
import udr
from udr_query import QueryEngine, Subscriber, ChangeEvent


def print_section(title: str):
    """Print a section header."""
    print(f"\n{'='*70}")
    print(f"  {title}")
    print(f"{'='*70}\n")


def print_subsection(title: str):
    """Print a subsection header."""
    print(f"\n--- {title} ---\n")


def main():
    # Create temporary storage
    temp_dir = tempfile.mkdtemp(prefix="udr_changelog_demo_")
    chunks_dir = os.path.join(temp_dir, "chunks")
    catalog_dir = os.path.join(temp_dir, "catalog")
    branches_dir = os.path.join(temp_dir, "branches")
    tx_dir = os.path.join(temp_dir, "transactions")

    print_section("UDR Unified Batch/Stream Demo")
    print(f"Storage location: {temp_dir}")
    print("""
This demo shows how UDR unifies batch and streaming workloads:

  BATCH:  "What is the state?"     -> engine.query()
  STREAM: "What changed?"          -> engine.get_changes() / engine.subscribe()

Same data. Same guarantees. One system.
""")

    try:
        # Initialize UDR components with full transaction support
        store = udr.PyChunkStore(chunks_dir)
        catalog = udr.PyCatalog(catalog_dir)
        branches = udr.PyBranchManager(branches_dir)
        tx_manager = udr.PyTransactionManager(tx_dir, catalog_dir, branches_dir)

        engine = QueryEngine(
            store, catalog,
            branch_manager=branches,
            transaction_manager=tx_manager,
        )

        # =================================================================
        # PATTERN 1: Batch Query - "What is the current state?"
        # =================================================================
        print_section("Pattern 1: Batch Query")
        print("Traditional question: 'What is the current state of my data?'")

        # Create initial data through a transaction
        print_subsection("Creating initial order data...")

        orders_v1 = pd.DataFrame({
            "order_id": ["ORD001", "ORD002", "ORD003"],
            "customer": ["Alice", "Bob", "Charlie"],
            "total": [150.00, 275.50, 89.99],
            "status": ["pending", "pending", "pending"],
        })

        with engine.transaction() as tx:
            tx.write_table("orders", orders_v1)

        print("Initial orders created:")
        print(orders_v1.to_string(index=False))

        # Batch query - What is the current state?
        print_subsection("Batch Query: What are the pending orders?")
        result = engine.query("""
            SELECT order_id, customer, total
            FROM orders
            WHERE status = 'pending'
        """)
        print(result.to_pandas().to_string(index=False))
        print(f"\nTotal pending value: ${result.to_pandas()['total'].sum():.2f}")

        # =================================================================
        # PATTERN 2: Change Query - "What changed since X?"
        # =================================================================
        print_section("Pattern 2: Change Query")
        print("New question: 'What changed since my last check?'")

        # Record checkpoint before making changes
        checkpoint = engine.latest_tx_id()
        print(f"Checkpoint recorded: tx_id = {checkpoint}")

        # Make some changes through transactions
        print_subsection("Making changes: Adding customers and inventory tables")

        # Add customers table
        with engine.transaction() as tx:
            tx.write_table("customers", pd.DataFrame({
                "customer_id": ["C001", "C002", "C003"],
                "name": ["Alice", "Bob", "Charlie"],
                "tier": ["gold", "silver", "bronze"],
            }))

        # Add inventory table
        with engine.transaction() as tx:
            tx.write_table("inventory", pd.DataFrame({
                "sku": ["SKU001", "SKU002"],
                "quantity": [100, 50],
            }))

        # Query changes since checkpoint
        print_subsection("Change Query: What changed since checkpoint?")
        changes = engine.get_changes(since_tx_id=checkpoint)

        print(f"Found {len(changes)} changes since checkpoint:\n")
        for entry in changes:
            print(f"Transaction {entry['tx_id']} on branch '{entry['branch']}':")
            for change in entry['changes']:
                old_v = change['old_version'] or 'NEW'
                print(f"  - {change['table_name']}: v{old_v} -> v{change['new_version']}")

        # =================================================================
        # PATTERN 3: Polling Subscriber - Non-blocking change detection
        # =================================================================
        print_section("Pattern 3: Polling Subscriber")
        print("Use case: Periodic check for new changes (e.g., cron job, health check)")

        # Create subscriber starting from now (won't see past events)
        subscriber = engine.subscribe(poll_interval=0.5)
        print(f"Subscriber created, starting from tx_id = {subscriber.last_tx_id}")

        # Poll - should be empty (no new changes yet)
        events = subscriber.poll()
        print(f"First poll: {len(events)} events (expected: 0)")

        # Make a change
        print_subsection("Making a change while subscriber watches...")
        with engine.transaction() as tx:
            tx.write_table("metrics", pd.DataFrame({
                "metric": ["cpu_usage", "memory"],
                "value": [45.2, 78.5],
            }))

        # Poll again - should see the change
        events = subscriber.poll()
        print(f"Second poll: {len(events)} event(s)")
        for event in events:
            print(f"  - {event.table_name}: v{event.old_version or 'NEW'} -> v{event.new_version}")
            if event.is_new_table():
                print(f"    (New table created)")

        # =================================================================
        # PATTERN 4: Background Subscriber - Event-driven processing
        # =================================================================
        print_section("Pattern 4: Background Subscriber")
        print("Use case: React to changes in real-time (e.g., trigger downstream updates)")

        # Track received events
        received_events = []
        event_lock = threading.Lock()

        def on_change(event: ChangeEvent):
            """Callback for each change event."""
            with event_lock:
                received_events.append(event)
                print(f"  [CALLBACK] Received: {event.table_name} v{event.new_version}")

        # Start background subscriber
        bg_subscriber = engine.subscribe(poll_interval=0.2)
        bg_subscriber.start_background(on_change)
        print("Background subscriber started...")

        # Make changes that will trigger callbacks
        print_subsection("Making changes (callbacks should fire)...")

        with engine.transaction() as tx:
            tx.write_table("alerts", pd.DataFrame({
                "alert_id": ["A001"],
                "severity": ["high"],
                "message": ["CPU spike detected"],
            }))

        time.sleep(0.5)  # Wait for background thread

        with engine.transaction() as tx:
            tx.write_table("logs", pd.DataFrame({
                "timestamp": ["2024-01-16 10:30:00"],
                "level": ["INFO"],
                "message": ["System healthy"],
            }))

        time.sleep(0.5)  # Wait for background thread

        # Stop background subscriber
        bg_subscriber.stop()
        print("\nBackground subscriber stopped.")

        with event_lock:
            print(f"Total events received: {len(received_events)}")

        # =================================================================
        # PATTERN 5: Checkpoint/Resume - Incremental processing
        # =================================================================
        print_section("Pattern 5: Checkpoint/Resume Pattern")
        print("""
Use case: Incremental ETL, CDC (Change Data Capture)

This pattern enables:
- Process only NEW changes (not full table scan)
- Resume from where you left off after restart
- Build incremental pipelines without Kafka
""")

        print_subsection("Simulating incremental ETL job...")

        # Simulate: Job runs, processes data, saves checkpoint
        def run_etl_job(engine, last_checkpoint):
            """Simulate an incremental ETL job."""
            changes = engine.get_changes(since_tx_id=last_checkpoint)

            if not changes:
                print("  No new changes to process.")
                return last_checkpoint

            print(f"  Processing {len(changes)} new transactions...")
            for entry in changes:
                for change in entry['changes']:
                    print(f"    - ETL: {change['table_name']} v{change['new_version']}")

            # Return new checkpoint (last processed tx_id)
            return changes[-1]['tx_id']

        # First run - processes everything since our earlier checkpoint
        print("\nETL Run 1:")
        checkpoint = run_etl_job(engine, checkpoint)
        print(f"  New checkpoint: {checkpoint}")

        # Make more changes
        with engine.transaction() as tx:
            tx.write_table("daily_summary", pd.DataFrame({
                "date": ["2024-01-16"],
                "total_orders": [4],
                "revenue": [515.49],
            }))

        # Second run - only processes new changes
        print("\nETL Run 2:")
        checkpoint = run_etl_job(engine, checkpoint)
        print(f"  New checkpoint: {checkpoint}")

        # Third run - nothing to process
        print("\nETL Run 3:")
        checkpoint = run_etl_job(engine, checkpoint)

        # =================================================================
        # Summary: The Unified Model
        # =================================================================
        print_section("Summary: Unified Batch/Stream")

        print("""
UDR provides BOTH batch and stream semantics through ONE system:

  BEFORE UDR (fragmented):
  +-----------------+     +------------------+
  |   PostgreSQL    | --> |      Kafka       | --> downstream
  | (current state) |     | (change events)  |
  +-----------------+     +------------------+
       ^                        ^
       |                        |
    Batch queries          Stream processing
    (different API)        (different API)

  WITH UDR (unified):
  +------------------------------------------+
  |                   UDR                     |
  |                                          |
  |  engine.query()     ->  "What is state?" |
  |  engine.get_changes() -> "What changed?" |
  |  engine.subscribe()  ->  "Notify me"     |
  |                                          |
  |  Same data. Same guarantees. One system. |
  +------------------------------------------+

Key benefits:
  1. No separate streaming infrastructure needed
  2. Exactly-once semantics (ACID transactions)
  3. Time travel for debugging and replay
  4. Branch for experimentation
  5. Simple checkpoint/resume pattern
""")

        # Show final stats
        print_subsection("Final Statistics")

        # Count all transactions
        all_changes = engine.get_changes()
        print(f"Total committed transactions: {len(all_changes)}")

        # List tables
        tables = engine.list_tables()
        print(f"Tables created: {tables}")

        # Show latest tx_id
        latest = engine.latest_tx_id()
        print(f"Latest transaction ID: {latest}")

    finally:
        # Cleanup
        print(f"\nCleaning up {temp_dir}...")
        shutil.rmtree(temp_dir, ignore_errors=True)
        print("Done!")


if __name__ == "__main__":
    main()
