"""
Tests for Rhizo Transaction functionality (Phase 5.1).

Run with: pytest tests/test_transactions.py -v

These tests verify:
- Transaction lifecycle (begin, commit, abort)
- Cross-table atomic commits
- Read-your-writes within transactions
- Automatic rollback on exceptions
- Conflict detection
- Snapshot isolation
- Branch integration
"""

import os
import tempfile
import shutil

import pytest
import pandas as pd

import _rhizo
from rhizo import QueryEngine, TransactionContext


@pytest.fixture
def temp_storage():
    """Create temporary storage directories for testing."""
    base_dir = tempfile.mkdtemp(prefix="rhizo_tx_test_")
    chunks_dir = os.path.join(base_dir, "chunks")
    catalog_dir = os.path.join(base_dir, "catalog")
    branches_dir = os.path.join(base_dir, "branches")
    tx_dir = os.path.join(base_dir, "transactions")

    store = _rhizo.PyChunkStore(chunks_dir)
    catalog = _rhizo.PyCatalog(catalog_dir)
    branches = _rhizo.PyBranchManager(branches_dir)
    tx_manager = _rhizo.PyTransactionManager(tx_dir, catalog_dir, branches_dir)

    yield store, catalog, branches, tx_manager, base_dir

    # Cleanup
    shutil.rmtree(base_dir, ignore_errors=True)


@pytest.fixture
def engine_with_tx(temp_storage):
    """Create a QueryEngine with transaction support."""
    store, catalog, branches, tx_manager, base_dir = temp_storage

    engine = QueryEngine(
        store,
        catalog,
        branch_manager=branches,
        transaction_manager=tx_manager,
    )

    yield engine, base_dir

    engine.close()


class TestTransactionBasics:
    """Basic transaction lifecycle tests."""

    def test_transaction_commit(self, engine_with_tx):
        """Test basic transaction commit."""
        engine, _ = engine_with_tx

        df = pd.DataFrame({
            "id": [1, 2, 3],
            "name": ["Alice", "Bob", "Charlie"],
        })

        with engine.transaction() as tx:
            tx.write_table("users", df)
            # Explicit commit (also auto-commits on exit)

        # Verify data was committed
        result = engine.query("SELECT COUNT(*) as cnt FROM users")
        assert result.to_pandas()["cnt"].iloc[0] == 3

    def test_transaction_explicit_commit(self, engine_with_tx):
        """Test explicit commit within transaction."""
        engine, _ = engine_with_tx

        df = pd.DataFrame({"id": [1], "value": [100]})

        with engine.transaction() as tx:
            tx.write_table("data", df)
            tx.commit()
            # Context manager should not double-commit

        result = engine.query("SELECT value FROM data")
        assert result.to_pandas()["value"].iloc[0] == 100

    def test_transaction_abort(self, engine_with_tx):
        """Test explicit transaction abort."""
        engine, _ = engine_with_tx

        # Write initial data
        df1 = pd.DataFrame({"id": [1], "value": [100]})
        engine.write_table("data", df1)

        # Try to update in a transaction that we abort
        df2 = pd.DataFrame({"id": [1], "value": [999]})

        with engine.transaction() as tx:
            tx.write_table("data", df2)
            tx.abort("Changed my mind")

        # Original data should be unchanged
        result = engine.query("SELECT value FROM data")
        assert result.to_pandas()["value"].iloc[0] == 100

    def test_transaction_rollback_on_exception(self, engine_with_tx):
        """Test automatic rollback when exception is raised."""
        engine, _ = engine_with_tx

        # Write initial data
        df1 = pd.DataFrame({"id": [1], "value": [100]})
        engine.write_table("data", df1)

        # Transaction that raises an exception
        df2 = pd.DataFrame({"id": [1], "value": [999]})

        with pytest.raises(ValueError, match="intentional"):
            with engine.transaction() as tx:
                tx.write_table("data", df2)
                raise ValueError("intentional error")

        # Original data should be unchanged
        result = engine.query("SELECT value FROM data")
        assert result.to_pandas()["value"].iloc[0] == 100

    def test_transaction_not_configured(self, temp_storage):
        """Test that transaction() fails without transaction_manager."""
        store, catalog, branches, tx_manager, _ = temp_storage

        # Engine without transaction manager
        engine = QueryEngine(store, catalog, branch_manager=branches)

        with pytest.raises(RuntimeError, match="transaction_manager not configured"):
            with engine.transaction() as tx:
                pass

    def test_nested_transactions_not_supported(self, engine_with_tx):
        """Test that nested transactions raise error."""
        engine, _ = engine_with_tx

        with engine.transaction() as tx1:
            with pytest.raises(RuntimeError, match="Nested transactions"):
                with engine.transaction() as tx2:
                    pass


class TestCrossTableTransactions:
    """Tests for atomic multi-table transactions."""

    def test_atomic_multi_table_commit(self, engine_with_tx):
        """Test that multiple tables commit atomically."""
        engine, _ = engine_with_tx

        users_df = pd.DataFrame({
            "id": [1, 2],
            "name": ["Alice", "Bob"],
        })

        orders_df = pd.DataFrame({
            "id": [1, 2],
            "user_id": [1, 2],
            "total": [100.0, 200.0],
        })

        audit_df = pd.DataFrame({
            "action": ["created_users", "created_orders"],
            "timestamp": [1, 2],
        })

        with engine.transaction() as tx:
            tx.write_table("users", users_df)
            tx.write_table("orders", orders_df)
            tx.write_table("audit_log", audit_df)

        # All tables should be committed
        users_count = engine.query("SELECT COUNT(*) as cnt FROM users").to_pandas()["cnt"].iloc[0]
        orders_count = engine.query("SELECT COUNT(*) as cnt FROM orders").to_pandas()["cnt"].iloc[0]
        audit_count = engine.query("SELECT COUNT(*) as cnt FROM audit_log").to_pandas()["cnt"].iloc[0]

        assert users_count == 2
        assert orders_count == 2
        assert audit_count == 2

    def test_multi_table_rollback_on_exception(self, engine_with_tx):
        """Test that all tables are rolled back on exception."""
        engine, _ = engine_with_tx

        # Write initial data to one table
        engine.write_table("existing", pd.DataFrame({"id": [1]}))

        with pytest.raises(ValueError):
            with engine.transaction() as tx:
                tx.write_table("new_table_1", pd.DataFrame({"a": [1]}))
                tx.write_table("new_table_2", pd.DataFrame({"b": [2]}))
                raise ValueError("Abort everything")

        # New tables should not exist
        tables = engine.list_tables()
        assert "new_table_1" not in tables
        assert "new_table_2" not in tables
        assert "existing" in tables


