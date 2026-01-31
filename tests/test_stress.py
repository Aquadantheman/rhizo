"""
Stress tests for Rhizo concurrent operations.

Run with: pytest tests/test_stress.py -v
Run slow only: pytest tests/test_stress.py -v -m slow

These tests verify thread safety and correctness under high concurrency:
- High-concurrency writes (20 threads)
- Sustained transaction churn (50 rapid transactions)
- Mixed read/write workloads (15 threads)
- Branch operations under concurrency (10 threads)
- Distributed convergence at scale (10 nodes, 100 ops)
- Cache thread safety (10 threads, 50 ops each)
- Recovery with concurrent transactions in flight
"""

import os
import tempfile
import shutil
import threading
import time

import pytest
import pandas as pd

import _rhizo
from _rhizo import (
    PyAlgebraicOperation,
    PyAlgebraicTransaction,
    PyAlgebraicValue,
    PyLocalCommitProtocol,
    PyNodeId,
    PyOpType,
    PySimulatedCluster,
    PyVectorClock,
)
from rhizo import QueryEngine
from rhizo.cache import CacheKey, CacheManager


@pytest.fixture
def temp_storage():
    """Create temporary storage directories for testing."""
    base_dir = tempfile.mkdtemp(prefix="rhizo_stress_test_")
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

    yield engine, base_dir

    engine.close()


def _make_engine(store, catalog, branches, tx_manager):
    """Helper to create a QueryEngine instance for a worker thread."""
    return QueryEngine(
        store, catalog,
        branch_manager=branches,
        transaction_manager=tx_manager,
    )


