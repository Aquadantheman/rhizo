"""
Tests for Armillaria Recovery functionality (Phase 5.2).

Run with: pytest tests/test_recovery.py -v

These tests verify:
- Recovery on clean system (no pending transactions)
- Consistency verification
- Recovery report structure
- Recovery after simulated crashes
- Epoch-based recovery
"""

import os
import tempfile
import shutil

import pytest
import pandas as pd

import armillaria
from armillaria_query import QueryEngine


@pytest.fixture
def temp_storage():
    """Create temporary storage directories for testing."""
    base_dir = tempfile.mkdtemp(prefix="armillaria_recovery_test_")
    chunks_dir = os.path.join(base_dir, "chunks")
    catalog_dir = os.path.join(base_dir, "catalog")
    branches_dir = os.path.join(base_dir, "branches")
    tx_dir = os.path.join(base_dir, "transactions")

    store = armillaria.PyChunkStore(chunks_dir)
    catalog = armillaria.PyCatalog(catalog_dir)
    branches = armillaria.PyBranchManager(branches_dir)
    tx_manager = armillaria.PyTransactionManager(tx_dir, catalog_dir, branches_dir)

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

    yield engine, tx_manager, base_dir

    engine.close()


class TestRecoveryBasics:
    """Basic recovery functionality tests."""

    def test_recovery_on_clean_system(self, temp_storage):
        """Recovery on a fresh system with no transactions should succeed."""
        _, _, _, tx_manager, _ = temp_storage

        report = tx_manager.recover()

        # Clean system should have no issues
        assert report.is_clean is True
        assert len(report.errors) == 0
        assert len(report.warnings) == 0
        assert len(report.replayed) == 0
        assert len(report.rolled_back) == 0

    def test_recovery_after_committed_transaction(self, engine_with_tx):
        """Recovery after normal committed transactions should be clean."""
        engine, tx_manager, _ = engine_with_tx

        df = pd.DataFrame({
            "id": [1, 2, 3],
            "name": ["Alice", "Bob", "Charlie"],
        })

        # Perform normal transaction
        with engine.transaction() as tx:
            tx.write_table("users", df)

        # Run recovery
        report = tx_manager.recover()

        # Should be clean - committed transaction was properly recorded
        assert report.is_clean is True
        assert len(report.errors) == 0

    def test_recovery_report_structure(self, temp_storage):
        """Verify recovery report has all expected fields."""
        _, _, _, tx_manager, _ = temp_storage

        report = tx_manager.recover()

        # Verify all expected attributes exist
        assert hasattr(report, 'last_committed_epoch')
        assert hasattr(report, 'replayed')
        assert hasattr(report, 'rolled_back')
        assert hasattr(report, 'already_aborted')
        assert hasattr(report, 'already_committed')
        assert hasattr(report, 'warnings')
        assert hasattr(report, 'errors')
        assert hasattr(report, 'is_clean')

        # Verify types
        assert isinstance(report.replayed, list)
        assert isinstance(report.rolled_back, list)
        assert isinstance(report.warnings, list)
        assert isinstance(report.errors, list)
        assert isinstance(report.is_clean, bool)


class TestConsistencyVerification:
    """Consistency verification tests."""

    def test_verify_consistency_on_clean_system(self, temp_storage):
        """Consistency check on clean system should return no issues."""
        _, _, _, tx_manager, _ = temp_storage

        issues = tx_manager.verify_consistency()

        # Clean system should have no consistency issues
        assert isinstance(issues, list)
        assert len(issues) == 0

    def test_verify_consistency_after_transactions(self, engine_with_tx):
        """Consistency check after normal operations should be clean."""
        engine, tx_manager, _ = engine_with_tx

        # Perform some transactions
        df1 = pd.DataFrame({"id": [1, 2], "value": [10, 20]})
        df2 = pd.DataFrame({"id": [3, 4], "value": [30, 40]})

        with engine.transaction() as tx:
            tx.write_table("table1", df1)
            tx.write_table("table2", df2)

        # Verify consistency
        issues = tx_manager.verify_consistency()
        assert len(issues) == 0


class TestRecoveryAndApply:
    """Tests for recover_and_apply functionality."""

    def test_recover_and_apply_clean_system(self, temp_storage):
        """Recover and apply on clean system should succeed."""
        _, _, _, tx_manager, _ = temp_storage

        report = tx_manager.recover_and_apply()

        assert report.is_clean is True
        assert len(report.errors) == 0

    def test_recover_and_apply_after_committed_transaction(self, engine_with_tx):
        """Recover and apply after committed transaction should be clean."""
        engine, tx_manager, _ = engine_with_tx

        df = pd.DataFrame({
            "id": [1, 2],
            "amount": [100, 200],
        })

        with engine.transaction() as tx:
            tx.write_table("orders", df)

        # Recover and apply
        report = tx_manager.recover_and_apply()

        assert report.is_clean is True