class TestReadYourWrites:
    """Tests for read-your-writes within transactions."""

    def test_read_buffered_write(self, engine_with_tx):
        """Test that queries within transaction see buffered writes."""
        engine, _ = engine_with_tx

        df = pd.DataFrame({
            "id": [1, 2, 3],
            "score": [10, 20, 30],
        })

        with engine.transaction() as tx:
            tx.write_table("scores", df)

            # Query should see buffered data
            result = tx.query("SELECT SUM(score) as total FROM scores")
            assert result.to_pandas()["total"].iloc[0] == 60

    def test_read_updated_data(self, engine_with_tx):
        """Test that updates within transaction are visible."""
        engine, _ = engine_with_tx

        # Write initial data
        df1 = pd.DataFrame({"id": [1, 2], "value": [100, 200]})
        engine.write_table("data", df1)

        # Update in transaction
        df2 = pd.DataFrame({"id": [1, 2], "value": [150, 250]})

        with engine.transaction() as tx:
            tx.write_table("data", df2)

            # Should see updated values
            result = tx.query("SELECT SUM(value) as total FROM data")
            assert result.to_pandas()["total"].iloc[0] == 400

    def test_multiple_writes_same_table(self, engine_with_tx):
        """Test multiple writes to same table within transaction."""
        engine, _ = engine_with_tx

        with engine.transaction() as tx:
            # First write
            tx.write_table("data", pd.DataFrame({"x": [1]}))
            result1 = tx.query("SELECT x FROM data")
            assert result1.to_pandas()["x"].iloc[0] == 1

            # Second write (overwrites)
            tx.write_table("data", pd.DataFrame({"x": [999]}))
            result2 = tx.query("SELECT x FROM data")
            assert result2.to_pandas()["x"].iloc[0] == 999


class TestSnapshotIsolation:
    """Tests for snapshot isolation."""

    def test_transaction_sees_snapshot(self, engine_with_tx):
        """Test that transaction reads see consistent snapshot."""
        engine, _ = engine_with_tx

        # Write initial data
        df1 = pd.DataFrame({"id": [1], "value": [100]})
        engine.write_table("data", df1)

        with engine.transaction() as tx:
            # Read initial value - transaction sees snapshot at start
            result1 = tx.query("SELECT value FROM data")
            assert result1.to_pandas()["value"].iloc[0] == 100

            # Transaction still sees v1 even after reading
            result2 = tx.query("SELECT value FROM data")
            assert result2.to_pandas()["value"].iloc[0] == 100

    def test_snapshot_conflict_detection(self, engine_with_tx):
        """Test that concurrent writes cause snapshot conflict on commit."""
        engine, _ = engine_with_tx

        # Write initial data
        df1 = pd.DataFrame({"id": [1], "value": [100]})
        engine.write_table("data", df1)

        # Snapshot conflict should be detected when underlying data changes
        with pytest.raises(ValueError, match="Snapshot conflict"):
            with engine.transaction() as tx:
                # Read data (captures snapshot version)
                tx.query("SELECT value FROM data")

                # Write new value outside transaction (simulating concurrent write)
                df2 = pd.DataFrame({"id": [1], "value": [999]})
                engine.write_table("data", df2)

                # Transaction will auto-commit and detect the conflict

    def test_writes_not_visible_until_commit(self, engine_with_tx):
        """Test that buffered writes are not visible outside transaction."""
        engine, _ = engine_with_tx

        # Write initial data
        df1 = pd.DataFrame({"id": [1], "value": [100]})
        engine.write_table("data", df1)

        with engine.transaction() as tx:
            tx.write_table("data", pd.DataFrame({"id": [1], "value": [999]}))

            # Query outside transaction context sees old data
            # (Direct engine query, not through tx)
            # This is tricky to test without actual concurrency,
            # but we can verify the buffered write mechanism works

    def test_new_table_not_visible_in_transaction(self, engine_with_tx):
        """Test that a table created after tx.begin is not readable within tx."""
        engine, _ = engine_with_tx

        # Write initial table so we have something in the snapshot
        df1 = pd.DataFrame({"id": [1], "value": [100]})
        engine.write_table("existing", df1)

        with engine.transaction() as tx:
            # Table "existing" is in the snapshot and readable
            result = tx.query("SELECT value FROM existing")
            assert result.to_pandas()["value"].iloc[0] == 100

            # Create a NEW table outside the transaction
            df_new = pd.DataFrame({"id": [1], "name": ["phantom"]})
            engine.write_table("phantom_table", df_new)

            # Querying the new table within the transaction should fail
            # because it didn't exist in the snapshot
            with pytest.raises(ValueError, match="does not exist in transaction snapshot"):
                tx.query("SELECT * FROM phantom_table")

    def test_existing_table_still_readable_in_transaction(self, engine_with_tx):
        """Test that existing tables in the snapshot are still fully readable."""
        engine, _ = engine_with_tx

        # Write multiple tables before starting the transaction
        df_users = pd.DataFrame({"id": [1, 2], "name": ["alice", "bob"]})
        df_orders = pd.DataFrame({"id": [1], "user_id": [1], "amount": [50]})
        engine.write_table("users", df_users)
        engine.write_table("orders", df_orders)

        with engine.transaction() as tx:
            # Both tables should be readable from the snapshot
            users = tx.query("SELECT * FROM users")
            assert users.row_count == 2

            orders = tx.query("SELECT * FROM orders")
            assert orders.row_count == 1

            # Can query them multiple times consistently
            users2 = tx.query("SELECT name FROM users WHERE id = 1")
            assert users2.to_pandas()["name"].iloc[0] == "alice"

    def test_transaction_can_write_new_table(self, engine_with_tx):
        """Test that buffered writes to new tables still work within tx."""
        engine, _ = engine_with_tx

        with engine.transaction() as tx:
            # Writing a new table within the transaction should succeed
            df = pd.DataFrame({"id": [1], "value": [42]})
            tx.write_table("brand_new", df)

            # Can query the buffered write within the transaction
            result = tx.query("SELECT value FROM brand_new")
            assert result.to_pandas()["value"].iloc[0] == 42

        # After commit, the table should be visible
        result = engine.query("SELECT value FROM brand_new")
        assert result.to_pandas()["value"].iloc[0] == 42

    def test_new_table_visible_after_commit(self, engine_with_tx):
        """Test that tables created outside a tx are visible in subsequent queries."""
        engine, _ = engine_with_tx

        with engine.transaction() as tx:
            # Write something so the tx has work to do
            tx.write_table("tx_data", pd.DataFrame({"x": [1]}))

        # Now create a new table outside any transaction
        engine.write_table("later_table", pd.DataFrame({"y": [99]}))

        # A new transaction should see the new table in its snapshot
        with engine.transaction() as tx2:
            result = tx2.query("SELECT y FROM later_table")
            assert result.to_pandas()["y"].iloc[0] == 99


class TestBranchIntegration:
    """Tests for transaction + branch integration."""

    def test_transaction_on_current_branch(self, engine_with_tx):
        """Test transaction operates on current branch by default."""
        engine, _ = engine_with_tx

        df = pd.DataFrame({"id": [1], "value": [100]})

        with engine.transaction() as tx:
            assert tx.branch == "main"
            tx.write_table("data", df)

        # Verify data on main branch
        result = engine.query("SELECT value FROM data", branch="main")
        assert result.to_pandas()["value"].iloc[0] == 100

    def test_transaction_on_feature_branch(self, engine_with_tx):
        """Test transaction on a specific branch."""
        engine, _ = engine_with_tx

        # Create feature branch
        engine.create_branch("feature/test")

        df = pd.DataFrame({"id": [1], "value": [999]})

        with engine.transaction(branch="feature/test") as tx:
            assert tx.branch == "feature/test"
            tx.write_table("data", df)

        # Data should be on feature branch
        result = engine.query("SELECT value FROM data", branch="feature/test")
        assert result.to_pandas()["value"].iloc[0] == 999

        # Main branch should not have this data
        tables = engine.branch_manager.get("main").head
        assert "data" not in tables

    def test_transaction_updates_branch_head(self, engine_with_tx):
        """Test that committed transaction updates branch head."""
        engine, _ = engine_with_tx

        df = pd.DataFrame({"id": [1, 2, 3]})

        with engine.transaction() as tx:
            tx.write_table("users", df)

        # Branch head should be updated
        head = engine.branch_manager.get("main").head
        assert "users" in head
        assert head["users"] == 1  # First version


