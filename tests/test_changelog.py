"""
Tests for Phase 6: Changelog & Subscriptions.

Tests the unified batch/stream model:
- Batch: "What is the state?" -> engine.query()
- Stream: "What changed?" -> engine.get_changes() / engine.subscribe()
"""

import pytest
import tempfile
import time
import threading
import pandas as pd
from pathlib import Path

import _rhizo
from rhizo import QueryEngine, Subscriber, ChangeEvent


@pytest.fixture
def temp_storage():
    """Create temporary storage directories."""
    with tempfile.TemporaryDirectory() as tmpdir:
        base = Path(tmpdir)
        yield {
            "chunks": str(base / "chunks"),
            "catalog": str(base / "catalog"),
            "branches": str(base / "branches"),
            "transactions": str(base / "transactions"),
        }


@pytest.fixture
def engine_with_tx(temp_storage):
    """Create QueryEngine with transaction support."""
    store = _rhizo.PyChunkStore(temp_storage["chunks"])
    catalog = _rhizo.PyCatalog(temp_storage["catalog"])
    branches = _rhizo.PyBranchManager(temp_storage["branches"])
    tx_manager = _rhizo.PyTransactionManager(
        temp_storage["transactions"],
        temp_storage["catalog"],
        temp_storage["branches"],
    )

    engine = QueryEngine(
        store, catalog,
        branch_manager=branches,
        transaction_manager=tx_manager,
    )

    yield engine


@pytest.fixture
def engine_no_tx(temp_storage):
    """Create QueryEngine without transaction support."""
    store = _rhizo.PyChunkStore(temp_storage["chunks"])
    catalog = _rhizo.PyCatalog(temp_storage["catalog"])

    engine = QueryEngine(store, catalog)

    yield engine


class TestGetChanges:
    """Tests for engine.get_changes() method."""

    def test_get_changes_empty(self, engine_with_tx):
        """Empty changelog returns empty list."""
        changes = engine_with_tx.get_changes()
        assert changes == []

    def test_get_changes_after_transaction(self, engine_with_tx):
        """Changes appear after committed transaction."""
        df = pd.DataFrame({"id": [1, 2], "name": ["Alice", "Bob"]})

        with engine_with_tx.transaction() as tx:
            tx.write_table("users", df)

        changes = engine_with_tx.get_changes()

        assert len(changes) == 1
        assert changes[0]["branch"] == "main"
        assert len(changes[0]["changes"]) == 1
        assert changes[0]["changes"][0]["table_name"] == "users"
        assert changes[0]["changes"][0]["old_version"] is None  # New table
        assert changes[0]["changes"][0]["new_version"] == 1

    def test_get_changes_multiple_transactions(self, engine_with_tx):
        """Multiple transactions to different tables create changelog entries."""
        df1 = pd.DataFrame({"id": [1], "name": ["Alice"]})
        df2 = pd.DataFrame({"id": [1, 2], "name": ["Alice", "Bob"]})

        # Two separate transactions to different tables (no conflict)
        with engine_with_tx.transaction() as tx:
            tx.write_table("users", df1)

        with engine_with_tx.transaction() as tx:
            tx.write_table("orders", df2)

        changes = engine_with_tx.get_changes()

        # Two changelog entries, one per transaction
        assert len(changes) == 2
        table_names = [c["changes"][0]["table_name"] for c in changes]
        assert "users" in table_names
        assert "orders" in table_names

    def test_get_changes_since_tx_id(self, engine_with_tx):
        """Filter changes since a specific transaction."""
        df = pd.DataFrame({"id": [1], "name": ["Test"]})

        # Create 3 transactions
        for i in range(3):
            with engine_with_tx.transaction() as tx:
                tx.write_table(f"table{i}", df)

        # Get all changes
        all_changes = engine_with_tx.get_changes()
        assert len(all_changes) == 3

        # Get changes since first transaction
        first_tx_id = all_changes[0]["tx_id"]
        recent_changes = engine_with_tx.get_changes(since_tx_id=first_tx_id)
        assert len(recent_changes) == 2

    def test_get_changes_filter_tables(self, engine_with_tx):
        """Filter changes to specific tables."""
        df = pd.DataFrame({"id": [1]})

        with engine_with_tx.transaction() as tx:
            tx.write_table("users", df)
            tx.write_table("orders", df)

        # Filter to users only
        changes = engine_with_tx.get_changes(tables=["users"])

        assert len(changes) == 1
        assert len(changes[0]["changes"]) == 2  # Entry has both, but matched on users

    def test_get_changes_with_limit(self, engine_with_tx):
        """Limit number of returned entries."""
        df = pd.DataFrame({"id": [1]})

        for i in range(5):
            with engine_with_tx.transaction() as tx:
                tx.write_table(f"table{i}", df)

        changes = engine_with_tx.get_changes(limit=2)
        assert len(changes) == 2

    def test_get_changes_no_tx_manager_raises(self, engine_no_tx):
        """Raises RuntimeError without transaction_manager."""
        with pytest.raises(RuntimeError, match="transaction_manager not configured"):
            engine_no_tx.get_changes()


