"""Tests for distributed systems primitives (coordination-free transactions)."""

import pytest
from _rhizo import (
    PyNodeId, PyVectorClock, PyCausalOrder,
    PyAlgebraicOperation, PyAlgebraicTransaction, PyVersionedUpdate,
    PyLocalCommitProtocol, PyOpType, PyAlgebraicValue, algebraic_merge,
)


class TestNodeId:
    """Tests for PyNodeId."""

    def test_create_node_id(self):
        """Test creating a node ID."""
        node = PyNodeId("test-node")
        assert str(node) == "test-node"

    def test_node_id_repr(self):
        """Test node ID representation."""
        node = PyNodeId("my-node")
        assert "my-node" in repr(node)

    def test_node_id_equality(self):
        """Test node ID equality."""
        node1 = PyNodeId("node-a")
        node2 = PyNodeId("node-a")
        node3 = PyNodeId("node-b")

        assert node1 == node2
        assert node1 != node3

    def test_node_id_hashable(self):
        """Test that node IDs can be used in sets/dicts."""
        node1 = PyNodeId("node-a")
        node2 = PyNodeId("node-a")
        node3 = PyNodeId("node-b")

        # Same ID should have same hash
        assert hash(node1) == hash(node2)

        # Can be used in set
        node_set = {node1, node2, node3}
        assert len(node_set) == 2  # node1 and node2 are duplicates