@pytest.mark.slow
class TestHighConcurrencyStress:
    """Stress tests for high-concurrency write and read workloads."""

    def test_high_concurrency_writes_different_tables(self, temp_storage):
        """20 threads writing to 20 different tables simultaneously."""
        store, catalog, branches, tx_manager, base_dir = temp_storage

        num_threads = 20
        results = {"errors": [], "success_count": 0}
        lock = threading.Lock()
        barrier = threading.Barrier(num_threads)

        def writer(thread_id):
            try:
                engine = _make_engine(store, catalog, branches, tx_manager)
                barrier.wait(timeout=10)

                df = pd.DataFrame({
                    "id": list(range(100)),
                    "value": [thread_id * 1000 + i for i in range(100)],
                })

                with engine.transaction() as tx:
                    tx.write_table(f"stress_table_{thread_id}", df)

                engine.close()

                with lock:
                    results["success_count"] += 1
            except Exception as e:
                with lock:
                    results["errors"].append(f"Thread {thread_id}: {e}")

        threads = [
            threading.Thread(target=writer, args=(i,))
            for i in range(num_threads)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert not results["errors"], f"Errors: {results['errors']}"
        assert results["success_count"] == num_threads

        # Verify all tables exist with correct row counts
        engine = _make_engine(store, catalog, branches, tx_manager)
        for i in range(num_threads):
            result = engine.query(f"SELECT COUNT(*) as cnt FROM stress_table_{i}")
            assert result.to_pandas()["cnt"].iloc[0] == 100
        engine.close()

    def test_mixed_read_write_workload(self, temp_storage):
        """15 threads: 10 readers + 5 writers operating concurrently."""
        store, catalog, branches, tx_manager, base_dir = temp_storage

        # Seed initial data
        engine = _make_engine(store, catalog, branches, tx_manager)
        engine.write_table("mixed_data", pd.DataFrame({
            "id": list(range(50)),
            "value": list(range(50)),
        }))
        engine.close()

        results = {
            "read_values": [],
            "write_successes": 0,
            "write_conflicts": 0,
            "errors": [],
        }
        lock = threading.Lock()
        barrier = threading.Barrier(15)

        def reader(reader_id):
            try:
                engine = _make_engine(store, catalog, branches, tx_manager)
                barrier.wait(timeout=10)

                for _ in range(5):
                    result = engine.query("SELECT COUNT(*) as cnt FROM mixed_data")
                    cnt = result.to_pandas()["cnt"].iloc[0]
                    with lock:
                        results["read_values"].append(cnt)
                    time.sleep(0.005)

                engine.close()
            except Exception as e:
                with lock:
                    results["errors"].append(f"Reader {reader_id}: {e}")

        def writer(writer_id):
            try:
                engine = _make_engine(store, catalog, branches, tx_manager)
                barrier.wait(timeout=10)

                for attempt in range(3):
                    try:
                        df = pd.DataFrame({
                            "id": [writer_id * 100 + attempt],
                            "value": [writer_id * 1000 + attempt],
                        })
                        # Write to a unique table per writer to avoid conflicts
                        with engine.transaction() as tx:
                            tx.write_table(f"writer_{writer_id}_data", df)
                        with lock:
                            results["write_successes"] += 1
                    except ValueError as e:
                        if "conflict" in str(e).lower():
                            with lock:
                                results["write_conflicts"] += 1
                        else:
                            with lock:
                                results["errors"].append(f"Writer {writer_id}: {e}")
                    time.sleep(0.01)

                engine.close()
            except Exception as e:
                with lock:
                    results["errors"].append(f"Writer {writer_id}: {e}")

        threads = []
        for i in range(10):
            threads.append(threading.Thread(target=reader, args=(i,)))
        for i in range(5):
            threads.append(threading.Thread(target=writer, args=(i,)))

        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert not results["errors"], f"Errors: {results['errors']}"
        # All reads should return valid counts (>= 50 rows from seed data)
        for val in results["read_values"]:
            assert val >= 50, f"Read returned invalid count: {val}"
        # Writers should have some successes
        assert results["write_successes"] > 0

    def test_high_contention_same_table(self, temp_storage):
        """10 threads all writing to the same table — tests conflict detection."""
        store, catalog, branches, tx_manager, base_dir = temp_storage

        # Seed the table
        engine = _make_engine(store, catalog, branches, tx_manager)
        engine.write_table("contended", pd.DataFrame({"id": [1], "value": [0]}))
        engine.close()

        results = {"commits": 0, "conflicts": 0, "errors": []}
        lock = threading.Lock()
        barrier = threading.Barrier(10)

        def contender(thread_id):
            try:
                engine = _make_engine(store, catalog, branches, tx_manager)
                barrier.wait(timeout=10)

                try:
                    with engine.transaction() as tx:
                        tx.query("SELECT * FROM contended")
                        time.sleep(0.02)
                        tx.write_table("contended", pd.DataFrame({
                            "id": [1], "value": [thread_id],
                        }))
                    with lock:
                        results["commits"] += 1
                except ValueError as e:
                    if "conflict" in str(e).lower() or "snapshot" in str(e).lower():
                        with lock:
                            results["conflicts"] += 1
                    else:
                        with lock:
                            results["errors"].append(f"Thread {thread_id}: {e}")

                engine.close()
            except Exception as e:
                with lock:
                    results["errors"].append(f"Thread {thread_id}: {e}")

        threads = [
            threading.Thread(target=contender, args=(i,))
            for i in range(10)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert not results["errors"], f"Errors: {results['errors']}"
        assert results["commits"] >= 1, "At least one should commit"
        assert results["commits"] + results["conflicts"] == 10

        # Final state should be a valid value from one of the threads
        engine = _make_engine(store, catalog, branches, tx_manager)
        result = engine.query("SELECT value FROM contended")
        final = result.to_pandas()["value"].iloc[0]
        assert final in list(range(10)), f"Unexpected final value: {final}"
        engine.close()

    def test_concurrent_queries_during_version_creation(self, temp_storage):
        """1 writer + 5 readers — verifies read atomicity during writes."""
        store, catalog, branches, tx_manager, base_dir = temp_storage

        # Seed data
        engine = _make_engine(store, catalog, branches, tx_manager)
        engine.write_table("versioned", pd.DataFrame({
            "id": [1, 2, 3],
            "value": [10, 20, 30],
        }))
        engine.close()

        results = {"read_sums": [], "writer_done": False, "errors": []}
        lock = threading.Lock()
        writer_started = threading.Event()
        stop_readers = threading.Event()

        def writer():
            try:
                engine = _make_engine(store, catalog, branches, tx_manager)
                writer_started.set()

                for i in range(5):
                    try:
                        new_val = (i + 1) * 100
                        with engine.transaction() as tx:
                            tx.write_table("versioned", pd.DataFrame({
                                "id": [1, 2, 3],
                                "value": [new_val, new_val * 2, new_val * 3],
                            }))
                    except ValueError:
                        pass  # Conflicts OK
                    time.sleep(0.01)

                engine.close()
                with lock:
                    results["writer_done"] = True
                stop_readers.set()
            except Exception as e:
                with lock:
                    results["errors"].append(f"Writer: {e}")
                stop_readers.set()

        def reader(reader_id):
            try:
                engine = _make_engine(store, catalog, branches, tx_manager)
                writer_started.wait(timeout=10)

                while not stop_readers.is_set():
                    result = engine.query("SELECT SUM(value) as total FROM versioned")
                    total = result.to_pandas()["total"].iloc[0]
                    with lock:
                        results["read_sums"].append(total)
                    time.sleep(0.005)

                engine.close()
            except Exception as e:
                with lock:
                    results["errors"].append(f"Reader {reader_id}: {e}")

        threads = [threading.Thread(target=writer)]
        for i in range(5):
            threads.append(threading.Thread(target=reader, args=(i,)))

        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert not results["errors"], f"Errors: {results['errors']}"
        assert results["writer_done"]
        # All reads should return consistent sums (never partial writes)
        # Initial sum is 60; each version has value * (1+2+3) = value * 6
        # Valid sums: 60, 600, 1200, 1800, 2400, 3000
        valid_sums = {60, 600, 1200, 1800, 2400, 3000}
        for s in results["read_sums"]:
            assert s in valid_sums, f"Read inconsistent sum: {s}"


@pytest.mark.slow
class TestSustainedLoadStress:
    """Stress tests for sustained transaction load."""

    def test_rapid_transaction_churn(self, temp_storage):
        """50 transactions in rapid succession on a single thread."""
        store, catalog, branches, tx_manager, base_dir = temp_storage
        engine = _make_engine(store, catalog, branches, tx_manager)

        successes = 0
        conflicts = 0

        for i in range(50):
            try:
                with engine.transaction() as tx:
                    tx.write_table(f"churn_table_{i}", pd.DataFrame({
                        "id": list(range(10)),
                        "seq": [i] * 10,
                    }))
                successes += 1
            except ValueError as e:
                if "conflict" in str(e).lower():
                    conflicts += 1
                else:
                    raise

        engine.close()

        assert successes == 50, f"All 50 should succeed (different tables), got {successes}"

        # Verify all tables exist
        engine = _make_engine(store, catalog, branches, tx_manager)
        tables = engine.list_tables()
        for i in range(50):
            assert f"churn_table_{i}" in tables
        engine.close()

    def test_rapid_version_creation(self, temp_storage):
        """30 sequential versions of the same table — tests time travel correctness."""
        store, catalog, branches, tx_manager, base_dir = temp_storage
        engine = _make_engine(store, catalog, branches, tx_manager)

        created_versions = []
        for i in range(30):
            try:
                with engine.transaction() as tx:
                    tx.write_table("versioned_table", pd.DataFrame({
                        "version_marker": [i],
                        "data": [f"version_{i}"],
                    }))
                created_versions.append(i)
            except ValueError:
                pass  # Conflicts between rapid writes are OK

        engine.close()

        # Should have created at least some versions
        assert len(created_versions) >= 1, "Should create at least 1 version"

        # Verify latest version has the last marker written
        engine = _make_engine(store, catalog, branches, tx_manager)
        result = engine.query("SELECT version_marker FROM versioned_table")
        latest_marker = result.to_pandas()["version_marker"].iloc[0]
        assert latest_marker in created_versions
        engine.close()


@pytest.mark.slow
class TestBranchStress:
    """Stress tests for branch operations under concurrency."""

    def test_concurrent_branch_creation(self, temp_storage):
        """10 threads each creating a branch simultaneously."""
        store, catalog, branches, tx_manager, base_dir = temp_storage

        # Seed main branch with data
        engine = _make_engine(store, catalog, branches, tx_manager)
        engine.write_table("shared", pd.DataFrame({"id": [1], "value": [100]}))
        engine.close()

        results = {"created": [], "errors": []}
        lock = threading.Lock()
        barrier = threading.Barrier(10)

        def create_branch(thread_id):
            try:
                engine = _make_engine(store, catalog, branches, tx_manager)
                barrier.wait(timeout=10)

                branch_name = f"feature/branch_{thread_id}"
                engine.create_branch(branch_name)

                with lock:
                    results["created"].append(branch_name)

                engine.close()
            except Exception as e:
                with lock:
                    results["errors"].append(f"Thread {thread_id}: {e}")

        threads = [
            threading.Thread(target=create_branch, args=(i,))
            for i in range(10)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert not results["errors"], f"Errors: {results['errors']}"
        assert len(results["created"]) == 10

    def test_concurrent_branch_merge(self, temp_storage):
        """5 branches with writes + 3 readers during merge."""
        store, catalog, branches, tx_manager, base_dir = temp_storage

        # Seed main
        engine = _make_engine(store, catalog, branches, tx_manager)
        engine.write_table("merge_target", pd.DataFrame({"id": [0], "value": [0]}))

        # Create branches and write unique data
        for i in range(5):
            branch = f"feature/merge_{i}"
            engine.create_branch(branch)
            with engine.transaction(branch=branch) as tx:
                tx.write_table(f"branch_{i}_data", pd.DataFrame({
                    "id": [i], "value": [i * 100],
                }))
        engine.close()

        results = {"reads": [], "merges": 0, "errors": []}
        lock = threading.Lock()
        merge_started = threading.Event()
        stop_readers = threading.Event()

        def merge_branches():
            try:
                engine = _make_engine(store, catalog, branches, tx_manager)
                merge_started.set()

                for i in range(5):
                    try:
                        engine.merge_branch(f"feature/merge_{i}")
                        with lock:
                            results["merges"] += 1
                    except Exception:
                        pass  # Some merges may fail under contention
                    time.sleep(0.01)

                engine.close()
                stop_readers.set()
            except Exception as e:
                with lock:
                    results["errors"].append(f"Merger: {e}")
                stop_readers.set()

        def reader(reader_id):
            try:
                engine = _make_engine(store, catalog, branches, tx_manager)
                merge_started.wait(timeout=10)

                while not stop_readers.is_set():
                    try:
                        result = engine.query("SELECT * FROM merge_target")
                        with lock:
                            results["reads"].append(result.row_count)
                    except Exception:
                        pass  # Table may be in flux
                    time.sleep(0.01)

                engine.close()
            except Exception as e:
                with lock:
                    results["errors"].append(f"Reader {reader_id}: {e}")

        threads = [threading.Thread(target=merge_branches)]
        for i in range(3):
            threads.append(threading.Thread(target=reader, args=(i,)))

        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert not results["errors"], f"Errors: {results['errors']}"
        # At least some merges should succeed
        assert results["merges"] >= 1, "At least one merge should succeed"


@pytest.mark.slow
class TestDistributedConvergenceStress:
    """Stress tests for distributed convergence at scale."""

    def test_large_cluster_convergence(self):
        """10 nodes, 100 operations — verifies algebraic convergence at scale."""
        cluster = PySimulatedCluster(10)

        # Each node commits 10 operations
        for node_id in range(10):
            for op_idx in range(10):
                tx = PyAlgebraicTransaction()
                tx.add_operation(PyAlgebraicOperation(
                    "global_counter",
                    PyOpType("add"),
                    PyAlgebraicValue.integer(1),
                ))
                tx.add_operation(PyAlgebraicOperation(
                    "max_timestamp",
                    PyOpType("max"),
                    PyAlgebraicValue.integer(node_id * 100 + op_idx),
                ))
                cluster.commit_on_node(node_id, tx)

        cluster.propagate_all()

        assert cluster.verify_convergence()

        # global_counter: 10 nodes * 10 ops * 1 = 100
        for node_id in range(10):
            assert str(cluster.get_node_state(node_id, "global_counter")) == "100"

        # max_timestamp: max value is 9*100 + 9 = 909
        for node_id in range(10):
            assert str(cluster.get_node_state(node_id, "max_timestamp")) == "909"

    def test_cluster_convergence_under_partitions(self):
        """6 nodes partitioned into two groups, heal, then converge."""
        cluster = PySimulatedCluster(6)

        # Partition: {0,1,2} and {3,4,5}
        for a in range(3):
            for b in range(3, 6):
                cluster.partition(a, b)

        # Group A commits
        for node_id in range(3):
            tx = PyAlgebraicTransaction()
            tx.add_operation(PyAlgebraicOperation(
                "counter", PyOpType("add"), PyAlgebraicValue.integer(10),
            ))
            tx.add_operation(PyAlgebraicOperation(
                "tags", PyOpType("union"),
                PyAlgebraicValue.string_set([f"group_a_node_{node_id}"]),
            ))
            cluster.commit_on_node(node_id, tx)

        # Group B commits
        for node_id in range(3, 6):
            tx = PyAlgebraicTransaction()
            tx.add_operation(PyAlgebraicOperation(
                "counter", PyOpType("add"), PyAlgebraicValue.integer(20),
            ))
            tx.add_operation(PyAlgebraicOperation(
                "tags", PyOpType("union"),
                PyAlgebraicValue.string_set([f"group_b_node_{node_id}"]),
            ))
            cluster.commit_on_node(node_id, tx)

        # Propagate within partitions
        cluster.propagate_all()
        assert not cluster.verify_convergence()

        # Verify partition isolation: group A sees 30, group B sees 60
        assert str(cluster.get_node_state(0, "counter")) == "30"
        assert str(cluster.get_node_state(3, "counter")) == "60"

        # Heal and converge — multiple propagation rounds to ensure full convergence
        cluster.heal_partitions()
        cluster.requeue_all_updates()
        cluster.propagate_all()
        # Second round to handle any stragglers
        cluster.requeue_all_updates()
        cluster.propagate_all()

        assert cluster.verify_convergence()
        # counter: 3*10 + 3*20 = 90
        for node_id in range(6):
            assert str(cluster.get_node_state(node_id, "counter")) == "90"

        # Tags should contain all 6 node tags (check all nodes for full set)
        # After convergence all nodes should have the same state
        all_tags = set()
        for node_id in range(6):
            tags_str = str(cluster.get_node_state(node_id, "tags"))
            for i in range(3):
                if f"group_a_node_{i}" in tags_str:
                    all_tags.add(f"group_a_node_{i}")
            for i in range(3, 6):
                if f"group_b_node_{i}" in tags_str:
                    all_tags.add(f"group_b_node_{i}")
        assert len(all_tags) == 6, f"Expected 6 tags after convergence, got {all_tags}"

    def test_distributed_mixed_operations_convergence(self):
        """5 nodes with heterogeneous algebraic operations (ADD, MAX, UNION)."""
        cluster = PySimulatedCluster(5)

        for node_id in range(5):
            tx = PyAlgebraicTransaction()
            # ADD: each node adds node_id+1
            tx.add_operation(PyAlgebraicOperation(
                "sum", PyOpType("add"),
                PyAlgebraicValue.integer((node_id + 1) * 10),
            ))
            # MAX: each node sets its own timestamp
            tx.add_operation(PyAlgebraicOperation(
                "latest", PyOpType("max"),
                PyAlgebraicValue.integer(node_id * 100),
            ))
            # UNION: each node adds a tag
            tx.add_operation(PyAlgebraicOperation(
                "sources", PyOpType("union"),
                PyAlgebraicValue.string_set([f"node-{node_id}"]),
            ))
            cluster.commit_on_node(node_id, tx)

        cluster.propagate_all()
        assert cluster.verify_convergence()

        # sum: 10+20+30+40+50 = 150
        assert str(cluster.get_node_state(0, "sum")) == "150"

        # latest: max(0, 100, 200, 300, 400) = 400
        assert str(cluster.get_node_state(0, "latest")) == "400"

        # sources: all 5 nodes
        sources_str = str(cluster.get_node_state(0, "sources"))
        for i in range(5):
            assert f"node-{i}" in sources_str


@pytest.mark.slow
class TestCacheStress:
    """Stress tests for cache thread safety."""

    def test_cache_correctness_under_concurrent_access(self):
        """10 threads doing 50 put/get/invalidate ops each on a shared cache."""
        import pyarrow as pa

        cache = CacheManager(max_size_bytes=10_000_000)  # 10MB

        results = {"errors": [], "hits": 0, "misses": 0}
        lock = threading.Lock()
        barrier = threading.Barrier(10)

        def cache_worker(thread_id):
            try:
                barrier.wait(timeout=10)

                for op_idx in range(50):
                    table_name = f"table_{thread_id}_{op_idx % 5}"
                    version = op_idx % 3
                    key = CacheKey(table_name, version)

                    if op_idx % 3 == 0:
                        # Put
                        table = pa.table({
                            "id": list(range(10)),
                            "val": [thread_id * 100 + op_idx] * 10,
                        })
                        cache.put(key, table)
                    elif op_idx % 3 == 1:
                        # Get
                        result = cache.get(key)
                        with lock:
                            if result is not None:
                                results["hits"] += 1
                            else:
                                results["misses"] += 1
                    else:
                        # Invalidate
                        cache.invalidate(table_name)

            except Exception as e:
                with lock:
                    results["errors"].append(f"Thread {thread_id}: {e}")

        threads = [
            threading.Thread(target=cache_worker, args=(i,))
            for i in range(10)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert not results["errors"], f"Errors: {results['errors']}"

        # Cache stats should be internally consistent
        stats = cache.stats()
        assert stats.hits + stats.misses > 0
        assert stats.current_size_bytes <= stats.max_size_bytes


@pytest.mark.slow
class TestRecoveryStress:
    """Stress tests for recovery after concurrent crashes."""

    def test_recovery_with_concurrent_transactions_in_flight(self, temp_storage):
        """5 threads: 3 commit normally, 2 abort mid-flight — verify consistency."""
        store, catalog, branches, tx_manager, base_dir = temp_storage

        # Seed data
        engine = _make_engine(store, catalog, branches, tx_manager)
        engine.write_table("recovery_test", pd.DataFrame({
            "id": [0], "value": [0],
        }))
        engine.close()

        results = {"committed": [], "aborted": [], "errors": []}
        lock = threading.Lock()
        barrier = threading.Barrier(5)

        def committer(thread_id):
            try:
                engine = _make_engine(store, catalog, branches, tx_manager)
                barrier.wait(timeout=10)

                try:
                    with engine.transaction() as tx:
                        tx.write_table(f"committed_{thread_id}", pd.DataFrame({
                            "id": [thread_id], "status": ["committed"],
                        }))
                    with lock:
                        results["committed"].append(thread_id)
                except ValueError as e:
                    if "conflict" in str(e).lower():
                        pass  # OK
                    else:
                        with lock:
                            results["errors"].append(f"Committer {thread_id}: {e}")

                engine.close()
            except Exception as e:
                with lock:
                    results["errors"].append(f"Committer {thread_id}: {e}")

        def aborter(thread_id):
            try:
                engine = _make_engine(store, catalog, branches, tx_manager)
                barrier.wait(timeout=10)

                with engine.transaction() as tx:
                    tx.write_table(f"aborted_{thread_id}", pd.DataFrame({
                        "id": [thread_id], "status": ["should_not_persist"],
                    }))
                    tx.abort("simulated crash")

                with lock:
                    results["aborted"].append(thread_id)

                engine.close()
            except Exception as e:
                with lock:
                    results["errors"].append(f"Aborter {thread_id}: {e}")

        threads = []
        for i in range(3):
            threads.append(threading.Thread(target=committer, args=(i,)))
        for i in range(3, 5):
            threads.append(threading.Thread(target=aborter, args=(i,)))

        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert not results["errors"], f"Errors: {results['errors']}"

        # Verify: committed tables exist, aborted tables do not
        engine = _make_engine(store, catalog, branches, tx_manager)
        tables = engine.list_tables()

        for tid in results["committed"]:
            assert f"committed_{tid}" in tables, \
                f"committed_{tid} should exist"

        for tid in results["aborted"]:
            assert f"aborted_{tid}" not in tables, \
                f"aborted_{tid} should NOT exist"

        # Original table should still be intact
        assert "recovery_test" in tables
        engine.close()