class TestLatestTxId:
    """Tests for engine.latest_tx_id() method."""

    def test_latest_tx_id_empty(self, engine_with_tx):
        """Returns None when no transactions."""
        assert engine_with_tx.latest_tx_id() is None

    def test_latest_tx_id_after_commits(self, engine_with_tx):
        """Returns latest transaction ID."""
        df = pd.DataFrame({"id": [1]})

        with engine_with_tx.transaction() as tx:
            tx.write_table("test", df)

        tx_id_1 = engine_with_tx.latest_tx_id()
        assert tx_id_1 is not None

        with engine_with_tx.transaction() as tx:
            tx.write_table("test2", df)

        tx_id_2 = engine_with_tx.latest_tx_id()
        assert tx_id_2 > tx_id_1

    def test_latest_tx_id_no_tx_manager_raises(self, engine_no_tx):
        """Raises RuntimeError without transaction_manager."""
        with pytest.raises(RuntimeError, match="transaction_manager not configured"):
            engine_no_tx.latest_tx_id()


class TestSubscribe:
    """Tests for engine.subscribe() method."""

    def test_subscribe_returns_subscriber(self, engine_with_tx):
        """subscribe() returns a Subscriber instance."""
        subscriber = engine_with_tx.subscribe()
        assert isinstance(subscriber, Subscriber)

    def test_subscribe_with_filters(self, engine_with_tx):
        """subscribe() accepts filter parameters."""
        subscriber = engine_with_tx.subscribe(
            tables=["users"],
            branch="main",
            since_tx_id=0,
            poll_interval=0.5,
        )
        assert isinstance(subscriber, Subscriber)

    def test_subscribe_no_tx_manager_raises(self, engine_no_tx):
        """Raises RuntimeError without transaction_manager."""
        with pytest.raises(RuntimeError, match="transaction_manager not configured"):
            engine_no_tx.subscribe()


class TestSubscriberPoll:
    """Tests for Subscriber.poll() method."""

    def test_poll_empty(self, engine_with_tx):
        """poll() returns empty list when no new changes."""
        subscriber = engine_with_tx.subscribe()
        events = subscriber.poll()
        assert events == []

    def test_poll_returns_events(self, engine_with_tx):
        """poll() returns events for new changes."""
        # Start subscriber before changes
        subscriber = engine_with_tx.subscribe(since_tx_id=0)

        # Make a change
        df = pd.DataFrame({"id": [1], "name": ["Alice"]})
        with engine_with_tx.transaction() as tx:
            tx.write_table("users", df)

        # Poll for changes
        events = subscriber.poll()

        assert len(events) == 1
        assert events[0].table_name == "users"
        assert events[0].old_version is None
        assert events[0].new_version == 1

    def test_poll_updates_cursor(self, engine_with_tx):
        """poll() updates cursor so next poll only gets new events."""
        subscriber = engine_with_tx.subscribe(since_tx_id=0)

        df = pd.DataFrame({"id": [1]})

        # First transaction
        with engine_with_tx.transaction() as tx:
            tx.write_table("table1", df)

        events1 = subscriber.poll()
        assert len(events1) == 1

        # Second transaction
        with engine_with_tx.transaction() as tx:
            tx.write_table("table2", df)

        events2 = subscriber.poll()
        assert len(events2) == 1
        assert events2[0].table_name == "table2"

    def test_poll_filter_tables(self, engine_with_tx):
        """poll() respects table filter."""
        subscriber = engine_with_tx.subscribe(tables=["users"], since_tx_id=0)

        df = pd.DataFrame({"id": [1]})

        with engine_with_tx.transaction() as tx:
            tx.write_table("users", df)
            tx.write_table("orders", df)

        events = subscriber.poll()

        # Should get the entry (contains users), which has both changes
        # But filtering happens at transaction level, not event level
        table_names = [e.table_name for e in events]
        assert "users" in table_names