class TestVectorClock:
    """Tests for PyVectorClock."""

    def test_create_empty_clock(self):
        """Test creating an empty vector clock."""
        clock = PyVectorClock()
        assert clock.is_empty()
        assert clock.node_count() == 0

    def test_tick_increments(self):
        """Test that tick increments the clock."""
        node = PyNodeId("node-1")
        clock = PyVectorClock()

        assert clock.get(node) == 0

        clock.tick(node)
        assert clock.get(node) == 1

        clock.tick(node)
        assert clock.get(node) == 2

    def test_tick_only_affects_one_node(self):
        """Test that tick only affects the specified node."""
        node_a = PyNodeId("a")
        node_b = PyNodeId("b")
        clock = PyVectorClock()

        clock.tick(node_a)
        clock.tick(node_a)
        clock.tick(node_b)

        assert clock.get(node_a) == 2
        assert clock.get(node_b) == 1

    def test_with_node_constructor(self):
        """Test with_node static constructor."""
        node = PyNodeId("node-1")
        clock = PyVectorClock.with_node(node, 5)

        assert clock.get(node) == 5
        assert not clock.is_empty()

    def test_set_time(self):
        """Test setting time directly."""
        node = PyNodeId("node-1")
        clock = PyVectorClock()

        clock.set(node, 10)
        assert clock.get(node) == 10

    def test_merge_takes_max(self):
        """Test that merge takes component-wise maximum."""
        node_a = PyNodeId("a")
        node_b = PyNodeId("b")
        node_c = PyNodeId("c")

        clock1 = PyVectorClock()
        clock1.set(node_a, 3)
        clock1.set(node_b, 1)

        clock2 = PyVectorClock()
        clock2.set(node_a, 2)
        clock2.set(node_b, 4)
        clock2.set(node_c, 5)

        clock1.merge(clock2)

        assert clock1.get(node_a) == 3  # max(3, 2)
        assert clock1.get(node_b) == 4  # max(1, 4)
        assert clock1.get(node_c) == 5  # max(0, 5)

    def test_happened_before_simple(self):
        """Test happened_before for simple case."""
        node = PyNodeId("node-1")

        clock1 = PyVectorClock.with_node(node, 1)
        clock2 = PyVectorClock.with_node(node, 2)

        assert clock1.happened_before(clock2)
        assert not clock2.happened_before(clock1)
        assert not clock1.happened_before(clock1)

    def test_happened_after(self):
        """Test happened_after."""
        node = PyNodeId("node-1")

        clock1 = PyVectorClock.with_node(node, 1)
        clock2 = PyVectorClock.with_node(node, 2)

        assert clock2.happened_after(clock1)
        assert not clock1.happened_after(clock2)

    def test_concurrent_different_nodes(self):
        """Test concurrent detection for different nodes."""
        node_a = PyNodeId("a")
        node_b = PyNodeId("b")

        clock_a = PyVectorClock()
        clock_a.tick(node_a)

        clock_b = PyVectorClock()
        clock_b.tick(node_b)

        # These are concurrent
        assert clock_a.concurrent_with(clock_b)
        assert clock_b.concurrent_with(clock_a)
        assert not clock_a.happened_before(clock_b)
        assert not clock_b.happened_before(clock_a)

    def test_concurrent_crossed_updates(self):
        """Test concurrent detection for crossed updates."""
        node_a = PyNodeId("a")
        node_b = PyNodeId("b")

        clock1 = PyVectorClock()
        clock1.set(node_a, 2)
        clock1.set(node_b, 1)

        clock2 = PyVectorClock()
        clock2.set(node_a, 1)
        clock2.set(node_b, 2)

        # Neither dominates
        assert clock1.concurrent_with(clock2)
        assert clock2.concurrent_with(clock1)

    def test_compare_all_cases(self):
        """Test compare returns correct CausalOrder."""
        node_a = PyNodeId("a")
        node_b = PyNodeId("b")

        # Equal
        clock1 = PyVectorClock.with_node(node_a, 1)
        clock2 = PyVectorClock.with_node(node_a, 1)
        assert clock1.compare(clock2).order == "equal"

        # Before
        clock3 = PyVectorClock.with_node(node_a, 2)
        assert clock1.compare(clock3).order == "before"

        # After
        assert clock3.compare(clock1).order == "after"

        # Concurrent
        clock4 = PyVectorClock.with_node(node_b, 1)
        assert clock1.compare(clock4).order == "concurrent"

    def test_message_passing_scenario(self):
        """Test realistic message passing scenario."""
        node_a = PyNodeId("sf")
        node_b = PyNodeId("tokyo")

        # Node A does local work
        clock_a = PyVectorClock()
        clock_a.tick(node_a)
        clock_a.tick(node_a)

        # Node B does local work
        clock_b = PyVectorClock()
        clock_b.tick(node_b)

        # At this point, concurrent
        assert clock_a.concurrent_with(clock_b)

        # A sends message to B (B receives and merges)
        clock_b.merge(clock_a)
        clock_b.tick(node_b)

        # Now B is after A
        assert clock_a.happened_before(clock_b)
        assert clock_b.get(node_a) == 2
        assert clock_b.get(node_b) == 2

    def test_sum(self):
        """Test sum of all clock components."""
        clock = PyVectorClock()
        clock.set(PyNodeId("a"), 10)
        clock.set(PyNodeId("b"), 20)
        clock.set(PyNodeId("c"), 30)

        assert clock.sum() == 60

    def test_max_static(self):
        """Test static max method."""
        node_a = PyNodeId("a")
        node_b = PyNodeId("b")

        clock1 = PyVectorClock()
        clock1.set(node_a, 3)
        clock1.set(node_b, 1)

        clock2 = PyVectorClock()
        clock2.set(node_a, 2)
        clock2.set(node_b, 4)

        merged = PyVectorClock.max(clock1, clock2)

        # Original unchanged
        assert clock1.get(node_a) == 3
        assert clock1.get(node_b) == 1

        # Merged has max
        assert merged.get(node_a) == 3
        assert merged.get(node_b) == 4

    def test_ticked_returns_copy(self):
        """Test ticked returns a new clock."""
        node = PyNodeId("node-1")
        clock1 = PyVectorClock.with_node(node, 1)
        clock2 = clock1.ticked(node)

        # Original unchanged
        assert clock1.get(node) == 1
        # Copy incremented
        assert clock2.get(node) == 2

    def test_json_serialization(self):
        """Test JSON serialization roundtrip."""
        node_a = PyNodeId("a")
        node_b = PyNodeId("b")

        clock = PyVectorClock()
        clock.set(node_a, 42)
        clock.set(node_b, 17)

        json_str = clock.to_json()
        restored = PyVectorClock.from_json(json_str)

        assert clock == restored
        assert restored.get(node_a) == 42
        assert restored.get(node_b) == 17

    def test_equality(self):
        """Test clock equality."""
        node = PyNodeId("node-1")

        clock1 = PyVectorClock.with_node(node, 5)
        clock2 = PyVectorClock.with_node(node, 5)
        clock3 = PyVectorClock.with_node(node, 6)

        assert clock1 == clock2
        assert clock1 != clock3