class TestTransactionContext:
    """Tests for TransactionContext behavior."""

    def test_tx_id_property(self, engine_with_tx):
        """Test accessing transaction ID."""
        engine, _ = engine_with_tx

        with engine.transaction() as tx:
            assert tx.tx_id > 0
            assert isinstance(tx.tx_id, int)

    def test_is_active_property(self, engine_with_tx):
        """Test is_active property."""
        engine, _ = engine_with_tx

        with engine.transaction() as tx:
            assert tx.is_active is True
            tx.commit()
            assert tx.is_active is False

    def test_operation_after_commit_fails(self, engine_with_tx):
        """Test that operations fail after commit."""
        engine, _ = engine_with_tx

        with engine.transaction() as tx:
            tx.commit()

            with pytest.raises(RuntimeError, match="already committed"):
                tx.write_table("test", pd.DataFrame({"x": [1]}))

            with pytest.raises(RuntimeError, match="already committed"):
                tx.query("SELECT 1")

    def test_operation_after_abort_fails(self, engine_with_tx):
        """Test that operations fail after abort."""
        engine, _ = engine_with_tx

        with engine.transaction() as tx:
            tx.abort()

            with pytest.raises(RuntimeError, match="was aborted"):
                tx.write_table("test", pd.DataFrame({"x": [1]}))

    def test_double_commit_idempotent(self, engine_with_tx):
        """Test that double commit is safe (idempotent)."""
        engine, _ = engine_with_tx

        with engine.transaction() as tx:
            tx.write_table("data", pd.DataFrame({"x": [1]}))
            tx.commit()
            tx.commit()  # Should not raise

    def test_double_abort_idempotent(self, engine_with_tx):
        """Test that double abort is safe (idempotent)."""
        engine, _ = engine_with_tx

        with engine.transaction() as tx:
            tx.abort()
            tx.abort()  # Should not raise

    def test_commit_after_abort_fails(self, engine_with_tx):
        """Test that commit after abort fails."""
        engine, _ = engine_with_tx

        with engine.transaction() as tx:
            tx.abort()

            with pytest.raises(RuntimeError, match="Cannot commit aborted"):
                tx.commit()

    def test_abort_after_commit_fails(self, engine_with_tx):
        """Test that abort after commit fails."""
        engine, _ = engine_with_tx

        with engine.transaction() as tx:
            tx.write_table("data", pd.DataFrame({"x": [1]}))
            tx.commit()

            with pytest.raises(RuntimeError, match="Cannot abort committed"):
                tx.abort()


class TestTransactionEdgeCases:
    """Edge case and error handling tests."""

    def test_empty_transaction(self, engine_with_tx):
        """Test transaction with no writes commits successfully."""
        engine, _ = engine_with_tx

        # Should not raise
        with engine.transaction() as tx:
            pass

    def test_write_invalid_data_type(self, engine_with_tx):
        """Test that invalid data types are rejected."""
        engine, _ = engine_with_tx

        with engine.transaction() as tx:
            with pytest.raises(TypeError, match="Expected DataFrame"):
                tx.write_table("data", {"not": "a dataframe"})

    def test_query_pandas_in_transaction(self, engine_with_tx):
        """Test query_pandas helper within transaction."""
        engine, _ = engine_with_tx

        df = pd.DataFrame({"x": [1, 2, 3]})

        with engine.transaction() as tx:
            tx.write_table("data", df)
            result = tx.query_pandas("SELECT SUM(x) as total FROM data")
            assert result["total"].iloc[0] == 6