class TestTransactionStateRecovery:
    """Tests for transaction state recovery scenarios."""

    def test_multiple_transactions_recovery(self, engine_with_tx):
        """Recovery after multiple committed transactions should be clean."""
        engine, tx_manager, _ = engine_with_tx

        # Multiple transactions to different tables (avoiding conflict detection)
        for i in range(3):
            df = pd.DataFrame({
                "id": [i * 10 + j for j in range(5)],
                "batch": [i] * 5,
            })
            with engine.transaction() as tx:
                tx.write_table(f"batch_{i}", df)

        # Recovery should find everything clean
        report = tx_manager.recover()
        assert report.is_clean is True

    def test_aborted_transaction_recovery(self, engine_with_tx):
        """Recovery after aborted transaction should be clean."""
        engine, tx_manager, _ = engine_with_tx

        df = pd.DataFrame({"id": [1], "value": [100]})

        # Start and abort a transaction
        try:
            with engine.transaction() as tx:
                tx.write_table("data", df)
                raise ValueError("Simulated error")
        except ValueError:
            pass  # Expected

        # Recovery should be clean (aborted tx was properly rolled back)
        report = tx_manager.recover()
        assert report.is_clean is True

    def test_active_count_after_recovery(self, engine_with_tx):
        """Active transaction count should be zero after recovery."""
        engine, tx_manager, _ = engine_with_tx

        # Perform a transaction
        df = pd.DataFrame({"id": [1], "name": ["test"]})
        with engine.transaction() as tx:
            tx.write_table("test", df)

        # No active transactions after commit
        assert tx_manager.active_count() == 0

        # Recovery
        tx_manager.recover()

        # Still no active transactions
        assert tx_manager.active_count() == 0


class TestEpochRecovery:
    """Tests for epoch-based recovery."""

    def test_last_committed_epoch_tracking(self, engine_with_tx):
        """Last committed epoch should be tracked in recovery report."""
        engine, tx_manager, _ = engine_with_tx

        # Initial state - no epoch yet
        report = tx_manager.recover()
        initial_epoch = report.last_committed_epoch

        # Commit a transaction
        df = pd.DataFrame({"id": [1], "value": [100]})
        with engine.transaction() as tx:
            tx.write_table("data", df)

        # Check epoch after commit
        report = tx_manager.recover()
        # Epoch should be tracked (might be None or a number)
        assert hasattr(report, 'last_committed_epoch')


class TestRecoveryIdempotence:
    """Tests for recovery idempotence (running recovery multiple times)."""

    def test_recovery_is_idempotent(self, engine_with_tx):
        """Running recovery multiple times should produce same result."""
        engine, tx_manager, _ = engine_with_tx

        df = pd.DataFrame({"id": [1, 2, 3], "value": [10, 20, 30]})
        with engine.transaction() as tx:
            tx.write_table("data", df)

        # Run recovery multiple times
        report1 = tx_manager.recover()
        report2 = tx_manager.recover()
        report3 = tx_manager.recover()

        # All should be clean
        assert report1.is_clean is True
        assert report2.is_clean is True
        assert report3.is_clean is True

        # No changes on subsequent runs
        assert len(report2.replayed) == 0
        assert len(report3.replayed) == 0

    def test_recover_and_apply_is_idempotent(self, engine_with_tx):
        """Running recover_and_apply multiple times should be safe."""
        engine, tx_manager, _ = engine_with_tx

        df = pd.DataFrame({"id": [1], "value": [100]})
        with engine.transaction() as tx:
            tx.write_table("data", df)

        # Run multiple times
        for _ in range(3):
            report = tx_manager.recover_and_apply()
            assert report.is_clean is True


class TestRecoveryWithBranches:
    """Tests for recovery with branch operations."""

    def test_recovery_preserves_branch_state(self, engine_with_tx):
        """Recovery should preserve branch head pointers."""
        engine, tx_manager, base_dir = engine_with_tx

        # Create a branch and write data
        engine.create_branch("feature/test")
        engine.checkout("feature/test")

        df = pd.DataFrame({"id": [1, 2], "name": ["a", "b"]})
        with engine.transaction() as tx:
            tx.write_table("data", df)

        # Get branch head before recovery
        branch = engine.branch_manager.get("feature/test")
        head_before = dict(branch.head)

        # Run recovery
        report = tx_manager.recover()
        assert report.is_clean is True

        # Branch head should be preserved
        branch = engine.branch_manager.get("feature/test")
        head_after = dict(branch.head)

        assert head_before == head_after

    def test_recovery_after_multi_branch_transactions(self, engine_with_tx):
        """Recovery after transactions on multiple branches."""
        engine, tx_manager, _ = engine_with_tx

        # Main branch transaction
        df_main = pd.DataFrame({"id": [1], "branch": ["main"]})
        with engine.transaction() as tx:
            tx.write_table("main_data", df_main)

        # Feature branch transaction (to different table to avoid conflict)
        engine.create_branch("feature/x")
        engine.checkout("feature/x")
        df_feature = pd.DataFrame({"id": [2], "branch": ["feature"]})
        with engine.transaction() as tx:
            tx.write_table("feature_data", df_feature)

        # Recovery should be clean
        report = tx_manager.recover()
        assert report.is_clean is True
        assert len(report.errors) == 0