class TestCausalOrder:
    """Tests for PyCausalOrder."""

    def test_needs_merge(self):
        """Test needs_merge for different orderings."""
        node_a = PyNodeId("a")
        node_b = PyNodeId("b")

        clock1 = PyVectorClock.with_node(node_a, 1)
        clock2 = PyVectorClock.with_node(node_b, 1)

        # Concurrent needs merge
        order = clock1.compare(clock2)
        assert order.needs_merge()

        # Sequential doesn't need merge
        clock3 = PyVectorClock.with_node(node_a, 2)
        order2 = clock1.compare(clock3)
        assert not order2.needs_merge()

    def test_should_apply(self):
        """Test should_apply logic."""
        node = PyNodeId("node-1")

        clock1 = PyVectorClock.with_node(node, 1)
        clock2 = PyVectorClock.with_node(node, 2)

        # clock1 is before clock2, so from clock1's perspective, apply clock2
        order = clock1.compare(clock2)
        assert order.should_apply()  # Other is newer

        # clock2 is after clock1, so from clock2's perspective, don't apply clock1
        order2 = clock2.compare(clock1)
        assert not order2.should_apply()  # We're newer

    def test_order_string_values(self):
        """Test that order attribute has expected values."""
        node_a = PyNodeId("a")
        node_b = PyNodeId("b")

        clock1 = PyVectorClock.with_node(node_a, 1)
        clock2 = PyVectorClock.with_node(node_a, 2)
        clock3 = PyVectorClock.with_node(node_b, 1)
        clock4 = PyVectorClock.with_node(node_a, 1)

        assert clock1.compare(clock2).order == "before"
        assert clock2.compare(clock1).order == "after"
        assert clock1.compare(clock3).order == "concurrent"
        assert clock1.compare(clock4).order == "equal"


class TestDistributedScenarios:
    """Integration tests for distributed scenarios."""

    def test_three_node_convergence(self):
        """Test that three nodes can track causality correctly."""
        node_sf = PyNodeId("san-francisco")
        node_ny = PyNodeId("new-york")
        node_tokyo = PyNodeId("tokyo")

        # Each node starts independently
        clock_sf = PyVectorClock()
        clock_sf.tick(node_sf)

        clock_ny = PyVectorClock()
        clock_ny.tick(node_ny)

        clock_tokyo = PyVectorClock()
        clock_tokyo.tick(node_tokyo)

        # All are concurrent
        assert clock_sf.concurrent_with(clock_ny)
        assert clock_ny.concurrent_with(clock_tokyo)
        assert clock_sf.concurrent_with(clock_tokyo)

        # NY receives from both SF and Tokyo
        clock_ny.merge(clock_sf)
        clock_ny.merge(clock_tokyo)
        clock_ny.tick(node_ny)

        # Now NY is after both
        assert clock_sf.happened_before(clock_ny)
        assert clock_tokyo.happened_before(clock_ny)

    def test_algebraic_merge_with_causality(self):
        """Test combining vector clocks with algebraic merge."""
        node_a = PyNodeId("a")
        node_b = PyNodeId("b")

        # Node A: increment counter
        clock_a = PyVectorClock()
        clock_a.tick(node_a)
        delta_a = PyAlgebraicValue.integer(5)

        # Node B: increment counter (concurrent)
        clock_b = PyVectorClock()
        clock_b.tick(node_b)
        delta_b = PyAlgebraicValue.integer(3)

        # Detect concurrent updates
        order = clock_a.compare(clock_b)
        assert order.order == "concurrent"
        assert order.needs_merge()

        # Merge using algebraic properties
        op_type = PyOpType("add")
        merged = algebraic_merge(op_type, delta_a, delta_b)

        # Result: 5 + 3 = 8
        assert str(merged) == "8"
        assert merged.is_numeric()


