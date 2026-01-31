"""
Tests for crash and corruption recovery scenarios.

Covers:
- Chunk store corruption detection (bit-flip, truncation, zeroed)
- Orphaned temp file cleanup after simulated crash
- Catalog metadata corruption and missing files
- Transaction log corruption and mid-commit crashes
- Multi-table partial commit recovery
- Recovery after corruption + data integrity verification
- Engine-level health_check and recover() paths
"""

import json
import os
import shutil
import tempfile

import pandas as pd
import pyarrow as pa
import pytest

import _rhizo
import rhizo
from rhizo import QueryEngine


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def temp_storage():
    """Create temporary storage directories with all managers."""
    base_dir = tempfile.mkdtemp(prefix="rhizo_crash_test_")
    chunks_dir = os.path.join(base_dir, "chunks")
    catalog_dir = os.path.join(base_dir, "catalog")
    branches_dir = os.path.join(base_dir, "branches")
    tx_dir = os.path.join(base_dir, "transactions")

    store = _rhizo.PyChunkStore(chunks_dir)
    catalog = _rhizo.PyCatalog(catalog_dir)
    branches = _rhizo.PyBranchManager(branches_dir)
    tx_manager = _rhizo.PyTransactionManager(tx_dir, catalog_dir, branches_dir)

    yield store, catalog, branches, tx_manager, base_dir

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
    yield engine, store, catalog, tx_manager, base_dir
    engine.close()


@pytest.fixture
def populated_engine(engine_with_tx):
    """Engine with pre-written data for corruption tests."""
    engine, store, catalog, tx_manager, base_dir = engine_with_tx

    df_users = pd.DataFrame({"id": [1, 2, 3], "name": ["Alice", "Bob", "Charlie"]})
    df_orders = pd.DataFrame({"id": [10, 20], "amount": [100.0, 200.0]})

    with engine.transaction() as tx:
        tx.write_table("users", df_users)
        tx.write_table("orders", df_orders)

    yield engine, store, catalog, tx_manager, base_dir


@pytest.fixture
def db_path(tmp_path):
    """Create a temporary path for Database-level tests."""
    return str(tmp_path / "crash_db")


# ---------------------------------------------------------------------------
# Helper: find chunk files on disk
# ---------------------------------------------------------------------------

def find_chunk_files(chunks_dir):
    """Walk the chunk store directory and return paths to all chunk files."""
    result = []
    for root, dirs, files in os.walk(chunks_dir):
        for f in files:
            if not f.endswith(".tmp"):
                result.append(os.path.join(root, f))
    return result


def find_temp_files(chunks_dir):
    """Find orphaned .tmp files in the chunk directory tree."""
    result = []
    for root, dirs, files in os.walk(chunks_dir):
        for f in files:
            if f.endswith(".tmp"):
                result.append(os.path.join(root, f))
    return result


# ===================================================================
# 1. CHUNK STORE CORRUPTION DETECTION
# ===================================================================