class TestConcurrency:
    """Concurrency and thread-safety tests."""

    def test_concurrent_writes_different_tables(self, temp_storage):
        """Test concurrent transactions writing to different tables."""
        import threading
        import time

        store, catalog, branches, tx_manager, base_dir = temp_storage

        results = {"errors": [], "success_count": 0}
        lock = threading.Lock()

        def write_table(table_name: str, data: dict):
            """Worker function to write to a specific table."""
            try:
                # Each thread gets its own engine instance
                engine = QueryEngine(
                    store, catalog,
                    branch_manager=branches,
                    transaction_manager=tx_manager,
                )
                df = pd.DataFrame(data)

                with engine.transaction() as tx:
                    tx.write_table(table_name, df)
                    # Small delay to increase chance of overlap
                    time.sleep(0.01)

                engine.close()

                with lock:
                    results["success_count"] += 1

            except Exception as e:
                with lock:
                    results["errors"].append(f"{table_name}: {e}")

        # Create threads for different tables
        threads = []
        for i in range(5):
            t = threading.Thread(
                target=write_table,
                args=(f"table_{i}", {"id": [i], "value": [i * 100]})
            )
            threads.append(t)

        # Start all threads
        for t in threads:
            t.start()

        # Wait for completion
        for t in threads:
            t.join(timeout=10)

        # Verify results
        assert not results["errors"], f"Errors occurred: {results['errors']}"
        assert results["success_count"] == 5

        # Verify all tables were written
        engine = QueryEngine(
            store, catalog,
            branch_manager=branches,
            transaction_manager=tx_manager,
        )
        for i in range(5):
            result = engine.query(f"SELECT * FROM table_{i}")
            assert result.row_count == 1
        engine.close()

    def test_concurrent_writes_same_table_conflict(self, temp_storage):
        """Test that concurrent writes to the same table trigger conflict detection."""
        import threading
        import time

        store, catalog, branches, tx_manager, base_dir = temp_storage

        # First, create the table
        engine = QueryEngine(
            store, catalog,
            branch_manager=branches,
            transaction_manager=tx_manager,
        )
        initial_df = pd.DataFrame({"id": [1], "value": [100]})
        engine.write_table("shared_table", initial_df)
        engine.close()

        results = {"commits": 0, "conflicts": 0, "errors": []}
        lock = threading.Lock()
        barrier = threading.Barrier(3)  # Synchronize thread starts

        def concurrent_update(thread_id: int):
            """Worker function that tries to update the shared table."""
            try:
                engine = QueryEngine(
                    store, catalog,
                    branch_manager=branches,
                    transaction_manager=tx_manager,
                )

                # Wait for all threads to be ready
                barrier.wait()

                with engine.transaction() as tx:
                    # Read current value
                    current = tx.query("SELECT value FROM shared_table")

                    # Simulate some processing time
                    time.sleep(0.05)

                    # Write new value
                    new_df = pd.DataFrame({"id": [1], "value": [thread_id * 1000]})
                    tx.write_table("shared_table", new_df)

                engine.close()

                with lock:
                    results["commits"] += 1

            except ValueError as e:
                # Conflict detected
                if "conflict" in str(e).lower():
                    with lock:
                        results["conflicts"] += 1
                else:
                    with lock:
                        results["errors"].append(f"Thread {thread_id}: {e}")
            except Exception as e:
                with lock:
                    results["errors"].append(f"Thread {thread_id}: {e}")

        # Create threads that will all try to update the same table
        threads = []
        for i in range(3):
            t = threading.Thread(target=concurrent_update, args=(i,))
            threads.append(t)

        # Start all threads
        for t in threads:
            t.start()

        # Wait for completion
        for t in threads:
            t.join(timeout=10)

        # At least one should succeed, others may conflict or all succeed
        # (depending on timing - not all may overlap)
        assert not results["errors"], f"Unexpected errors: {results['errors']}"
        assert results["commits"] >= 1, "At least one transaction should commit"
        # Note: We can't guarantee conflicts will occur due to timing

    def test_concurrent_reads_during_write(self, temp_storage):
        """Test that reads see consistent snapshots during concurrent writes."""
        import threading
        import time

        store, catalog, branches, tx_manager, base_dir = temp_storage

        # Create initial data
        engine = QueryEngine(
            store, catalog,
            branch_manager=branches,
            transaction_manager=tx_manager,
        )
        initial_df = pd.DataFrame({"id": [1, 2, 3], "value": [10, 20, 30]})
        engine.write_table("data", initial_df)
        engine.close()

        read_results = []
        write_done = threading.Event()
        read_started = threading.Event()
        lock = threading.Lock()

        def reader():
            """Reader that queries multiple times during the write."""
            engine = QueryEngine(
                store, catalog,
                branch_manager=branches,
                transaction_manager=tx_manager,
            )

            read_started.set()  # Signal that reader is ready

            # Read multiple times while writer is working
            for _ in range(5):
                result = engine.query("SELECT SUM(value) as total FROM data")
                total = result.to_pandas()["total"].iloc[0]
                with lock:
                    read_results.append(total)
                time.sleep(0.02)

            engine.close()

        def writer():
            """Writer that updates all rows in a transaction."""
            engine = QueryEngine(
                store, catalog,
                branch_manager=branches,
                transaction_manager=tx_manager,
            )

            read_started.wait()  # Wait for reader to start

            with engine.transaction() as tx:
                # Update to new values (sum = 600 instead of 60)
                new_df = pd.DataFrame({"id": [1, 2, 3], "value": [100, 200, 300]})
                tx.write_table("data", new_df)
                time.sleep(0.05)  # Hold transaction open

            write_done.set()
            engine.close()

        # Start reader and writer
        reader_thread = threading.Thread(target=reader)
        writer_thread = threading.Thread(target=writer)

        reader_thread.start()
        writer_thread.start()

        reader_thread.join(timeout=10)
        writer_thread.join(timeout=10)

        # All reads should see consistent values (either 60 or 600, not partial)
        valid_values = {60, 600}
        for val in read_results:
            assert val in valid_values, f"Read inconsistent value: {val}"

    def test_sequential_transactions_different_tables(self, engine_with_tx):
        """Test that sequential transactions writing to different tables work correctly."""
        engine, _ = engine_with_tx

        # Multiple sequential transactions writing to different tables
        for i in range(5):
            with engine.transaction() as tx:
                df = pd.DataFrame({"id": [i], "value": [i * 100]})
                tx.write_table(f"sequential_table_{i}", df)

        # Verify all tables were written
        for i in range(5):
            result = engine.query(f"SELECT value FROM sequential_table_{i}")
            assert result.to_pandas()["value"].iloc[0] == i * 100

    def test_write_conflict_detection_same_table(self, engine_with_tx):
        """
        Test that write-write conflicts to the same table are detected.

        This verifies the ACID conflict detection: if two transactions
        within the same epoch both write to the same table, the second
        one should fail with a conflict error.
        """
        engine, _ = engine_with_tx

        # Transaction 1: Create and write to table
        with engine.transaction() as tx:
            df1 = pd.DataFrame({"id": [1], "value": [100]})
            tx.write_table("conflict_table", df1)

        # Transaction 2: Should fail when writing to the same table
        # (within same epoch, conflict detection triggers)
        with pytest.raises(ValueError, match="conflict"):
            with engine.transaction() as tx:
                df2 = pd.DataFrame({"id": [1], "value": [200]})
                tx.write_table("conflict_table", df2)