# ============================================================================
# Local Commit Protocol Tests
# ============================================================================


class TestAlgebraicOperation:
    """Tests for PyAlgebraicOperation."""

    def test_create_operation(self):
        """Test creating an algebraic operation."""
        op_type = PyOpType("add")
        value = PyAlgebraicValue.integer(5)
        op = PyAlgebraicOperation("counter", op_type, value)

        assert op.key == "counter"
        assert op.is_algebraic()

    def test_operation_with_different_types(self):
        """Test operations with different algebraic types."""
        # AbelianAdd
        op1 = PyAlgebraicOperation("x", PyOpType("add"), PyAlgebraicValue.integer(1))
        assert op1.is_algebraic()

        # SemilatticeMax
        op2 = PyAlgebraicOperation("y", PyOpType("max"), PyAlgebraicValue.integer(100))
        assert op2.is_algebraic()

        # SemilatticeUnion
        op3 = PyAlgebraicOperation("tags", PyOpType("union"), PyAlgebraicValue.string_set(["a", "b"]))
        assert op3.is_algebraic()

        # GenericOverwrite is NOT algebraic
        op4 = PyAlgebraicOperation("name", PyOpType("overwrite"), PyAlgebraicValue.integer(1))
        assert not op4.is_algebraic()

    def test_operation_repr(self):
        """Test operation repr."""
        op = PyAlgebraicOperation("counter", PyOpType("add"), PyAlgebraicValue.integer(42))
        r = repr(op)
        assert "counter" in r
        assert "42" in r


class TestAlgebraicTransaction:
    """Tests for PyAlgebraicTransaction."""

    def test_create_transaction(self):
        """Test creating an empty transaction."""
        tx = PyAlgebraicTransaction()
        assert tx.is_empty()
        assert tx.len() == 0

    def test_add_operations(self):
        """Test adding operations to a transaction."""
        tx = PyAlgebraicTransaction()
        tx.add_operation(PyAlgebraicOperation("a", PyOpType("add"), PyAlgebraicValue.integer(1)))
        tx.add_operation(PyAlgebraicOperation("b", PyOpType("max"), PyAlgebraicValue.integer(2)))

        assert tx.len() == 2
        assert not tx.is_empty()

    def test_fully_algebraic_transaction(self):
        """Test detecting fully algebraic transactions."""
        tx = PyAlgebraicTransaction()
        tx.add_operation(PyAlgebraicOperation("a", PyOpType("add"), PyAlgebraicValue.integer(1)))
        tx.add_operation(PyAlgebraicOperation("b", PyOpType("max"), PyAlgebraicValue.integer(2)))

        assert tx.is_fully_algebraic()

    def test_non_algebraic_transaction(self):
        """Test detecting non-algebraic transactions."""
        tx = PyAlgebraicTransaction()
        tx.add_operation(PyAlgebraicOperation("a", PyOpType("add"), PyAlgebraicValue.integer(1)))
        tx.add_operation(PyAlgebraicOperation("b", PyOpType("overwrite"), PyAlgebraicValue.integer(2)))

        assert not tx.is_fully_algebraic()

    def test_transaction_metadata(self):
        """Test transaction metadata."""
        tx = PyAlgebraicTransaction()
        tx.set_metadata("user", "alice")
        tx.set_metadata("source", "api")

        assert tx.get_metadata("user") == "alice"
        assert tx.get_metadata("source") == "api"
        assert tx.get_metadata("missing") is None


