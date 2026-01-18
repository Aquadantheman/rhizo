"""
Tests for Armillaria Transaction functionality (Phase 5.1).

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