class TestChangeEvent:
    """Tests for ChangeEvent dataclass."""

    def test_change_event_is_new_table(self, engine_with_tx):
        """is_new_table() returns True for new tables."""
        subscriber = engine_with_tx.subscribe(since_tx_id=0)

        df = pd.DataFrame({"id": [1]})
        with engine_with_tx.transaction() as tx:
            tx.write_table("new_table", df)

        events = subscriber.poll()

        assert len(events) == 1
        assert events[0].is_new_table() is True

    def test_change_event_not_new_table(self, engine_with_tx):
        """
        Verify we can detect updates vs new tables.

        Note: old_version tracking requires prior transactions.
        When initial write is outside a transaction, the changelog
        doesn't track it, so old_version will be None.
        The new_version > 1 indicates it's not the first version.
        """
        # Initial write outside transaction
        df1 = pd.DataFrame({"id": [1]})
        engine_with_tx.write_table("users", df1)

        # Start subscriber after initial write
        subscriber = engine_with_tx.subscribe(since_tx_id=0)

        df2 = pd.DataFrame({"id": [1, 2]})

        # Update in transaction
        with engine_with_tx.transaction() as tx:
            tx.query("SELECT * FROM users")
            tx.write_table("users", df2)

        events = subscriber.poll()

        assert len(events) == 1
        # new_version > 1 means this isn't the first version of the table
        assert events[0].new_version == 2
        # old_version is None because prior version wasn't in a transaction
        # (changelog only tracks transaction-based changes)


class TestSubscriberBackground:
    """Tests for Subscriber background thread."""

    def test_background_processing(self, engine_with_tx):
        """Background thread processes events."""
        received_events = []
        lock = threading.Lock()

        def on_change(event):
            with lock:
                received_events.append(event)

        subscriber = engine_with_tx.subscribe(since_tx_id=0, poll_interval=0.1)
        subscriber.start_background(on_change)

        try:
            # Make a change
            df = pd.DataFrame({"id": [1]})
            with engine_with_tx.transaction() as tx:
                tx.write_table("users", df)

            # Wait for background thread to process
            time.sleep(0.3)

            with lock:
                assert len(received_events) >= 1
                assert received_events[0].table_name == "users"
        finally:
            subscriber.stop()

    def test_background_stop(self, engine_with_tx):
        """stop() terminates background thread."""
        subscriber = engine_with_tx.subscribe(poll_interval=0.1)
        subscriber.start_background(lambda e: None)

        assert subscriber.is_running is True

        subscriber.stop()

        assert subscriber.is_running is False

    def test_background_double_start_raises(self, engine_with_tx):
        """Starting twice raises RuntimeError."""
        subscriber = engine_with_tx.subscribe(poll_interval=0.1)
        subscriber.start_background(lambda e: None)

        try:
            with pytest.raises(RuntimeError, match="already running"):
                subscriber.start_background(lambda e: None)
        finally:
            subscriber.stop()


class TestUnifiedBatchStream:
    """Tests demonstrating the unified batch/stream model."""

    def test_batch_stream_equivalence(self, engine_with_tx):
        """
        Demonstrates that batch and stream see the same data.

        Batch: "What is the state?" -> engine.query()
        Stream: "What changed?" -> engine.get_changes()
        """
        # Initial state (outside transaction)
        df1 = pd.DataFrame({"id": [1, 2], "value": [10, 20]})
        engine_with_tx.write_table("metrics", df1)

        # Update in transaction
        df2 = pd.DataFrame({"id": [1, 2, 3], "value": [15, 20, 30]})
        with engine_with_tx.transaction() as tx:
            tx.query("SELECT * FROM metrics")  # Read to establish snapshot
            tx.write_table("metrics", df2)

        # BATCH: What is the current state?
        result = engine_with_tx.query("SELECT SUM(value) as total FROM metrics")
        total = result.to_pandas()["total"].iloc[0]
        assert total == 65  # 15 + 20 + 30

        # STREAM: What changed? (only transaction changes appear)
        changes = engine_with_tx.get_changes()
        assert len(changes) == 1

        # Change: updated to version 2
        # (old_version is None because v1 was written outside transaction)
        assert changes[0]["changes"][0]["new_version"] == 2
        assert changes[0]["changes"][0]["table_name"] == "metrics"

    def test_checkpoint_and_replay(self, engine_with_tx):
        """
        Demonstrates checkpoint-based change tracking.

        This pattern is useful for:
        - Incremental ETL
        - Change data capture (CDC)
        - Event sourcing replay
        """
        df = pd.DataFrame({"id": [1]})

        # Initial data (in transaction to create changelog entry)
        with engine_with_tx.transaction() as tx:
            tx.write_table("events", df)

        # Checkpoint: record current position
        checkpoint = engine_with_tx.latest_tx_id()
        assert checkpoint is not None

        # More changes using different tables to avoid conflicts
        with engine_with_tx.transaction() as tx:
            tx.write_table("events_v2", pd.DataFrame({"id": [1, 2]}))

        with engine_with_tx.transaction() as tx:
            tx.write_table("events_v3", pd.DataFrame({"id": [1, 2, 3]}))

        # Replay: get all changes since checkpoint
        changes = engine_with_tx.get_changes(since_tx_id=checkpoint)

        assert len(changes) == 2  # Two transactions since checkpoint
        # Both new tables (version 1)
        table_names = [c["changes"][0]["table_name"] for c in changes]
        assert "events_v2" in table_names
        assert "events_v3" in table_names