class TestLocalCommitProtocol:
    """Tests for PyLocalCommitProtocol."""

    def test_can_commit_locally_algebraic(self):
        """Test can_commit_locally for algebraic transactions."""
        tx = PyAlgebraicTransaction()
        tx.add_operation(PyAlgebraicOperation("counter", PyOpType("add"), PyAlgebraicValue.integer(5)))
        tx.add_operation(PyAlgebraicOperation("timestamp", PyOpType("max"), PyAlgebraicValue.integer(1000)))

        assert PyLocalCommitProtocol.can_commit_locally(tx)

    def test_cannot_commit_locally_non_algebraic(self):
        """Test can_commit_locally rejects non-algebraic transactions."""
        tx = PyAlgebraicTransaction()
        tx.add_operation(PyAlgebraicOperation("counter", PyOpType("add"), PyAlgebraicValue.integer(5)))
        tx.add_operation(PyAlgebraicOperation("name", PyOpType("overwrite"), PyAlgebraicValue.integer(1)))

        assert not PyLocalCommitProtocol.can_commit_locally(tx)

    def test_cannot_commit_locally_empty(self):
        """Test can_commit_locally rejects empty transactions."""
        tx = PyAlgebraicTransaction()
        assert not PyLocalCommitProtocol.can_commit_locally(tx)

    def test_commit_local_success(self):
        """Test successfully committing a local transaction."""
        node = PyNodeId("node-1")
        clock = PyVectorClock()

        tx = PyAlgebraicTransaction()
        tx.add_operation(PyAlgebraicOperation("counter", PyOpType("add"), PyAlgebraicValue.integer(5)))

        update = PyLocalCommitProtocol.commit_local(tx, node, clock)

        # Check the update
        assert str(update.origin_node) == "node-1"
        assert len(update.operations()) == 1
        assert update.operations()[0].key == "counter"

        # Check clock was incremented
        assert clock.get(node) == 1

    def test_commit_local_fails_non_algebraic(self):
        """Test commit_local fails for non-algebraic transactions."""
        node = PyNodeId("node-1")
        clock = PyVectorClock()

        tx = PyAlgebraicTransaction()
        tx.add_operation(PyAlgebraicOperation("name", PyOpType("overwrite"), PyAlgebraicValue.integer(1)))

        with pytest.raises(ValueError):
            PyLocalCommitProtocol.commit_local(tx, node, clock)

        # Clock should NOT be incremented on failure
        assert clock.get(node) == 0


class TestMergeUpdates:
    """Tests for merging versioned updates."""

    def test_merge_disjoint_keys(self):
        """Test merging updates with disjoint keys."""
        node_a = PyNodeId("a")
        node_b = PyNodeId("b")
        clock_a = PyVectorClock()
        clock_b = PyVectorClock()

        tx_a = PyAlgebraicTransaction()
        tx_a.add_operation(PyAlgebraicOperation("counter_a", PyOpType("add"), PyAlgebraicValue.integer(5)))

        tx_b = PyAlgebraicTransaction()
        tx_b.add_operation(PyAlgebraicOperation("counter_b", PyOpType("add"), PyAlgebraicValue.integer(3)))

        update_a = PyLocalCommitProtocol.commit_local(tx_a, node_a, clock_a)
        update_b = PyLocalCommitProtocol.commit_local(tx_b, node_b, clock_b)

        merged = PyLocalCommitProtocol.merge_updates(update_a, update_b)

        # Both operations should be present
        ops = {op.key: op for op in merged.operations()}
        assert len(ops) == 2
        assert "counter_a" in ops
        assert "counter_b" in ops

    def test_merge_same_key_add(self):
        """Test merging updates with same key (additive)."""
        node_a = PyNodeId("a")
        node_b = PyNodeId("b")
        clock_a = PyVectorClock()
        clock_b = PyVectorClock()

        tx_a = PyAlgebraicTransaction()
        tx_a.add_operation(PyAlgebraicOperation("counter", PyOpType("add"), PyAlgebraicValue.integer(5)))

        tx_b = PyAlgebraicTransaction()
        tx_b.add_operation(PyAlgebraicOperation("counter", PyOpType("add"), PyAlgebraicValue.integer(3)))

        update_a = PyLocalCommitProtocol.commit_local(tx_a, node_a, clock_a)
        update_b = PyLocalCommitProtocol.commit_local(tx_b, node_b, clock_b)

        merged = PyLocalCommitProtocol.merge_updates(update_a, update_b)

        # 5 + 3 = 8
        ops = merged.operations()
        assert len(ops) == 1
        assert str(ops[0].value) == "8"

    def test_merge_same_key_max(self):
        """Test merging updates with same key (max)."""
        node_a = PyNodeId("a")
        node_b = PyNodeId("b")
        clock_a = PyVectorClock()
        clock_b = PyVectorClock()

        tx_a = PyAlgebraicTransaction()
        tx_a.add_operation(PyAlgebraicOperation("timestamp", PyOpType("max"), PyAlgebraicValue.integer(1000)))

        tx_b = PyAlgebraicTransaction()
        tx_b.add_operation(PyAlgebraicOperation("timestamp", PyOpType("max"), PyAlgebraicValue.integer(1500)))

        update_a = PyLocalCommitProtocol.commit_local(tx_a, node_a, clock_a)
        update_b = PyLocalCommitProtocol.commit_local(tx_b, node_b, clock_b)

        merged = PyLocalCommitProtocol.merge_updates(update_a, update_b)

        # max(1000, 1500) = 1500
        ops = merged.operations()
        assert len(ops) == 1
        assert str(ops[0].value) == "1500"

    def test_merge_same_key_union(self):
        """Test merging updates with same key (union)."""
        node_a = PyNodeId("a")
        node_b = PyNodeId("b")
        clock_a = PyVectorClock()
        clock_b = PyVectorClock()

        tx_a = PyAlgebraicTransaction()
        tx_a.add_operation(PyAlgebraicOperation("tags", PyOpType("union"), PyAlgebraicValue.string_set(["featured"])))

        tx_b = PyAlgebraicTransaction()
        tx_b.add_operation(PyAlgebraicOperation("tags", PyOpType("union"), PyAlgebraicValue.string_set(["new", "sale"])))

        update_a = PyLocalCommitProtocol.commit_local(tx_a, node_a, clock_a)
        update_b = PyLocalCommitProtocol.commit_local(tx_b, node_b, clock_b)

        merged = PyLocalCommitProtocol.merge_updates(update_a, update_b)

        # union should contain all tags
        ops = merged.operations()
        assert len(ops) == 1
        value_str = str(ops[0].value)
        assert "featured" in value_str
        assert "new" in value_str
        assert "sale" in value_str