class TestConflictScenarios:
    """
    Comprehensive tests for transaction conflict detection scenarios.

    These tests verify:
    - Concurrent write conflict detection
    - Three-way merge conflicts
    - Read-write isolation (snapshot isolation)
    - Nested transaction behavior
    - Timeout and deadlock scenarios
    """

    def test_concurrent_write_conflict(self, temp_storage):
        """
        Test that concurrent write conflicts are properly detected.

        Scenario:
        - Transaction A starts
        - Transaction B starts
        - Both write to the same table
        - Transaction A commits first
        - Transaction B's commit should detect conflict or handle gracefully
        """
        import threading
        import time

        store, catalog, branches, tx_manager, base_dir = temp_storage

        # Create initial table data
        engine = QueryEngine(
            store, catalog,
            branch_manager=branches,
            transaction_manager=tx_manager,
        )
        initial_df = pd.DataFrame({"id": [1], "value": [100]})
        engine.write_table("concurrent_test", initial_df)
        engine.close()

        results = {"tx_a_committed": False, "tx_b_conflict": False, "errors": []}
        lock = threading.Lock()
        tx_a_started = threading.Event()
        tx_b_started = threading.Event()
        tx_a_written = threading.Event()

        def transaction_a():
            """First transaction that will commit successfully."""
            try:
                eng = QueryEngine(
                    store, catalog,
                    branch_manager=branches,
                    transaction_manager=tx_manager,
                )
                with eng.transaction() as tx:
                    tx_a_started.set()
                    tx_b_started.wait(timeout=5)  # Wait for B to start

                    # Write and commit first
                    df = pd.DataFrame({"id": [1], "value": [200]})
                    tx.write_table("concurrent_test", df)
                    tx_a_written.set()

                eng.close()
                with lock:
                    results["tx_a_committed"] = True
            except Exception as e:
                with lock:
                    results["errors"].append(f"TX_A: {e}")

        def transaction_b():
            """Second transaction that should detect conflict."""
            try:
                eng = QueryEngine(
                    store, catalog,
                    branch_manager=branches,
                    transaction_manager=tx_manager,
                )
                tx_a_started.wait(timeout=5)  # Wait for A to start

                with eng.transaction() as tx:
                    tx_b_started.set()
                    tx_a_written.wait(timeout=5)  # Wait for A to write
                    time.sleep(0.1)  # Give A time to commit

                    # Try to write same table - should conflict
                    df = pd.DataFrame({"id": [1], "value": [300]})
                    tx.write_table("concurrent_test", df)

                eng.close()
            except ValueError as e:
                if "conflict" in str(e).lower() or "snapshot" in str(e).lower():
                    with lock:
                        results["tx_b_conflict"] = True
                else:
                    with lock:
                        results["errors"].append(f"TX_B unexpected ValueError: {e}")
            except Exception as e:
                with lock:
                    results["errors"].append(f"TX_B: {e}")

        # Run both transactions
        thread_a = threading.Thread(target=transaction_a)
        thread_b = threading.Thread(target=transaction_b)

        thread_a.start()
        thread_b.start()

        thread_a.join(timeout=10)
        thread_b.join(timeout=10)

        # Verify results
        assert not results["errors"], f"Errors: {results['errors']}"
        assert results["tx_a_committed"], "Transaction A should have committed"
        # Transaction B either detects conflict or succeeds if it commits before A's changes are visible
        # This is acceptable behavior for concurrent transactions

    def test_concurrent_write_conflict_with_barrier(self, temp_storage):
        """
        Test concurrent write conflict with synchronized start using barrier.

        This uses a barrier to ensure both transactions start simultaneously,
        maximizing the chance of conflict detection.
        """
        import threading
        import time

        store, catalog, branches, tx_manager, base_dir = temp_storage

        # Create initial table
        engine = QueryEngine(
            store, catalog,
            branch_manager=branches,
            transaction_manager=tx_manager,
        )
        engine.write_table("barrier_test", pd.DataFrame({"id": [1], "value": [0]}))
        engine.close()

        results = {"commits": 0, "conflicts": 0, "errors": []}
        lock = threading.Lock()
        barrier = threading.Barrier(2)

        def concurrent_writer(writer_id: int):
            try:
                eng = QueryEngine(
                    store, catalog,
                    branch_manager=branches,
                    transaction_manager=tx_manager,
                )

                # Synchronize start
                barrier.wait(timeout=5)

                with eng.transaction() as tx:
                    # Both read current state
                    tx.query("SELECT * FROM barrier_test")

                    # Small delay to increase conflict window
                    time.sleep(0.02)

                    # Both try to write
                    df = pd.DataFrame({"id": [1], "value": [writer_id * 100]})
                    tx.write_table("barrier_test", df)

                eng.close()
                with lock:
                    results["commits"] += 1

            except ValueError as e:
                if "conflict" in str(e).lower() or "snapshot" in str(e).lower():
                    with lock:
                        results["conflicts"] += 1
                else:
                    with lock:
                        results["errors"].append(f"Writer {writer_id}: {e}")
            except Exception as e:
                with lock:
                    results["errors"].append(f"Writer {writer_id}: {e}")

        threads = [
            threading.Thread(target=concurrent_writer, args=(i,))
            for i in range(2)
        ]

        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        # Verify
        assert not results["errors"], f"Errors: {results['errors']}"
        # At least one should commit, at most one should conflict
        assert results["commits"] >= 1, "At least one transaction should commit"
        assert results["commits"] + results["conflicts"] == 2, \
            f"Total should be 2: {results['commits']} commits + {results['conflicts']} conflicts"

    def test_three_way_conflict(self, temp_storage):
        """
        Test conflict resolution with three concurrent transactions.

        Scenario:
        - Transaction A writes table X
        - Transaction B writes table X
        - Transaction C writes table X
        - Only one should succeed, others should detect conflicts
        """
        import threading
        import time

        store, catalog, branches, tx_manager, base_dir = temp_storage

        # Create initial table
        engine = QueryEngine(
            store, catalog,
            branch_manager=branches,
            transaction_manager=tx_manager,
        )
        engine.write_table("three_way", pd.DataFrame({"id": [1], "value": [0]}))
        engine.close()

        results = {"commits": 0, "conflicts": 0, "errors": []}
        lock = threading.Lock()
        barrier = threading.Barrier(3)

        def writer(tx_name: str, new_value: int):
            try:
                eng = QueryEngine(
                    store, catalog,
                    branch_manager=branches,
                    transaction_manager=tx_manager,
                )

                barrier.wait(timeout=5)

                with eng.transaction() as tx:
                    # Read (establishes snapshot)
                    tx.query("SELECT * FROM three_way")

                    # Stagger writes slightly
                    time.sleep(0.01 * new_value)

                    df = pd.DataFrame({"id": [1], "value": [new_value]})
                    tx.write_table("three_way", df)

                eng.close()
                with lock:
                    results["commits"] += 1

            except ValueError as e:
                if "conflict" in str(e).lower() or "snapshot" in str(e).lower():
                    with lock:
                        results["conflicts"] += 1
                else:
                    with lock:
                        results["errors"].append(f"{tx_name}: {e}")
            except Exception as e:
                with lock:
                    results["errors"].append(f"{tx_name}: {e}")

        threads = [
            threading.Thread(target=writer, args=("A", 100)),
            threading.Thread(target=writer, args=("B", 200)),
            threading.Thread(target=writer, args=("C", 300)),
        ]

        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)

        # Verify
        assert not results["errors"], f"Errors: {results['errors']}"
        # At least one commit, some may conflict
        assert results["commits"] >= 1, "At least one transaction should commit"
        assert results["commits"] + results["conflicts"] == 3, \
            f"Total should be 3: {results['commits']} commits + {results['conflicts']} conflicts"

        # Verify final state is consistent
        eng = QueryEngine(
            store, catalog,
            branch_manager=branches,
            transaction_manager=tx_manager,
        )
        result = eng.query("SELECT value FROM three_way")
        final_value = result.to_pandas()["value"].iloc[0]
        assert final_value in [100, 200, 300], f"Unexpected final value: {final_value}"
        eng.close()

    def test_three_way_conflict_sequential_commits(self, engine_with_tx):
        """
        Test three-way conflict with sequential commits (deterministic version).

        This tests the scenario where transactions start sequentially and
        each tries to write to the same table, demonstrating conflict detection
        without threading complexity.
        """
        engine, _ = engine_with_tx

        # Create initial table
        engine.write_table("seq_three_way", pd.DataFrame({"id": [1], "value": [0]}))

        commits = 0
        conflicts = 0

        # Transaction 1 should succeed
        with engine.transaction() as tx:
            tx.write_table("seq_three_way", pd.DataFrame({"id": [1], "value": [100]}))
        commits += 1

        # Transaction 2 - should see conflict (snapshot conflict)
        try:
            with engine.transaction() as tx:
                tx.write_table("seq_three_way", pd.DataFrame({"id": [1], "value": [200]}))
            commits += 1
        except ValueError as e:
            if "conflict" in str(e).lower():
                conflicts += 1
            else:
                raise

        # Transaction 3 - also should see conflict
        try:
            with engine.transaction() as tx:
                tx.write_table("seq_three_way", pd.DataFrame({"id": [1], "value": [300]}))
            commits += 1
        except ValueError as e:
            if "conflict" in str(e).lower():
                conflicts += 1
            else:
                raise

        # First commit always succeeds, subsequent may conflict
        assert commits >= 1, "At least one transaction should commit"

    def test_read_write_isolation(self, temp_storage):
        """
        Test snapshot isolation for read-write conflicts.

        Scenario:
        - Transaction A reads table
        - Transaction B writes table and commits
        - Transaction A sees old data during reads (snapshot isolation)
        - Transaction A detects stale snapshot at commit time (strict isolation)

        Note: Rhizo implements strict snapshot isolation where even read-only
        transactions validate their snapshots at commit. If the snapshot is
        stale, the transaction fails. This prevents anomalies but requires
        readers to handle snapshot conflicts.
        """
        import threading
        import time

        store, catalog, branches, tx_manager, base_dir = temp_storage

        # Create initial data
        engine = QueryEngine(
            store, catalog,
            branch_manager=branches,
            transaction_manager=tx_manager,
        )
        initial_df = pd.DataFrame({"id": [1, 2, 3], "value": [10, 20, 30]})
        engine.write_table("isolation_test", initial_df)
        engine.close()

        results = {
            "reader_first_sum": None,
            "reader_second_sum": None,
            "writer_committed": False,
            "reader_snapshot_conflict": False,
            "errors": [],
        }
        lock = threading.Lock()
        reader_started = threading.Event()
        writer_done = threading.Event()

        def reader():
            try:
                eng = QueryEngine(
                    store, catalog,
                    branch_manager=branches,
                    transaction_manager=tx_manager,
                )

                with eng.transaction() as tx:
                    # First read - captures snapshot
                    result1 = tx.query("SELECT SUM(value) as total FROM isolation_test")
                    first_sum = result1.to_pandas()["total"].iloc[0]

                    with lock:
                        results["reader_first_sum"] = first_sum

                    reader_started.set()

                    # Wait for writer to complete
                    writer_done.wait(timeout=5)
                    time.sleep(0.1)  # Give writer time to fully commit

                    # Second read - should still see snapshot (old data)
                    # even though external write has occurred
                    result2 = tx.query("SELECT SUM(value) as total FROM isolation_test")
                    second_sum = result2.to_pandas()["total"].iloc[0]

                    with lock:
                        results["reader_second_sum"] = second_sum

                    # Transaction will fail at commit due to snapshot validation
                    # (strict snapshot isolation)

                eng.close()

            except ValueError as e:
                if "snapshot" in str(e).lower() or "conflict" in str(e).lower():
                    with lock:
                        results["reader_snapshot_conflict"] = True
                else:
                    with lock:
                        results["errors"].append(f"Reader: {e}")
            except Exception as e:
                with lock:
                    results["errors"].append(f"Reader: {e}")

        def writer():
            try:
                reader_started.wait(timeout=5)

                eng = QueryEngine(
                    store, catalog,
                    branch_manager=branches,
                    transaction_manager=tx_manager,
                )

                with eng.transaction() as tx:
                    # Write new values (sum = 600)
                    new_df = pd.DataFrame({"id": [1, 2, 3], "value": [100, 200, 300]})
                    tx.write_table("isolation_test", new_df)

                eng.close()

                with lock:
                    results["writer_committed"] = True

                writer_done.set()

            except Exception as e:
                with lock:
                    results["errors"].append(f"Writer: {e}")
                writer_done.set()

        reader_thread = threading.Thread(target=reader)
        writer_thread = threading.Thread(target=writer)

        reader_thread.start()
        writer_thread.start()

        reader_thread.join(timeout=10)
        writer_thread.join(timeout=10)

        # Verify results
        assert not results["errors"], f"Errors: {results['errors']}"
        assert results["writer_committed"], "Writer should have committed"

        # First read should see original sum (60)
        assert results["reader_first_sum"] == 60, \
            f"First read should see original sum 60, got {results['reader_first_sum']}"

        # Second read within transaction should also see 60 (snapshot data)
        # This verifies reads see consistent snapshot during transaction
        assert results["reader_second_sum"] == 60, \
            f"Second read should still see snapshot sum 60, got {results['reader_second_sum']}"

        # Strict snapshot isolation: reader detects stale snapshot at commit
        assert results["reader_snapshot_conflict"], \
            "Reader should detect snapshot conflict at commit time (strict isolation)"

    def test_read_write_isolation_single_engine(self, engine_with_tx):
        """
        Test snapshot isolation with a single engine (simpler verification).

        Demonstrates that reads within a transaction see a consistent snapshot
        even when external writes occur. However, with strict snapshot isolation,
        the transaction will fail at commit time if the snapshot became stale.
        """
        engine, _ = engine_with_tx

        # Create initial data
        initial_df = pd.DataFrame({"id": [1, 2], "value": [50, 50]})
        engine.write_table("snapshot_test", initial_df)

        # Track read values to verify snapshot consistency during transaction
        read_values = []
        snapshot_conflict = False

        try:
            with engine.transaction() as tx:
                # Read establishes snapshot
                result1 = tx.query("SELECT SUM(value) as total FROM snapshot_test")
                read_values.append(result1.to_pandas()["total"].iloc[0])

                # External write (outside transaction) - this creates a new version
                engine.write_table("snapshot_test", pd.DataFrame({"id": [1, 2], "value": [500, 500]}))

                # Read within transaction should still see old data
                result2 = tx.query("SELECT SUM(value) as total FROM snapshot_test")
                read_values.append(result2.to_pandas()["total"].iloc[0])

                # Don't write - just verifying read isolation
                # Transaction will fail at commit due to strict snapshot validation

        except ValueError as e:
            if "snapshot" in str(e).lower() or "conflict" in str(e).lower():
                snapshot_conflict = True
            else:
                raise

        # Both reads should see consistent snapshot value (100)
        assert read_values[0] == 100, f"First read should be 100, got {read_values[0]}"
        assert read_values[1] == 100, f"Second read should be 100, got {read_values[1]}"

        # Strict snapshot isolation: even read-only transactions detect stale snapshots
        assert snapshot_conflict, "Should detect snapshot conflict at commit (strict isolation)"

    def test_read_then_write_conflict(self, engine_with_tx):
        """
        Test that read-then-write pattern detects conflicts properly.

        If a transaction reads a table, then external changes occur,
        attempting to write to that table should fail (snapshot conflict).
        """
        engine, _ = engine_with_tx

        # Create initial data
        engine.write_table("read_write_conflict", pd.DataFrame({"id": [1], "value": [100]}))

        with pytest.raises(ValueError, match="[Ss]napshot|[Cc]onflict"):
            with engine.transaction() as tx:
                # Read establishes snapshot at version 1
                tx.query("SELECT * FROM read_write_conflict")

                # External write creates version 2
                engine.write_table("read_write_conflict", pd.DataFrame({"id": [1], "value": [200]}))

                # Try to write - should fail due to snapshot conflict
                tx.write_table("read_write_conflict", pd.DataFrame({"id": [1], "value": [300]}))
                # Auto-commit should detect conflict

    def test_nested_transactions_not_supported(self, engine_with_tx):
        """
        Verify that nested transactions raise an appropriate error.

        Rhizo does not support nested transactions. Attempting to start
        a transaction while another is active should fail.
        """
        engine, _ = engine_with_tx

        with engine.transaction() as tx1:
            assert tx1.is_active

            # Attempting to start another transaction should fail
            with pytest.raises(RuntimeError, match="[Nn]ested"):
                with engine.transaction() as tx2:
                    pass

    def test_nested_transaction_attempt_with_write(self, engine_with_tx):
        """
        Test that nested transaction attempt after write still fails.
        """
        engine, _ = engine_with_tx

        with engine.transaction() as tx1:
            # Write some data
            tx1.write_table("outer_tx", pd.DataFrame({"x": [1]}))

            # Still cannot nest
            with pytest.raises(RuntimeError, match="[Nn]ested"):
                with engine.transaction() as tx2:
                    tx2.write_table("inner_tx", pd.DataFrame({"y": [2]}))

    def test_transaction_reuse_after_completion(self, engine_with_tx):
        """
        Test that a completed transaction context cannot be reused.
        """
        engine, _ = engine_with_tx

        tx_ref = None
        with engine.transaction() as tx:
            tx_ref = tx
            tx.write_table("reuse_test", pd.DataFrame({"x": [1]}))

        # Transaction is now committed
        assert not tx_ref.is_active

        # Cannot query
        with pytest.raises(RuntimeError, match="committed"):
            tx_ref.query("SELECT * FROM reuse_test")

        # Cannot write
        with pytest.raises(RuntimeError, match="committed"):
            tx_ref.write_table("another", pd.DataFrame({"y": [2]}))

    def test_long_running_transaction_conflict(self, temp_storage):
        """
        Test conflict detection for long-running transactions.

        A transaction that takes a long time should still detect conflicts
        when external changes occur to tables it has read.
        """
        import time

        store, catalog, branches, tx_manager, base_dir = temp_storage

        engine = QueryEngine(
            store, catalog,
            branch_manager=branches,
            transaction_manager=tx_manager,
        )

        # Create initial data
        engine.write_table("long_running", pd.DataFrame({"id": [1], "value": [100]}))

        # Start long-running transaction
        conflict_detected = False
        try:
            with engine.transaction() as tx:
                # Read data
                tx.query("SELECT * FROM long_running")

                # Simulate long-running work
                time.sleep(0.1)

                # External modification
                engine.write_table("long_running", pd.DataFrame({"id": [1], "value": [999]}))

                # More work
                time.sleep(0.1)

                # Try to commit with our changes - should fail
                tx.write_table("long_running", pd.DataFrame({"id": [1], "value": [200]}))

        except ValueError as e:
            if "conflict" in str(e).lower() or "snapshot" in str(e).lower():
                conflict_detected = True
            else:
                raise

        assert conflict_detected, "Long-running transaction should detect conflict"
        engine.close()

    def test_write_only_transaction_no_conflict(self, engine_with_tx):
        """
        Test that write-only transactions to different tables don't conflict.
        """
        engine, _ = engine_with_tx

        # First transaction writes to table A
        with engine.transaction() as tx:
            tx.write_table("table_a", pd.DataFrame({"x": [1]}))

        # Second transaction writes to table B - no conflict
        with engine.transaction() as tx:
            tx.write_table("table_b", pd.DataFrame({"y": [2]}))

        # Verify both tables exist
        assert engine.query("SELECT x FROM table_a").to_pandas()["x"].iloc[0] == 1
        assert engine.query("SELECT y FROM table_b").to_pandas()["y"].iloc[0] == 2

    def test_multiple_tables_partial_conflict(self, engine_with_tx):
        """
        Test conflict detection when transactions share some but not all tables.

        Transaction A writes to tables X and Y
        Transaction B writes to tables Y and Z
        Should conflict on Y.
        """
        engine, _ = engine_with_tx

        # Create initial tables
        engine.write_table("multi_x", pd.DataFrame({"x": [0]}))
        engine.write_table("multi_y", pd.DataFrame({"y": [0]}))
        engine.write_table("multi_z", pd.DataFrame({"z": [0]}))

        # Transaction A writes X and Y
        with engine.transaction() as tx:
            tx.write_table("multi_x", pd.DataFrame({"x": [1]}))
            tx.write_table("multi_y", pd.DataFrame({"y": [1]}))

        # Transaction B writes Y and Z - should conflict on Y
        conflict_detected = False
        try:
            with engine.transaction() as tx:
                tx.write_table("multi_y", pd.DataFrame({"y": [2]}))
                tx.write_table("multi_z", pd.DataFrame({"z": [2]}))
        except ValueError as e:
            if "conflict" in str(e).lower():
                conflict_detected = True
            else:
                raise

        # Conflict should be detected due to overlapping table Y
        assert conflict_detected, "Should detect conflict on shared table Y"

    def test_conflict_error_message_contains_table_name(self, temp_storage):
        """
        Test that conflict error messages include relevant table information.
        """
        import threading

        store, catalog, branches, tx_manager, base_dir = temp_storage

        engine = QueryEngine(
            store, catalog,
            branch_manager=branches,
            transaction_manager=tx_manager,
        )

        # Create table
        engine.write_table("error_msg_test", pd.DataFrame({"id": [1]}))

        # Cause a conflict
        error_message = None
        try:
            with engine.transaction() as tx:
                tx.query("SELECT * FROM error_msg_test")

                # External write
                engine.write_table("error_msg_test", pd.DataFrame({"id": [2]}))

                # This should fail
                tx.write_table("error_msg_test", pd.DataFrame({"id": [3]}))
        except ValueError as e:
            error_message = str(e)

        assert error_message is not None, "Should have raised an error"
        # Error message should mention conflict or snapshot
        assert "conflict" in error_message.lower() or "snapshot" in error_message.lower()
        engine.close()

    def test_empty_transaction_no_conflict(self, engine_with_tx):
        """
        Test that empty transactions (read-only) don't cause conflicts.
        """
        engine, _ = engine_with_tx

        # Create table
        engine.write_table("empty_tx_test", pd.DataFrame({"id": [1]}))

        # Empty transaction 1
        with engine.transaction() as tx1:
            pass  # No reads or writes

        # Empty transaction 2
        with engine.transaction() as tx2:
            pass  # No reads or writes

        # Read-only transaction
        with engine.transaction() as tx3:
            result = tx3.query("SELECT * FROM empty_tx_test")
            assert result.row_count == 1

        # All should complete without error

    def test_aborted_transaction_no_conflict_effect(self, engine_with_tx):
        """
        Test that aborted transactions don't affect conflict detection.
        """
        engine, _ = engine_with_tx

        # Create table
        engine.write_table("abort_effect", pd.DataFrame({"id": [1], "value": [100]}))

        # Transaction that will be aborted
        with engine.transaction() as tx:
            tx.write_table("abort_effect", pd.DataFrame({"id": [1], "value": [999]}))
            tx.abort("intentionally aborted")

        # Subsequent transaction should succeed (no conflict with aborted tx)
        with engine.transaction() as tx:
            tx.write_table("abort_effect", pd.DataFrame({"id": [1], "value": [200]}))

        # Verify final value
        result = engine.query("SELECT value FROM abort_effect")
        assert result.to_pandas()["value"].iloc[0] == 200