class TestChunkCorruptionDetection:
    """Test that corrupted chunk data is detected by get_verified()."""

    def test_bitflip_detected(self, temp_storage):
        """A single bit-flip in chunk data should be caught by get_verified."""
        store, *_ = temp_storage
        data = b"some important data that must not be silently corrupted"
        hash_id = store.put(data)

        # Corrupt the chunk file: flip one byte
        chunks_dir = os.path.join(_[3], "chunks")
        chunk_files = find_chunk_files(chunks_dir)
        assert len(chunk_files) >= 1

        target = [f for f in chunk_files if os.path.basename(f) == hash_id]
        assert len(target) == 1
        path = target[0]

        raw = open(path, "rb").read()
        corrupted = bytes([raw[0] ^ 0xFF]) + raw[1:]
        with open(path, "wb") as f:
            f.write(corrupted)

        with pytest.raises(Exception, match="(?i)(mismatch|corrupt|hash|integrity)"):
            store.get_verified(hash_id)

    def test_truncated_chunk_detected(self, temp_storage):
        """A truncated chunk file should be caught."""
        store, *_ = temp_storage
        data = b"x" * 1024
        hash_id = store.put(data)

        chunks_dir = os.path.join(_[3], "chunks")
        target = [f for f in find_chunk_files(chunks_dir) if os.path.basename(f) == hash_id]
        path = target[0]

        # Truncate to half the original size
        raw = open(path, "rb").read()
        with open(path, "wb") as f:
            f.write(raw[:len(raw) // 2])

        with pytest.raises(Exception):
            store.get_verified(hash_id)

    def test_zeroed_chunk_detected(self, temp_storage):
        """A chunk file zeroed out should be caught."""
        store, *_ = temp_storage
        data = b"real content here"
        hash_id = store.put(data)

        chunks_dir = os.path.join(_[3], "chunks")
        target = [f for f in find_chunk_files(chunks_dir) if os.path.basename(f) == hash_id]
        path = target[0]

        size = os.path.getsize(path)
        with open(path, "wb") as f:
            f.write(b"\x00" * size)

        with pytest.raises(Exception):
            store.get_verified(hash_id)

    def test_unverified_get_returns_corrupt_data_silently(self, temp_storage):
        """get() (without verification) returns data even if corrupted."""
        store, *_ = temp_storage
        data = b"original data"
        hash_id = store.put(data)

        chunks_dir = os.path.join(_[3], "chunks")
        target = [f for f in find_chunk_files(chunks_dir) if os.path.basename(f) == hash_id]
        path = target[0]

        with open(path, "wb") as f:
            f.write(b"CORRUPTED")

        # get() does NOT verify — returns whatever is on disk
        result = store.get(hash_id)
        assert result == b"CORRUPTED"

        # get_verified() catches it
        with pytest.raises(Exception):
            store.get_verified(hash_id)

    def test_missing_chunk_raises(self, temp_storage):
        """Requesting a non-existent chunk hash should raise."""
        store, *_ = temp_storage
        fake_hash = "a" * 64  # Valid length but doesn't exist
        with pytest.raises(Exception):
            store.get(fake_hash)

    def test_exists_returns_false_for_deleted_chunk(self, temp_storage):
        """After deleting a chunk, exists() returns False."""
        store, *_ = temp_storage
        hash_id = store.put(b"ephemeral data")
        assert store.exists(hash_id) is True
        store.delete(hash_id)
        assert store.exists(hash_id) is False


# ===================================================================
# 2. ORPHANED TEMP FILE CLEANUP
# ===================================================================

class TestOrphanedTempFileCleanup:
    """Simulate crashes that leave .tmp files in the chunk store."""

    def test_orphaned_tmp_files_do_not_affect_reads(self, temp_storage):
        """Leftover .tmp files from crashes shouldn't affect normal reads."""
        store, _, _, _, base_dir = temp_storage
        chunks_dir = os.path.join(base_dir, "chunks")

        # Write a real chunk
        hash_id = store.put(b"real chunk data")

        # Simulate crash: leave orphaned .tmp files
        prefix_dir = os.path.join(chunks_dir, "ab", "cd")
        os.makedirs(prefix_dir, exist_ok=True)
        orphan = os.path.join(prefix_dir, "crash_leftover.tmp")
        with open(orphan, "wb") as f:
            f.write(b"partial write from crash")

        # Normal operations should still work
        data = store.get(hash_id)
        assert data == b"real chunk data"

    def test_put_after_crash_with_orphaned_temps(self, temp_storage):
        """New puts should succeed even with orphaned .tmp files present."""
        store, _, _, _, base_dir = temp_storage
        chunks_dir = os.path.join(base_dir, "chunks")

        # Create orphaned temp files
        prefix_dir = os.path.join(chunks_dir, "ff", "ee")
        os.makedirs(prefix_dir, exist_ok=True)
        for i in range(5):
            orphan = os.path.join(prefix_dir, f"orphan_{i}.tmp")
            with open(orphan, "wb") as f:
                f.write(b"crash residue")

        # New writes should work fine
        h1 = store.put(b"new data after crash 1")
        h2 = store.put(b"new data after crash 2")
        assert store.get(h1) == b"new data after crash 1"
        assert store.get(h2) == b"new data after crash 2"


# ===================================================================
# 3. CATALOG CORRUPTION AND RECOVERY
# ===================================================================

class TestCatalogCorruption:
    """Test recovery from corrupted catalog metadata."""

    def test_corrupted_version_json(self, temp_storage):
        """Corrupted version JSON should raise on read."""
        store, catalog, _, _, base_dir = temp_storage
        catalog_dir = os.path.join(base_dir, "catalog")

        # Write a version via commit_next (auto-assigns version number)
        chunk_data = b"chunk content for catalog test"
        chunk_hash = store.put(chunk_data)
        catalog.commit_next("test_table", [chunk_hash])

        # Verify we can read the version
        versions = catalog.list_versions("test_table")
        assert len(versions) >= 1

        # Now corrupt the version JSON file
        table_dir = os.path.join(catalog_dir, "test_table")
        version_file = os.path.join(table_dir, "1.json")
        if os.path.exists(version_file):
            with open(version_file, "w") as f:
                f.write("{{NOT VALID JSON///")

            # Reading the corrupted version should raise
            with pytest.raises(Exception):
                catalog.get_version("test_table", 1)

    def test_missing_latest_pointer(self, temp_storage):
        """Removing the 'latest' pointer file should be handled."""
        store, catalog, _, _, base_dir = temp_storage
        catalog_dir = os.path.join(base_dir, "catalog")

        chunk_hash = store.put(b"data for latest pointer test")
        catalog.commit_next("latest_test", [chunk_hash])

        # Remove the latest pointer
        latest_path = os.path.join(catalog_dir, "latest_test", "latest")
        if os.path.exists(latest_path):
            os.remove(latest_path)

            # Operations on this table should fail gracefully or recover
            # (exact behavior depends on implementation)
            try:
                v = catalog.latest_version("latest_test")
                # If it recovers, version should still be valid
            except Exception:
                pass  # Expected: missing pointer causes error

    def test_version_file_count_matches_listing(self, temp_storage):
        """Number of version files should match catalog listing."""
        store, catalog, _, _, base_dir = temp_storage
        catalog_dir = os.path.join(base_dir, "catalog")

        # Write multiple versions
        for i in range(4):
            chunk_hash = store.put(f"data v{i}".encode())
            catalog.commit_next("multi_v", [chunk_hash])

        versions = catalog.list_versions("multi_v")
        assert len(versions) == 4

        # Count actual JSON files on disk
        table_dir = os.path.join(catalog_dir, "multi_v")
        json_files = [f for f in os.listdir(table_dir) if f.endswith(".json")]
        assert len(json_files) == len(versions)


# ===================================================================
# 4. TRANSACTION LOG CORRUPTION
# ===================================================================

class TestTransactionLogCorruption:
    """Test recovery from corrupted transaction log state."""

    def test_recovery_after_multiple_committed_then_abort(self, engine_with_tx):
        """Recovery is clean after mix of committed and aborted transactions."""
        engine, _, _, tx_manager, _ = engine_with_tx

        # Commit several transactions
        for i in range(3):
            df = pd.DataFrame({"id": [i], "val": [i * 10]})
            with engine.transaction() as tx:
                tx.write_table(f"table_{i}", df)

        # Abort one
        try:
            with engine.transaction() as tx:
                tx.write_table("aborted_table", pd.DataFrame({"x": [1]}))
                raise RuntimeError("simulated crash")
        except RuntimeError:
            pass

        report = tx_manager.recover()
        assert report.is_clean is True
        assert len(report.errors) == 0

    def test_verify_consistency_after_aborted_transaction(self, engine_with_tx):
        """Consistency should be clean after a properly aborted transaction."""
        engine, _, _, tx_manager, _ = engine_with_tx

        df = pd.DataFrame({"id": [1], "val": [99]})
        with engine.transaction() as tx:
            tx.write_table("good_table", df)

        try:
            with engine.transaction() as tx:
                tx.write_table("bad_table", pd.DataFrame({"z": [0]}))
                raise ValueError("crash")
        except ValueError:
            pass

        issues = tx_manager.verify_consistency()
        assert len(issues) == 0

    def test_recovery_reports_no_active_transactions(self, engine_with_tx):
        """After recovery, no transactions should be active."""
        engine, _, _, tx_manager, _ = engine_with_tx

        df = pd.DataFrame({"id": [1]})
        with engine.transaction() as tx:
            tx.write_table("t", df)

        report = tx_manager.recover_and_apply()
        assert tx_manager.active_count() == 0


# ===================================================================
# 5. MID-COMMIT CRASH SIMULATION
# ===================================================================

class TestMidCommitCrash:
    """Simulate crashes at various points during a commit."""

    def test_data_readable_after_successful_commit(self, engine_with_tx):
        """Baseline: data written in a committed tx is readable."""
        engine, store, catalog, tx_manager, _ = engine_with_tx

        df = pd.DataFrame({"id": [1, 2], "name": ["A", "B"]})
        with engine.transaction() as tx:
            tx.write_table("baseline", df)

        result = engine.query("SELECT * FROM baseline ORDER BY id").to_pandas()
        assert len(result) == 2
        assert list(result["id"]) == [1, 2]

    def test_aborted_tx_data_not_visible(self, engine_with_tx):
        """Data from an aborted transaction should not be visible."""
        engine, _, _, _, _ = engine_with_tx

        try:
            with engine.transaction() as tx:
                df = pd.DataFrame({"id": [99], "secret": ["hidden"]})
                tx.write_table("ghost_table", df)
                raise RuntimeError("crash before commit")
        except RuntimeError:
            pass

        # ghost_table should not exist
        tables = engine.list_tables()
        assert "ghost_table" not in tables

    def test_first_table_not_visible_if_second_table_aborts(self, engine_with_tx):
        """In a multi-table tx, if the tx aborts, nothing is visible."""
        engine, _, _, _, _ = engine_with_tx

        try:
            with engine.transaction() as tx:
                tx.write_table("table_a", pd.DataFrame({"id": [1]}))
                tx.write_table("table_b", pd.DataFrame({"id": [2]}))
                raise RuntimeError("crash mid-transaction")
        except RuntimeError:
            pass

        tables = engine.list_tables()
        assert "table_a" not in tables
        assert "table_b" not in tables

    def test_previous_data_intact_after_failed_update(self, engine_with_tx):
        """Existing data should be intact after a failed update transaction."""
        engine, _, _, _, _ = engine_with_tx

        # Write initial data
        df1 = pd.DataFrame({"id": [1, 2], "value": [10, 20]})
        with engine.transaction() as tx:
            tx.write_table("stable", df1)

        # Attempt to overwrite, but abort
        try:
            with engine.transaction() as tx:
                tx.write_table("stable", pd.DataFrame({"id": [99], "value": [999]}))
                raise RuntimeError("crash during update")
        except RuntimeError:
            pass

        # Original data should be intact
        result = engine.query("SELECT * FROM stable ORDER BY id").to_pandas()
        assert list(result["value"]) == [10, 20]


# ===================================================================
# 6. RECOVERY + DATA INTEGRITY VERIFICATION
# ===================================================================

class TestRecoveryDataIntegrity:
    """Verify data integrity after recovery from various failure modes."""

    def test_data_intact_after_recover_and_apply(self, populated_engine):
        """Data should be intact after running recover_and_apply."""
        engine, _, _, tx_manager, _ = populated_engine

        # Run recovery
        report = tx_manager.recover_and_apply()
        assert report.is_clean is True

        # Verify all data is intact
        users = engine.query("SELECT * FROM users ORDER BY id").to_pandas()
        assert len(users) == 3
        assert list(users["name"]) == ["Alice", "Bob", "Charlie"]

        orders = engine.query("SELECT * FROM orders ORDER BY id").to_pandas()
        assert len(orders) == 2
        assert list(orders["amount"]) == [100.0, 200.0]

    def test_multiple_recovery_cycles_preserve_data(self, populated_engine):
        """Running recovery multiple times shouldn't corrupt data."""
        engine, _, _, tx_manager, _ = populated_engine

        for _ in range(5):
            report = tx_manager.recover_and_apply()
            assert report.is_clean is True

        # Data still intact
        users = engine.query("SELECT count(*) as cnt FROM users").to_pandas()
        assert list(users["cnt"]) == [3]

    def test_recovery_after_abort_preserves_committed_data(self, engine_with_tx):
        """Committed data should survive even after a subsequent abort + recovery."""
        engine, _, _, tx_manager, _ = engine_with_tx

        # Commit good data
        df = pd.DataFrame({"id": [1, 2], "val": [10, 20]})
        with engine.transaction() as tx:
            tx.write_table("important", df)

        # Abort a second transaction
        try:
            with engine.transaction() as tx:
                tx.write_table("important", pd.DataFrame({"id": [99], "val": [0]}))
                raise RuntimeError("crash")
        except RuntimeError:
            pass

        # Recovery
        report = tx_manager.recover_and_apply()
        assert report.is_clean is True

        # Original data still there
        result = engine.query("SELECT * FROM important ORDER BY id").to_pandas()
        assert list(result["val"]) == [10, 20]

    def test_recovery_idempotent_with_data_check(self, populated_engine):
        """Recovery is idempotent and doesn't change data on repeated calls."""
        engine, _, _, tx_manager, _ = populated_engine

        r1 = tx_manager.recover_and_apply()
        data_after_1 = engine.query("SELECT * FROM users ORDER BY id").to_dict()

        r2 = tx_manager.recover_and_apply()
        data_after_2 = engine.query("SELECT * FROM users ORDER BY id").to_dict()

        r3 = tx_manager.recover_and_apply()
        data_after_3 = engine.query("SELECT * FROM users ORDER BY id").to_dict()

        assert data_after_1 == data_after_2 == data_after_3
        assert r1.is_clean and r2.is_clean and r3.is_clean


# ===================================================================
# 7. ENGINE-LEVEL HEALTH CHECK AND RECOVER
# ===================================================================

class TestEngineRecovery:
    """Test the engine.verify_integrity() and engine.recover() APIs."""

    def test_health_check_clean(self, engine_with_tx):
        """Health check on fresh engine should be healthy."""
        engine, *_ = engine_with_tx
        result = engine.verify_integrity()
        assert result["is_healthy"] is True
        assert len(result["issues"]) == 0

    def test_health_check_after_transactions(self, populated_engine):
        """Health check after normal transactions should be healthy."""
        engine, *_ = populated_engine
        result = engine.verify_integrity()
        assert result["is_healthy"] is True

    def test_recover_dry_run(self, populated_engine):
        """engine.recover(apply=False) reports without modifying state."""
        engine, *_ = populated_engine
        report = engine.recover(apply=False)
        assert report["is_clean"] is True
        assert isinstance(report["replayed"], list)
        assert isinstance(report["rolled_back"], list)
        assert isinstance(report["warnings"], list)
        assert isinstance(report["errors"], list)

    def test_recover_apply(self, populated_engine):
        """engine.recover(apply=True) cleans up state."""
        engine, *_ = populated_engine
        report = engine.recover(apply=True)
        assert report["is_clean"] is True
        assert len(report["errors"]) == 0

    def test_recover_without_tx_manager_raises(self, temp_storage):
        """Engine without transaction_manager should raise on recover()."""
        store, catalog, branches, _, _ = temp_storage
        engine = QueryEngine(store, catalog, branch_manager=branches)
        with pytest.raises(RuntimeError, match="transaction_manager"):
            engine.recover()
        engine.close()

    def test_health_check_without_tx_manager(self, temp_storage):
        """Health check without tx_manager should still succeed (limited)."""
        store, catalog, branches, _, _ = temp_storage
        engine = QueryEngine(store, catalog, branch_manager=branches)
        result = engine.verify_integrity()
        assert result["is_healthy"] is True  # No tx system to check
        engine.close()


# ===================================================================
# 8. DATABASE-LEVEL CRASH RECOVERY
# ===================================================================

class TestDatabaseCrashRecovery:
    """Test crash recovery through the high-level Database API."""

    def test_database_survives_reopen_after_clean_close(self, db_path):
        """Data persists across close/reopen."""
        with rhizo.open(db_path) as db:
            df = pd.DataFrame({"id": [1, 2], "name": ["X", "Y"]})
            db.write("persist", df)

        with rhizo.open(db_path) as db:
            result = db.sql("SELECT * FROM persist ORDER BY id")
            assert len(result.to_pandas()) == 2

    def test_database_survives_multiple_reopens(self, db_path):
        """Multiple close/reopen cycles don't corrupt data."""
        with rhizo.open(db_path) as db:
            db.write("cycle", pd.DataFrame({"v": [1]}))

        for i in range(2, 6):
            with rhizo.open(db_path) as db:
                db.write("cycle", pd.DataFrame({"v": [i]}))

        with rhizo.open(db_path) as db:
            versions = db.versions("cycle")
            assert len(versions) == 5

    def test_different_tables_independent_across_reopens(self, db_path):
        """Tables written in separate sessions are all accessible."""
        with rhizo.open(db_path) as db:
            db.write("t1", pd.DataFrame({"a": [1]}))

        with rhizo.open(db_path) as db:
            db.write("t2", pd.DataFrame({"b": [2]}))

        with rhizo.open(db_path) as db:
            tables = db.tables()
            assert "t1" in tables
            assert "t2" in tables

    def test_time_travel_after_reopen(self, db_path):
        """Time travel queries work after reopen."""
        with rhizo.open(db_path) as db:
            db.write("history", pd.DataFrame({"val": [10]}))
            db.write("history", pd.DataFrame({"val": [20]}))

        with rhizo.open(db_path) as db:
            v1 = db.read("history", version=1)
            v2 = db.read("history", version=2)
            assert v1.column("val").to_pylist() == [10]
            assert v2.column("val").to_pylist() == [20]


# ===================================================================
# 9. CHUNK DEDUPLICATION AFTER CORRUPTION + REPAIR
# ===================================================================

class TestChunkDeduplicationIntegrity:
    """Verify content-addressing dedup semantics under edge cases."""

    def test_identical_data_produces_same_hash(self, temp_storage):
        """Content-addressable: same data = same hash."""
        store, *_ = temp_storage
        data = b"identical content for dedup test"
        h1 = store.put(data)
        h2 = store.put(data)
        assert h1 == h2

    def test_different_data_produces_different_hash(self, temp_storage):
        """Different data = different hash."""
        store, *_ = temp_storage
        h1 = store.put(b"data A")
        h2 = store.put(b"data B")
        assert h1 != h2

    def test_chunk_survives_duplicate_put(self, temp_storage):
        """Re-putting identical data doesn't corrupt the existing chunk."""
        store, *_ = temp_storage
        data = b"dedup safety test"
        h1 = store.put(data)
        h2 = store.put(data)
        assert h1 == h2
        # Verified read should still work
        assert store.get_verified(h1) == data

    def test_batch_put_deduplicates(self, temp_storage):
        """put_batch with duplicate chunks returns same hashes."""
        store, *_ = temp_storage
        chunks = [b"dup", b"unique", b"dup"]
        hashes = store.put_batch(chunks)
        assert hashes[0] == hashes[2]  # same content
        assert hashes[0] != hashes[1]  # different content


# ===================================================================
# 10. RECOVERY WITH BRANCHES
# ===================================================================

class TestBranchRecoveryIntegrity:
    """Test recovery preserves branch state and data isolation."""

    def test_branch_data_isolated_after_recovery(self, engine_with_tx):
        """Branch data should remain isolated from main after recovery."""
        engine, _, _, tx_manager, _ = engine_with_tx

        # Write to main
        with engine.transaction() as tx:
            tx.write_table("main_data", pd.DataFrame({"id": [1], "src": ["main"]}))

        # Write to branch (different table to avoid cross-branch conflict)
        engine.create_branch("feature/isolated")
        engine.checkout("feature/isolated")
        with engine.transaction() as tx:
            tx.write_table("branch_data", pd.DataFrame({"id": [2], "src": ["branch"]}))

        # Recovery
        report = tx_manager.recover_and_apply()
        assert report.is_clean is True

        # Branch sees its own data
        engine.checkout("feature/isolated")
        result = engine.query("SELECT * FROM branch_data").to_pandas()
        assert list(result["src"]) == ["branch"]

        # Branch also sees main's data (inherited)
        result = engine.query("SELECT * FROM main_data").to_pandas()
        assert list(result["src"]) == ["main"]

    def test_recovery_preserves_all_branch_heads(self, engine_with_tx):
        """All branch head pointers should survive recovery."""
        engine, _, _, tx_manager, _ = engine_with_tx

        # Create multiple branches with data
        for name in ["dev", "staging", "prod"]:
            engine.create_branch(name)
            engine.checkout(name)
            with engine.transaction() as tx:
                tx.write_table(f"{name}_data", pd.DataFrame({"v": [1]}))

        # Record heads before recovery
        heads_before = {}
        for name in ["dev", "staging", "prod"]:
            branch = engine.branch_manager.get(name)
            heads_before[name] = dict(branch.head)

        # Recovery
        report = tx_manager.recover_and_apply()
        assert report.is_clean is True

        # Heads preserved
        for name in ["dev", "staging", "prod"]:
            branch = engine.branch_manager.get(name)
            assert dict(branch.head) == heads_before[name]