class TestCommutativity:
    """Tests verifying the commutativity of merge operations."""

    def test_merge_is_commutative_add(self):
        """Test that merge(A, B) == merge(B, A) for ADD."""
        node_a = PyNodeId("a")
        node_b = PyNodeId("b")
        clock_a = PyVectorClock()
        clock_b = PyVectorClock()

        tx_a = PyAlgebraicTransaction()
        tx_a.add_operation(PyAlgebraicOperation("counter", PyOpType("add"), PyAlgebraicValue.integer(5)))

        tx_b = PyAlgebraicTransaction()
        tx_b.add_operation(PyAlgebraicOperation("counter", PyOpType("add"), PyAlgebraicValue.integer(3)))

        update_a = PyLocalCommitProtocol.commit_local(tx_a, node_a, clock_a)
        update_b = PyLocalCommitProtocol.commit_local(tx_b, node_b, clock_b)

        # merge(A, B) should equal merge(B, A)
        merged_ab = PyLocalCommitProtocol.merge_updates(update_a, update_b)
        merged_ba = PyLocalCommitProtocol.merge_updates(update_b, update_a)

        val_ab = str(merged_ab.operations()[0].value)
        val_ba = str(merged_ba.operations()[0].value)

        assert val_ab == val_ba
        assert val_ab == "8"

    def test_merge_is_commutative_max(self):
        """Test that merge(A, B) == merge(B, A) for MAX."""
        node_a = PyNodeId("a")
        node_b = PyNodeId("b")
        clock_a = PyVectorClock()
        clock_b = PyVectorClock()

        tx_a = PyAlgebraicTransaction()
        tx_a.add_operation(PyAlgebraicOperation("ts", PyOpType("max"), PyAlgebraicValue.integer(100)))

        tx_b = PyAlgebraicTransaction()
        tx_b.add_operation(PyAlgebraicOperation("ts", PyOpType("max"), PyAlgebraicValue.integer(200)))

        update_a = PyLocalCommitProtocol.commit_local(tx_a, node_a, clock_a)
        update_b = PyLocalCommitProtocol.commit_local(tx_b, node_b, clock_b)

        merged_ab = PyLocalCommitProtocol.merge_updates(update_a, update_b)
        merged_ba = PyLocalCommitProtocol.merge_updates(update_b, update_a)

        val_ab = str(merged_ab.operations()[0].value)
        val_ba = str(merged_ba.operations()[0].value)

        assert val_ab == val_ba
        assert val_ab == "200"