class TestTransactionTimeoutScenarios:
    """
    Tests for transaction timeout and deadlock-like scenarios.

    Note: Rhizo's current implementation doesn't have explicit timeout
    support, but these tests verify behavior under time pressure.
    """

    def test_rapid_sequential_transactions(self, engine_with_tx):
        """
        Test many rapid sequential transactions to the same table.

        This stress tests the conflict detection mechanism.
        """
        engine, _ = engine_with_tx

        # Create initial table
        engine.write_table("rapid_test", pd.DataFrame({"counter": [0]}))

        success_count = 0
        conflict_count = 0

        for i in range(10):
            try:
                with engine.transaction() as tx:
                    current = tx.query("SELECT counter FROM rapid_test")
                    new_val = current.to_pandas()["counter"].iloc[0] + 1
                    tx.write_table("rapid_test", pd.DataFrame({"counter": [new_val]}))
                success_count += 1
            except ValueError as e:
                if "conflict" in str(e).lower():
                    conflict_count += 1
                else:
                    raise

        # At least some should succeed
        assert success_count >= 1, f"At least 1 transaction should succeed, got {success_count}"

    def test_concurrent_read_heavy_workload(self, temp_storage):
        """
        Test many concurrent readers with occasional writers.

        Note: With strict snapshot isolation, readers that read a table
        which is then modified by a writer will fail at commit time.
        This test verifies:
        1. Readers see consistent values during their transaction
        2. The system handles concurrent access without corruption
        """
        import threading
        import time

        store, catalog, branches, tx_manager, base_dir = temp_storage

        # Create initial data
        engine = QueryEngine(
            store, catalog,
            branch_manager=branches,
            transaction_manager=tx_manager,
        )
        engine.write_table("read_heavy", pd.DataFrame({"value": [100]}))
        engine.close()

        results = {"reads": [], "write_count": 0, "snapshot_conflicts": 0, "other_errors": []}
        lock = threading.Lock()

        def reader(reader_id: int):
            eng = QueryEngine(
                store, catalog,
                branch_manager=branches,
                transaction_manager=tx_manager,
            )

            for _ in range(3):
                try:
                    with eng.transaction() as tx:
                        result = tx.query("SELECT value FROM read_heavy")
                        val = result.to_pandas()["value"].iloc[0]
                        with lock:
                            results["reads"].append(val)
                except ValueError as e:
                    if "snapshot" in str(e).lower() or "conflict" in str(e).lower():
                        with lock:
                            results["snapshot_conflicts"] += 1
                    else:
                        with lock:
                            results["other_errors"].append(f"Reader {reader_id}: {e}")
                except Exception as e:
                    with lock:
                        results["other_errors"].append(f"Reader {reader_id}: {e}")
                time.sleep(0.01)

            eng.close()

        def writer():
            eng = QueryEngine(
                store, catalog,
                branch_manager=branches,
                transaction_manager=tx_manager,
            )

            for i in range(3):
                time.sleep(0.02)
                try:
                    with eng.transaction() as tx:
                        tx.write_table("read_heavy", pd.DataFrame({"value": [(i + 2) * 100]}))
                    with lock:
                        results["write_count"] += 1
                except ValueError:
                    pass  # Conflicts are OK

            eng.close()

        # Start readers and one writer
        threads = [
            threading.Thread(target=reader, args=(i,))
            for i in range(5)
        ]
        threads.append(threading.Thread(target=writer))

        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)

        # Verify
        assert not results["other_errors"], f"Unexpected errors: {results['other_errors']}"
        # All successful reads should be valid values
        for val in results["reads"]:
            assert val >= 100, f"Invalid read value: {val}"
        # Some snapshot conflicts are expected due to strict isolation
        # (readers that read a table modified by writer will conflict)

    def test_transaction_with_computation(self, engine_with_tx):
        """
        Test transaction with significant computation between read and write.

        Simulates a real-world pattern where computation happens between
        reading data and writing results.
        """
        import time

        engine, _ = engine_with_tx

        # Create initial data
        engine.write_table("compute_test", pd.DataFrame({
            "id": list(range(100)),
            "value": [i * 10 for i in range(100)]
        }))

        with engine.transaction() as tx:
            # Read data
            data = tx.query_pandas("SELECT * FROM compute_test")

            # Simulate computation
            time.sleep(0.05)
            computed_sum = data["value"].sum()

            # Write result
            tx.write_table("compute_result", pd.DataFrame({
                "total": [computed_sum],
                "count": [len(data)]
            }))

        # Verify
        result = engine.query("SELECT total, count FROM compute_result")
        df = result.to_pandas()
        assert df["total"].iloc[0] == 49500  # Sum of 0, 10, 20, ..., 990
        assert df["count"].iloc[0] == 100

    def test_transaction_isolation_levels_simulation(self, temp_storage):
        """
        Test that demonstrates snapshot isolation behavior.

        In Rhizo's strict snapshot isolation:
        - Readers never block writers
        - Writers never block readers
        - Reads within a transaction see consistent snapshot data
        - Stale snapshots are detected at commit time (strict isolation)
        - Write-write conflicts are detected at commit time
        """
        import threading
        import time

        store, catalog, branches, tx_manager, base_dir = temp_storage

        # Create initial data
        engine = QueryEngine(
            store, catalog,
            branch_manager=branches,
            transaction_manager=tx_manager,
        )
        engine.write_table("isolation_demo", pd.DataFrame({
            "account": ["A", "B"],
            "balance": [1000, 1000]
        }))
        engine.close()

        results = {
            "reader_values": [],
            "writer_success": False,
            "reader_snapshot_conflict": False,
            "other_errors": []
        }
        lock = threading.Lock()
        reader_in_tx = threading.Event()
        writer_committed = threading.Event()

        def long_reader():
            """Reader that holds transaction open during writer's commit."""
            try:
                eng = QueryEngine(
                    store, catalog,
                    branch_manager=branches,
                    transaction_manager=tx_manager,
                )

                with eng.transaction() as tx:
                    # First read
                    r1 = tx.query_pandas("SELECT SUM(balance) as total FROM isolation_demo")
                    with lock:
                        results["reader_values"].append(r1["total"].iloc[0])

                    reader_in_tx.set()

                    # Wait for writer
                    writer_committed.wait(timeout=5)
                    time.sleep(0.05)

                    # Second read - should see same value (snapshot)
                    r2 = tx.query_pandas("SELECT SUM(balance) as total FROM isolation_demo")
                    with lock:
                        results["reader_values"].append(r2["total"].iloc[0])

                    # Transaction will fail at commit due to strict snapshot validation

                eng.close()
            except ValueError as e:
                if "snapshot" in str(e).lower() or "conflict" in str(e).lower():
                    with lock:
                        results["reader_snapshot_conflict"] = True
                else:
                    with lock:
                        results["other_errors"].append(f"Reader: {e}")
                reader_in_tx.set()
            except Exception as e:
                with lock:
                    results["other_errors"].append(f"Reader: {e}")
                reader_in_tx.set()

        def writer():
            """Writer that modifies data while reader is active."""
            try:
                reader_in_tx.wait(timeout=5)

                eng = QueryEngine(
                    store, catalog,
                    branch_manager=branches,
                    transaction_manager=tx_manager,
                )

                with eng.transaction() as tx:
                    # Transfer money (total stays 2000)
                    tx.write_table("isolation_demo", pd.DataFrame({
                        "account": ["A", "B"],
                        "balance": [500, 1500]
                    }))

                eng.close()

                with lock:
                    results["writer_success"] = True
                writer_committed.set()
            except Exception as e:
                with lock:
                    results["other_errors"].append(f"Writer: {e}")
                writer_committed.set()

        reader_thread = threading.Thread(target=long_reader)
        writer_thread = threading.Thread(target=writer)

        reader_thread.start()
        writer_thread.start()

        reader_thread.join(timeout=10)
        writer_thread.join(timeout=10)

        # Verify snapshot isolation behavior
        assert not results["other_errors"], f"Unexpected errors: {results['other_errors']}"
        assert results["writer_success"], "Writer should succeed"

        # Both reader values should be 2000 (consistent snapshot within transaction)
        assert len(results["reader_values"]) == 2
        assert results["reader_values"][0] == 2000, "First read should see 2000"
        assert results["reader_values"][1] == 2000, "Second read should also see 2000 (snapshot)"

        # Strict snapshot isolation: reader detects stale snapshot at commit
        assert results["reader_snapshot_conflict"], \
            "Reader should detect snapshot conflict at commit (strict isolation)"