class TestAssociativity:
    """Tests verifying the associativity of merge operations."""

    def test_merge_is_associative_add(self):
        """Test that (A merge B) merge C == A merge (B merge C) for ADD."""
        nodes = [PyNodeId(f"node-{i}") for i in range(3)]
        clocks = [PyVectorClock() for _ in range(3)]

        txs = [PyAlgebraicTransaction() for _ in range(3)]
        for i, tx in enumerate(txs):
            tx.add_operation(PyAlgebraicOperation(
                "counter", PyOpType("add"), PyAlgebraicValue.integer((i + 1) * 10)
            ))  # 10, 20, 30

        updates = [
            PyLocalCommitProtocol.commit_local(tx, node, clock)
            for tx, node, clock in zip(txs, nodes, clocks)
        ]

        # (A merge B) merge C
        ab = PyLocalCommitProtocol.merge_updates(updates[0], updates[1])
        ab_c = PyLocalCommitProtocol.merge_updates(ab, updates[2])

        # A merge (B merge C)
        bc = PyLocalCommitProtocol.merge_updates(updates[1], updates[2])
        a_bc = PyLocalCommitProtocol.merge_updates(updates[0], bc)

        val_ab_c = str(ab_c.operations()[0].value)
        val_a_bc = str(a_bc.operations()[0].value)

        # Both should equal 10 + 20 + 30 = 60
        assert val_ab_c == val_a_bc
        assert val_ab_c == "60"


class TestMergeAll:
    """Tests for merge_all function."""

    def test_merge_all(self):
        """Test merging multiple updates at once."""
        nodes = [PyNodeId(f"node-{i}") for i in range(5)]
        clocks = [PyVectorClock() for _ in range(5)]

        updates = []
        for i, (node, clock) in enumerate(zip(nodes, clocks)):
            tx = PyAlgebraicTransaction()
            tx.add_operation(PyAlgebraicOperation(
                "total", PyOpType("add"), PyAlgebraicValue.integer((i + 1) * 10)
            ))  # 10, 20, 30, 40, 50
            updates.append(PyLocalCommitProtocol.commit_local(tx, node, clock))

        merged = PyLocalCommitProtocol.merge_all(updates)

        # 10 + 20 + 30 + 40 + 50 = 150
        val = str(merged.operations()[0].value)
        assert val == "150"


class TestVersionedUpdate:
    """Tests for PyVersionedUpdate."""

    def test_update_causality(self):
        """Test causality comparison between updates."""
        node = PyNodeId("node-1")
        clock = PyVectorClock()

        tx1 = PyAlgebraicTransaction()
        tx1.add_operation(PyAlgebraicOperation("x", PyOpType("add"), PyAlgebraicValue.integer(1)))

        tx2 = PyAlgebraicTransaction()
        tx2.add_operation(PyAlgebraicOperation("y", PyOpType("add"), PyAlgebraicValue.integer(2)))

        update1 = PyLocalCommitProtocol.commit_local(tx1, node, clock)
        update2 = PyLocalCommitProtocol.commit_local(tx2, node, clock)

        # update1 happened before update2 (same node, sequential)
        order = update1.compare(update2)
        assert order.order == "before"

    def test_updates_are_concurrent(self):
        """Test detecting concurrent updates."""
        node_a = PyNodeId("a")
        node_b = PyNodeId("b")
        clock_a = PyVectorClock()
        clock_b = PyVectorClock()

        tx_a = PyAlgebraicTransaction()
        tx_a.add_operation(PyAlgebraicOperation("x", PyOpType("add"), PyAlgebraicValue.integer(1)))

        tx_b = PyAlgebraicTransaction()
        tx_b.add_operation(PyAlgebraicOperation("y", PyOpType("add"), PyAlgebraicValue.integer(1)))

        update_a = PyLocalCommitProtocol.commit_local(tx_a, node_a, clock_a)
        update_b = PyLocalCommitProtocol.commit_local(tx_b, node_b, clock_b)

        # These should be concurrent (different nodes, no communication)
        assert update_a.is_concurrent_with(update_b)
        assert update_a.compare(update_b).order == "concurrent"


class TestRealisticScenario:
    """Integration tests for realistic distributed scenarios."""

    def test_ecommerce_scenario(self):
        """Test a realistic e-commerce scenario with multiple metrics."""
        node_sf = PyNodeId("san-francisco")
        node_tokyo = PyNodeId("tokyo")
        clock_sf = PyVectorClock()
        clock_tokyo = PyVectorClock()

        # SF transaction: increment page views, add tags, update timestamp
        tx_sf = PyAlgebraicTransaction()
        tx_sf.add_operation(PyAlgebraicOperation("page_views", PyOpType("add"), PyAlgebraicValue.integer(100)))
        tx_sf.add_operation(PyAlgebraicOperation("tags", PyOpType("union"), PyAlgebraicValue.string_set(["featured", "promoted"])))
        tx_sf.add_operation(PyAlgebraicOperation("last_seen", PyOpType("max"), PyAlgebraicValue.integer(1000)))

        # Tokyo transaction: same metrics, concurrent
        tx_tokyo = PyAlgebraicTransaction()
        tx_tokyo.add_operation(PyAlgebraicOperation("page_views", PyOpType("add"), PyAlgebraicValue.integer(50)))
        tx_tokyo.add_operation(PyAlgebraicOperation("tags", PyOpType("union"), PyAlgebraicValue.string_set(["new", "promoted"])))
        tx_tokyo.add_operation(PyAlgebraicOperation("last_seen", PyOpType("max"), PyAlgebraicValue.integer(1200)))

        # Both commit locally (no coordination!)
        update_sf = PyLocalCommitProtocol.commit_local(tx_sf, node_sf, clock_sf)
        update_tokyo = PyLocalCommitProtocol.commit_local(tx_tokyo, node_tokyo, clock_tokyo)

        # They're concurrent
        assert update_sf.is_concurrent_with(update_tokyo)

        # Merge them
        merged = PyLocalCommitProtocol.merge_updates(update_sf, update_tokyo)

        # Verify results
        ops = {op.key: op for op in merged.operations()}

        # page_views: 100 + 50 = 150
        assert str(ops["page_views"].value) == "150"

        # last_seen: max(1000, 1200) = 1200
        assert str(ops["last_seen"].value) == "1200"

        # tags: union of all
        tags_str = str(ops["tags"].value)
        assert "featured" in tags_str
        assert "promoted" in tags_str
        assert "new" in tags_str

    def test_five_node_convergence(self):
        """Test that 5 nodes converge to the same state regardless of merge order."""
        nodes = [PyNodeId(f"node-{i}") for i in range(5)]
        clocks = [PyVectorClock() for _ in range(5)]

        # Each node increments a global counter
        updates = []
        for i, (node, clock) in enumerate(zip(nodes, clocks)):
            tx = PyAlgebraicTransaction()
            tx.add_operation(PyAlgebraicOperation(
                "global_counter", PyOpType("add"), PyAlgebraicValue.integer((i + 1) * 10)
            ))  # 10, 20, 30, 40, 50
            updates.append(PyLocalCommitProtocol.commit_local(tx, node, clock))

        # Merge in forward order
        merge_forward = updates[0]
        for u in updates[1:]:
            merge_forward = PyLocalCommitProtocol.merge_updates(merge_forward, u)

        # Merge in reverse order
        merge_reverse = updates[4]
        for u in reversed(updates[:4]):
            merge_reverse = PyLocalCommitProtocol.merge_updates(merge_reverse, u)

        # Merge using merge_all
        merge_all = PyLocalCommitProtocol.merge_all(updates)

        # All should equal 10 + 20 + 30 + 40 + 50 = 150
        val_forward = str(merge_forward.operations()[0].value)
        val_reverse = str(merge_reverse.operations()[0].value)
        val_all = str(merge_all.operations()[0].value)

        assert val_forward == "150"
        assert val_reverse == "150"
        assert val_all == "150"
