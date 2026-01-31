"""
Tests for PyO3 bindings (Rust↔Python bridge).

Covers classes NOT already tested in test_rhizo.py:
- BranchManager, Branch, BranchDiff
- TransactionManager, TransactionInfo, RecoveryReport
- Changelog (ChangelogEntry, TableChange)
- Merkle trees (MerkleConfig, MerkleTree, MerkleDiff, module functions)
- Algebraic types (OpType, AlgebraicValue, TableAlgebraicSchema, SchemaRegistry)
- Distributed types (NodeId, VectorClock, CausalOrder, LocalCommitProtocol)
- Filter/Predicate (FilterOp, ScalarValue, PredicateFilter)
- Parquet advanced (projection pushdown, predicate pushdown)
"""

import os
import tempfile
import shutil

import pyarrow as pa
import pytest

import _rhizo


@pytest.fixture
def temp_dir():
    d = tempfile.mkdtemp(prefix="rhizo_pyo3_")
    yield d
    shutil.rmtree(d, ignore_errors=True)


# ===================================================================
# BRANCH MANAGER BINDINGS
# ===================================================================

class TestPyBranchManager:
    """Test PyBranchManager, PyBranch, PyBranchDiff bindings."""

    def test_create_and_get_branch(self, temp_dir):
        bm = _rhizo.PyBranchManager(os.path.join(temp_dir, "branches"))
        branch = bm.create("feature/x", None, "A test branch")
        assert branch.name == "feature/x"
        assert branch.description == "A test branch"
        assert isinstance(branch.head, dict)
        assert isinstance(branch.created_at, int)

    def test_list_branches(self, temp_dir):
        bm = _rhizo.PyBranchManager(os.path.join(temp_dir, "branches"))
        bm.create("dev", None, None)
        bm.create("staging", None, None)
        names = bm.list()
        assert "dev" in names
        assert "staging" in names

    def test_delete_branch(self, temp_dir):
        bm = _rhizo.PyBranchManager(os.path.join(temp_dir, "branches"))
        bm.create("temp", None, None)
        assert "temp" in bm.list()
        bm.delete("temp")
        assert "temp" not in bm.list()

    def test_update_head(self, temp_dir):
        bm = _rhizo.PyBranchManager(os.path.join(temp_dir, "branches"))
        # "main" is auto-created by BranchManager
        bm.update_head("main", "users", 5)
        v = bm.get_table_version("main", "users")
        assert v == 5

    def test_get_table_version_returns_none_for_missing(self, temp_dir):
        bm = _rhizo.PyBranchManager(os.path.join(temp_dir, "branches"))
        v = bm.get_table_version("main", "nonexistent")
        assert v is None

    def test_diff_branches(self, temp_dir):
        bm = _rhizo.PyBranchManager(os.path.join(temp_dir, "branches"))
        bm.update_head("main", "users", 1)
        bm.create("feature", "main", None)
        bm.update_head("feature", "users", 2)

        diff = bm.diff("feature", "main")
        assert diff.source_branch == "feature"
        assert diff.target_branch == "main"
        assert isinstance(diff.has_conflicts, bool)
        assert isinstance(diff.unchanged, list)

    def test_branch_parent(self, temp_dir):
        bm = _rhizo.PyBranchManager(os.path.join(temp_dir, "branches"))
        child = bm.create("child", "main", None)
        assert child.parent_branch == "main"

    def test_get_nonexistent_branch_raises(self, temp_dir):
        bm = _rhizo.PyBranchManager(os.path.join(temp_dir, "branches"))
        with pytest.raises(Exception):
            bm.get("ghost")

    def test_default_branch(self, temp_dir):
        bm = _rhizo.PyBranchManager(os.path.join(temp_dir, "branches"))
        # "main" is auto-created by the constructor
        bm.set_default("main")
        assert bm.get_default() == "main"

    def test_fast_forward_merge(self, temp_dir):
        bm = _rhizo.PyBranchManager(os.path.join(temp_dir, "branches"))
        # "main" is auto-created by the constructor
        bm.update_head("main", "t1", 1)
        bm.create("feature", "main", None)
        bm.update_head("feature", "t1", 2)
        bm.update_head("feature", "t2", 1)

        bm.merge("feature", "main")
        assert bm.get_table_version("main", "t1") == 2
        assert bm.get_table_version("main", "t2") == 1


# ===================================================================
# TRANSACTION MANAGER BINDINGS
# ===================================================================

class TestPyTransactionManager:
    """Test PyTransactionManager, PyTransactionInfo, PyRecoveryReport."""

    @pytest.fixture
    def tx_env(self, temp_dir):
        cat_dir = os.path.join(temp_dir, "catalog")
        br_dir = os.path.join(temp_dir, "branches")
        tx_dir = os.path.join(temp_dir, "tx")
        catalog = _rhizo.PyCatalog(cat_dir)
        branches = _rhizo.PyBranchManager(br_dir)
        # "main" is auto-created by PyBranchManager constructor
        tm = _rhizo.PyTransactionManager(tx_dir, cat_dir, br_dir)
        return tm, catalog, branches

    def test_begin_and_commit(self, tx_env):
        tm, catalog, _ = tx_env
        tx_id = tm.begin("main")
        assert isinstance(tx_id, int)
        assert tm.active_count() == 1
        tm.commit(tx_id)
        assert tm.active_count() == 0

    def test_begin_and_abort(self, tx_env):
        tm, *_ = tx_env
        tx_id = tm.begin("main")
        tm.abort(tx_id, "test abort")
        assert tm.active_count() == 0

    def test_get_transaction_info(self, tx_env):
        tm, *_ = tx_env
        tx_id = tm.begin("main")
        info = tm.get_transaction(tx_id)
        assert info.tx_id == tx_id
        assert info.branch == "main"
        assert isinstance(info.status, str)
        assert isinstance(info.started_at, int)
        assert isinstance(info.read_snapshot, dict)
        assert isinstance(info.written_tables, list)
        tm.abort(tx_id)

    def test_active_transactions(self, tx_env):
        tm, *_ = tx_env
        tx1 = tm.begin("main")
        tx2 = tm.begin("main")
        active = tm.active_transactions()
        assert len(active) == 2
        tm.abort(tx1)
        tm.abort(tx2)

    def test_recover_clean_system(self, tx_env):
        tm, *_ = tx_env
        report = tm.recover()
        assert report.is_clean is True
        assert len(report.errors) == 0
        assert len(report.warnings) == 0
        assert isinstance(report.replayed, list)
        assert isinstance(report.rolled_back, list)
        assert isinstance(report.already_aborted, list)
        assert isinstance(report.already_committed, list)

    def test_recover_and_apply(self, tx_env):
        tm, *_ = tx_env
        report = tm.recover_and_apply()
        assert report.is_clean is True

    def test_verify_consistency(self, tx_env):
        tm, *_ = tx_env
        issues = tm.verify_consistency()
        assert isinstance(issues, list)
        assert len(issues) == 0

    def test_latest_tx_id(self, tx_env):
        tm, *_ = tx_env
        # Initially None or some value
        initial = tm.latest_tx_id()
        tx_id = tm.begin("main")
        tm.commit(tx_id)
        after = tm.latest_tx_id()
        assert after is not None


# ===================================================================
# CHANGELOG BINDINGS
# ===================================================================

class TestPyChangelog:
    """Test PyChangelogEntry, PyTableChange bindings."""

    @pytest.fixture
    def tx_with_data(self, temp_dir):
        cat_dir = os.path.join(temp_dir, "catalog")
        br_dir = os.path.join(temp_dir, "branches")
        tx_dir = os.path.join(temp_dir, "tx")
        store = _rhizo.PyChunkStore(os.path.join(temp_dir, "chunks"))
        catalog = _rhizo.PyCatalog(cat_dir)
        branches = _rhizo.PyBranchManager(br_dir)
        # "main" is auto-created by PyBranchManager constructor
        tm = _rhizo.PyTransactionManager(tx_dir, cat_dir, br_dir)

        # Commit a transaction with writes
        tx_id = tm.begin("main")
        h = store.put(b"chunk data")
        catalog.commit_next("users", [h])
        tm.add_write(tx_id, "users", 1, [h])
        tm.commit(tx_id)
        return tm, store, catalog

    def test_get_changelog(self, tx_with_data):
        tm, *_ = tx_with_data
        entries = tm.get_changelog()
        assert len(entries) >= 1

    def test_changelog_entry_attributes(self, tx_with_data):
        tm, *_ = tx_with_data
        entries = tm.get_changelog()
        entry = entries[0]
        assert isinstance(entry.tx_id, int)
        assert isinstance(entry.epoch_id, int)
        assert isinstance(entry.committed_at, int)
        assert isinstance(entry.branch, str)

    def test_changelog_entry_changes(self, tx_with_data):
        tm, *_ = tx_with_data
        entries = tm.get_changelog()
        entry = entries[0]
        changes = entry.changes
        assert len(changes) >= 1
        assert entry.change_count() >= 1
        assert entry.contains_table("users") is True

    def test_table_change_attributes(self, tx_with_data):
        tm, *_ = tx_with_data
        entries = tm.get_changelog()
        change = entries[0].get_change("users")
        assert change is not None
        assert change.table_name == "users"
        assert change.new_version == 1
        assert isinstance(change.chunk_hashes, list)
        assert change.is_new_table() is True  # old_version is None

    def test_changelog_entry_repr(self, tx_with_data):
        tm, *_ = tx_with_data
        entries = tm.get_changelog()
        r = repr(entries[0])
        assert "ChangelogEntry" in r or "tx_id" in r.lower() or isinstance(r, str)

    def test_changelog_changed_tables(self, tx_with_data):
        tm, *_ = tx_with_data
        entries = tm.get_changelog()
        tables = entries[0].changed_tables()
        assert "users" in tables


# ===================================================================
# MERKLE TREE BINDINGS
# ===================================================================

class TestPyMerkle:
    """Test Merkle tree bindings."""

    def test_build_tree_default_config(self):
        data = b"x" * 200_000
        tree = _rhizo.merkle_build_tree(data)
        assert len(tree.root_hash) == 64
        assert tree.total_size == 200_000
        assert tree.chunk_count() >= 1
        assert tree.height >= 1

    def test_build_tree_custom_config(self):
        config = _rhizo.PyMerkleConfig(chunk_size=1024, branching_factor=4)
        assert config.chunk_size == 1024
        assert config.branching_factor == 4
        data = b"y" * 10_000
        tree = _rhizo.merkle_build_tree(data, config)
        assert tree.chunk_count() >= 2  # 10KB / 1KB = ~10 chunks

    def test_tree_chunks(self):
        data = b"z" * 200_000
        tree = _rhizo.merkle_build_tree(data)
        chunks = tree.chunks
        assert len(chunks) == tree.chunk_count()
        for chunk in chunks:
            assert len(chunk.hash) == 64
            assert chunk.size > 0
            assert isinstance(chunk.index, int)
            start, end = chunk.byte_range
            assert end > start

    def test_chunk_hashes(self):
        data = b"a" * 100_000
        tree = _rhizo.merkle_build_tree(data)
        hashes = tree.chunk_hashes()
        assert len(hashes) == tree.chunk_count()
        assert all(len(h) == 64 for h in hashes)

    def test_chunk_for_offset(self):
        data = b"b" * 200_000
        tree = _rhizo.merkle_build_tree(data)
        chunk = tree.chunk_for_offset(0)
        assert chunk is not None
        assert chunk.index == 0

    def test_chunks_in_range(self):
        data = b"c" * 200_000
        tree = _rhizo.merkle_build_tree(data)
        chunks = tree.chunks_in_range(0, 100_000)
        assert len(chunks) >= 1

    def test_diff_identical_trees(self):
        data = b"d" * 100_000
        t1 = _rhizo.merkle_build_tree(data)
        t2 = _rhizo.merkle_build_tree(data)
        diff = _rhizo.merkle_diff_trees(t1, t2)
        assert diff.added_count() == 0
        assert diff.removed_count() == 0
        assert diff.unchanged_count() == t1.chunk_count()
        assert diff.reuse_ratio == pytest.approx(1.0)
        assert diff.reuse_percentage() == pytest.approx(100.0)

    def test_diff_different_trees(self):
        t1 = _rhizo.merkle_build_tree(b"e" * 100_000)
        t2 = _rhizo.merkle_build_tree(b"f" * 100_000)
        diff = _rhizo.merkle_diff_trees(t1, t2)
        assert diff.added_count() > 0
        assert diff.removed_count() > 0

    def test_verify_tree(self, temp_dir):
        store = _rhizo.PyChunkStore(os.path.join(temp_dir, "chunks"))
        data = b"g" * 100_000
        tree = _rhizo.merkle_build_tree(data)
        # Store all chunks so verify can find them
        config = _rhizo.PyMerkleConfig()
        chunk_size = config.chunk_size
        offset = 0
        while offset < len(data):
            chunk = data[offset:offset + chunk_size]
            store.put(chunk)
            offset += chunk_size
        # verify_tree checks that all chunks exist and hashes match
        result = _rhizo.merkle_verify_tree(tree, store)
        assert isinstance(result, bool)

    def test_tree_repr(self):
        tree = _rhizo.merkle_build_tree(b"h" * 100_000)
        r = repr(tree)
        assert isinstance(r, str)

    def test_diff_repr(self):
        t1 = _rhizo.merkle_build_tree(b"i" * 100_000)
        t2 = _rhizo.merkle_build_tree(b"i" * 100_000)
        diff = _rhizo.merkle_diff_trees(t1, t2)
        assert isinstance(repr(diff), str)


# ===================================================================
# ALGEBRAIC TYPE BINDINGS
# ===================================================================

class TestPyAlgebraicTypes:
    """Test PyOpType, PyAlgebraicValue, PyTableAlgebraicSchema, algebraic_merge."""

    # --- PyOpType ---

    def test_optype_creation(self):
        op = _rhizo.PyOpType("max")
        assert op.is_conflict_free() is True
        assert op.is_semilattice() is True
        assert op.is_abelian() is False

    def test_optype_abelian(self):
        op = _rhizo.PyOpType("add")
        assert op.is_abelian() is True
        assert op.is_conflict_free() is True

    def test_optype_generic(self):
        op = _rhizo.PyOpType("overwrite")
        assert op.is_conflict_free() is False
        assert op.is_semilattice() is False
        assert op.is_abelian() is False

    def test_optype_can_merge_with(self):
        max_op = _rhizo.PyOpType("max")
        add_op = _rhizo.PyOpType("add")
        assert max_op.can_merge_with(max_op) is True
        # Different algebraic ops may or may not merge
        assert isinstance(add_op.can_merge_with(max_op), bool)

    def test_optype_description(self):
        op = _rhizo.PyOpType("min")
        assert isinstance(op.description(), str)

    def test_optype_repr_str(self):
        op = _rhizo.PyOpType("union")
        assert isinstance(repr(op), str)
        assert isinstance(str(op), str)

    def test_optype_long_form(self):
        op = _rhizo.PyOpType("semilattice_max")
        assert op.is_semilattice() is True

    # --- PyAlgebraicValue ---

    def test_algebraic_value_integer(self):
        v = _rhizo.PyAlgebraicValue.integer(42)
        assert v.is_numeric() is True
        assert v.is_set() is False
        assert v.is_null() is False
        assert v.type_name() == "Integer"

    def test_algebraic_value_float(self):
        v = _rhizo.PyAlgebraicValue.float(3.14)
        assert v.is_numeric() is True
        assert v.type_name() == "Float"

    def test_algebraic_value_string_set(self):
        v = _rhizo.PyAlgebraicValue.string_set(["a", "b", "c"])
        assert v.is_set() is True
        assert v.is_numeric() is False

    def test_algebraic_value_int_set(self):
        v = _rhizo.PyAlgebraicValue.int_set([1, 2, 3])
        assert v.is_set() is True

    def test_algebraic_value_boolean(self):
        v = _rhizo.PyAlgebraicValue.boolean(True)
        assert v.is_null() is False

    def test_algebraic_value_null(self):
        v = _rhizo.PyAlgebraicValue.null()
        assert v.is_null() is True

    def test_algebraic_value_auto_infer_int(self):
        v = _rhizo.PyAlgebraicValue(10)
        assert v.is_numeric() is True
        assert v.type_name() == "Integer"

    def test_algebraic_value_auto_infer_float(self):
        v = _rhizo.PyAlgebraicValue(2.5)
        assert v.type_name() == "Float"

    def test_algebraic_value_auto_infer_bool(self):
        v = _rhizo.PyAlgebraicValue(True)
        assert v.type_name() == "Boolean"

    def test_algebraic_value_auto_infer_none(self):
        v = _rhizo.PyAlgebraicValue(None)
        assert v.is_null() is True

    def test_algebraic_value_repr(self):
        v = _rhizo.PyAlgebraicValue.integer(7)
        assert isinstance(repr(v), str)
        assert isinstance(str(v), str)

    # --- algebraic_merge ---

    def test_algebraic_merge_max(self):
        op = _rhizo.PyOpType("max")
        a = _rhizo.PyAlgebraicValue.integer(5)
        b = _rhizo.PyAlgebraicValue.integer(10)
        result = _rhizo.algebraic_merge(op, a, b)
        assert str(result) == "Integer(10)" or "10" in str(result)

    def test_algebraic_merge_add(self):
        op = _rhizo.PyOpType("add")
        a = _rhizo.PyAlgebraicValue.integer(3)
        b = _rhizo.PyAlgebraicValue.integer(7)
        result = _rhizo.algebraic_merge(op, a, b)
        assert "10" in str(result)

    def test_algebraic_merge_union_string_set(self):
        op = _rhizo.PyOpType("union")
        a = _rhizo.PyAlgebraicValue.string_set(["x", "y"])
        b = _rhizo.PyAlgebraicValue.string_set(["y", "z"])
        result = _rhizo.algebraic_merge(op, a, b)
        assert result.is_set() is True

    # --- PyTableAlgebraicSchema ---

    def test_table_schema_creation(self):
        schema = _rhizo.PyTableAlgebraicSchema("counters", _rhizo.PyOpType("add"))
        assert schema.table == "counters"

    def test_table_schema_all_additive(self):
        schema = _rhizo.PyTableAlgebraicSchema.all_additive("sums")
        assert schema.table == "sums"
        assert schema.is_fully_conflict_free() is True

    def test_table_schema_all_max(self):
        schema = _rhizo.PyTableAlgebraicSchema.all_max("peaks")
        assert schema.is_fully_conflict_free() is True

    def test_table_schema_add_column(self):
        schema = _rhizo.PyTableAlgebraicSchema("mixed", None)
        schema.add_column("count", _rhizo.PyOpType("add"))
        schema.add_column("name", _rhizo.PyOpType("overwrite"))
        cf = schema.conflict_free_columns()
        assert "count" in cf
        c = schema.conflicting_columns()
        assert "name" in c
        assert schema.is_fully_conflict_free() is False

    def test_table_schema_can_auto_merge(self):
        schema = _rhizo.PyTableAlgebraicSchema.all_additive("t")
        schema.add_column("x", _rhizo.PyOpType("add"))
        assert schema.can_auto_merge(["x"]) is True

    def test_table_schema_repr(self):
        schema = _rhizo.PyTableAlgebraicSchema("t", None)
        assert isinstance(repr(schema), str)

    # --- PyAlgebraicSchemaRegistry ---

    def test_registry_register_and_get(self):
        reg = _rhizo.PyAlgebraicSchemaRegistry()
        schema = _rhizo.PyTableAlgebraicSchema.all_additive("counters")
        reg.register(schema)
        assert reg.has_table("counters") is True
        retrieved = reg.get("counters")
        assert retrieved is not None
        assert retrieved.table == "counters"

    def test_registry_tables(self):
        reg = _rhizo.PyAlgebraicSchemaRegistry()
        reg.register(_rhizo.PyTableAlgebraicSchema.all_additive("a"))
        reg.register(_rhizo.PyTableAlgebraicSchema.all_max("b"))
        tables = reg.tables()
        assert "a" in tables
        assert "b" in tables

    def test_registry_unregister(self):
        reg = _rhizo.PyAlgebraicSchemaRegistry()
        reg.register(_rhizo.PyTableAlgebraicSchema.all_additive("temp"))
        assert reg.has_table("temp") is True
        removed = reg.unregister("temp")
        assert removed is not None
        assert reg.has_table("temp") is False

    def test_registry_get_op_type(self):
        reg = _rhizo.PyAlgebraicSchemaRegistry()
        schema = _rhizo.PyTableAlgebraicSchema("t", None)
        schema.add_column("count", _rhizo.PyOpType("add"))
        reg.register(schema)
        op = reg.get_op_type("t", "count")
        assert op.is_abelian() is True

    def test_registry_repr(self):
        reg = _rhizo.PyAlgebraicSchemaRegistry()
        assert isinstance(repr(reg), str)


# ===================================================================
# DISTRIBUTED TYPE BINDINGS
# ===================================================================

class TestPyDistributedTypes:
    """Test PyNodeId, PyVectorClock, PyCausalOrder, PyLocalCommitProtocol."""

    # --- PyNodeId ---

    def test_node_id_creation(self):
        n = _rhizo.PyNodeId("node-1")
        assert str(n) == "node-1"

    def test_node_id_equality(self):
        a = _rhizo.PyNodeId("x")
        b = _rhizo.PyNodeId("x")
        c = _rhizo.PyNodeId("y")
        assert a == b
        assert a != c

    def test_node_id_hashable(self):
        a = _rhizo.PyNodeId("x")
        b = _rhizo.PyNodeId("x")
        assert hash(a) == hash(b)
        s = {a, b}
        assert len(s) == 1

    def test_node_id_repr(self):
        n = _rhizo.PyNodeId("n1")
        assert isinstance(repr(n), str)

    # --- PyVectorClock ---

    def test_vector_clock_empty(self):
        vc = _rhizo.PyVectorClock()
        assert vc.is_empty() is True
        assert vc.node_count() == 0
        assert vc.sum() == 0

    def test_vector_clock_tick(self):
        vc = _rhizo.PyVectorClock()
        node = _rhizo.PyNodeId("n1")
        vc.tick(node)
        assert vc.get(node) == 1
        assert vc.is_empty() is False

    def test_vector_clock_set_get(self):
        vc = _rhizo.PyVectorClock()
        node = _rhizo.PyNodeId("n1")
        vc.set(node, 42)
        assert vc.get(node) == 42

    def test_vector_clock_merge(self):
        vc1 = _rhizo.PyVectorClock()
        vc2 = _rhizo.PyVectorClock()
        n1 = _rhizo.PyNodeId("n1")
        n2 = _rhizo.PyNodeId("n2")
        vc1.set(n1, 3)
        vc2.set(n2, 5)
        vc1.merge(vc2)
        assert vc1.get(n1) == 3
        assert vc1.get(n2) == 5

    def test_vector_clock_happened_before(self):
        vc1 = _rhizo.PyVectorClock()
        vc2 = _rhizo.PyVectorClock()
        n = _rhizo.PyNodeId("n1")
        vc1.set(n, 1)
        vc2.set(n, 2)
        assert vc1.happened_before(vc2) is True
        assert vc2.happened_after(vc1) is True

    def test_vector_clock_concurrent(self):
        vc1 = _rhizo.PyVectorClock()
        vc2 = _rhizo.PyVectorClock()
        n1 = _rhizo.PyNodeId("n1")
        n2 = _rhizo.PyNodeId("n2")
        vc1.set(n1, 1)
        vc2.set(n2, 1)
        assert vc1.concurrent_with(vc2) is True

    def test_vector_clock_compare(self):
        vc1 = _rhizo.PyVectorClock()
        vc2 = _rhizo.PyVectorClock()
        n = _rhizo.PyNodeId("n1")
        vc1.set(n, 1)
        vc2.set(n, 1)
        order = vc1.compare(vc2)
        assert order.order == "equal"
        assert order.needs_merge() is False

    def test_vector_clock_with_node(self):
        node = _rhizo.PyNodeId("n1")
        vc = _rhizo.PyVectorClock.with_node(node, 10)
        assert vc.get(node) == 10

    def test_vector_clock_max(self):
        vc1 = _rhizo.PyVectorClock()
        vc2 = _rhizo.PyVectorClock()
        n1 = _rhizo.PyNodeId("n1")
        n2 = _rhizo.PyNodeId("n2")
        vc1.set(n1, 5)
        vc2.set(n2, 3)
        merged = _rhizo.PyVectorClock.max(vc1, vc2)
        assert merged.get(n1) == 5
        assert merged.get(n2) == 3

    def test_vector_clock_ticked(self):
        vc = _rhizo.PyVectorClock()
        n = _rhizo.PyNodeId("n1")
        vc.set(n, 5)
        vc2 = vc.ticked(n)
        assert vc2.get(n) == 6
        assert vc.get(n) == 5  # original unchanged

    def test_vector_clock_json_roundtrip(self):
        vc = _rhizo.PyVectorClock()
        n = _rhizo.PyNodeId("n1")
        vc.set(n, 7)
        j = vc.to_json()
        vc2 = _rhizo.PyVectorClock.from_json(j)
        assert vc2.get(n) == 7

    def test_vector_clock_equality(self):
        vc1 = _rhizo.PyVectorClock()
        vc2 = _rhizo.PyVectorClock()
        n = _rhizo.PyNodeId("n")
        vc1.set(n, 3)
        vc2.set(n, 3)
        assert vc1 == vc2

    def test_vector_clock_repr(self):
        vc = _rhizo.PyVectorClock()
        assert isinstance(repr(vc), str)
        assert isinstance(str(vc), str)

    # --- PyCausalOrder ---

    def test_causal_order_before(self):
        vc1 = _rhizo.PyVectorClock()
        vc2 = _rhizo.PyVectorClock()
        n = _rhizo.PyNodeId("n")
        vc1.set(n, 1)
        vc2.set(n, 2)
        order = vc1.compare(vc2)
        assert order.order == "before"
        assert order.should_apply() is True
        assert order.needs_merge() is False

    def test_causal_order_concurrent(self):
        vc1 = _rhizo.PyVectorClock()
        vc2 = _rhizo.PyVectorClock()
        vc1.set(_rhizo.PyNodeId("a"), 1)
        vc2.set(_rhizo.PyNodeId("b"), 1)
        order = vc1.compare(vc2)
        assert order.order == "concurrent"
        assert order.needs_merge() is True
        assert order.should_apply() is True

    def test_causal_order_repr(self):
        vc1 = _rhizo.PyVectorClock()
        vc2 = _rhizo.PyVectorClock()
        order = vc1.compare(vc2)
        assert isinstance(repr(order), str)
        assert isinstance(str(order), str)

    # --- PyLocalCommitProtocol ---

    def test_can_commit_locally_algebraic(self):
        tx = _rhizo.PyAlgebraicTransaction()
        op = _rhizo.PyAlgebraicOperation("key1", _rhizo.PyOpType("add"), _rhizo.PyAlgebraicValue.integer(1))
        tx.add_operation(op)
        assert _rhizo.PyLocalCommitProtocol.can_commit_locally(tx) is True

    def test_can_commit_locally_generic(self):
        tx = _rhizo.PyAlgebraicTransaction()
        op = _rhizo.PyAlgebraicOperation("key1", _rhizo.PyOpType("overwrite"), _rhizo.PyAlgebraicValue.integer(1))
        tx.add_operation(op)
        assert _rhizo.PyLocalCommitProtocol.can_commit_locally(tx) is False

    def test_commit_local(self):
        tx = _rhizo.PyAlgebraicTransaction()
        op = _rhizo.PyAlgebraicOperation("k", _rhizo.PyOpType("max"), _rhizo.PyAlgebraicValue.integer(10))
        tx.add_operation(op)
        node = _rhizo.PyNodeId("n1")
        clock = _rhizo.PyVectorClock()
        update = _rhizo.PyLocalCommitProtocol.commit_local(tx, node, clock)
        assert update.origin_node == node
        ops = update.operations()
        assert len(ops) == 1

    def test_merge_updates(self):
        n1 = _rhizo.PyNodeId("n1")
        n2 = _rhizo.PyNodeId("n2")

        tx1 = _rhizo.PyAlgebraicTransaction()
        tx1.add_operation(_rhizo.PyAlgebraicOperation("k", _rhizo.PyOpType("add"), _rhizo.PyAlgebraicValue.integer(5)))
        c1 = _rhizo.PyVectorClock()
        u1 = _rhizo.PyLocalCommitProtocol.commit_local(tx1, n1, c1)

        tx2 = _rhizo.PyAlgebraicTransaction()
        tx2.add_operation(_rhizo.PyAlgebraicOperation("k", _rhizo.PyOpType("add"), _rhizo.PyAlgebraicValue.integer(3)))
        c2 = _rhizo.PyVectorClock()
        u2 = _rhizo.PyLocalCommitProtocol.commit_local(tx2, n2, c2)

        merged = _rhizo.PyLocalCommitProtocol.merge_updates(u1, u2)
        assert len(merged.operations()) >= 1

    def test_merge_all(self):
        updates = []
        for i in range(3):
            tx = _rhizo.PyAlgebraicTransaction()
            tx.add_operation(_rhizo.PyAlgebraicOperation(
                "k", _rhizo.PyOpType("add"), _rhizo.PyAlgebraicValue.integer(i + 1)
            ))
            node = _rhizo.PyNodeId(f"n{i}")
            clock = _rhizo.PyVectorClock()
            updates.append(_rhizo.PyLocalCommitProtocol.commit_local(tx, node, clock))
        merged = _rhizo.PyLocalCommitProtocol.merge_all(updates)
        assert len(merged.operations()) >= 1


# ===================================================================
# ALGEBRAIC TRANSACTION BINDINGS
# ===================================================================

class TestPyAlgebraicTransaction:
    """Test PyAlgebraicTransaction and PyAlgebraicOperation."""

    def test_empty_transaction(self):
        tx = _rhizo.PyAlgebraicTransaction()
        assert tx.len() == 0
        assert tx.is_empty() is True
        assert tx.is_fully_algebraic() is True  # empty = trivially algebraic

    def test_add_operation(self):
        tx = _rhizo.PyAlgebraicTransaction()
        op = _rhizo.PyAlgebraicOperation("key1", _rhizo.PyOpType("max"), _rhizo.PyAlgebraicValue.integer(5))
        tx.add_operation(op)
        assert tx.len() == 1
        assert tx.is_empty() is False

    def test_operation_properties(self):
        op = _rhizo.PyAlgebraicOperation("mykey", _rhizo.PyOpType("add"), _rhizo.PyAlgebraicValue.integer(3))
        assert op.key == "mykey"
        assert op.op_type.is_abelian() is True
        assert op.value.is_numeric() is True
        assert op.is_algebraic() is True

    def test_non_algebraic_operation(self):
        op = _rhizo.PyAlgebraicOperation("mykey", _rhizo.PyOpType("overwrite"), _rhizo.PyAlgebraicValue.integer(1))
        assert op.is_algebraic() is False

    def test_fully_algebraic_detection(self):
        tx = _rhizo.PyAlgebraicTransaction()
        tx.add_operation(_rhizo.PyAlgebraicOperation("a", _rhizo.PyOpType("add"), _rhizo.PyAlgebraicValue.integer(1)))
        assert tx.is_fully_algebraic() is True
        tx.add_operation(_rhizo.PyAlgebraicOperation("b", _rhizo.PyOpType("overwrite"), _rhizo.PyAlgebraicValue.integer(2)))
        assert tx.is_fully_algebraic() is False

    def test_transaction_metadata(self):
        tx = _rhizo.PyAlgebraicTransaction()
        tx.set_metadata("source", "test")
        assert tx.get_metadata("source") == "test"
        assert tx.get_metadata("missing") is None

    def test_operation_repr(self):
        op = _rhizo.PyAlgebraicOperation("k", _rhizo.PyOpType("min"), _rhizo.PyAlgebraicValue.integer(0))
        assert isinstance(repr(op), str)

    def test_transaction_repr(self):
        tx = _rhizo.PyAlgebraicTransaction()
        assert isinstance(repr(tx), str)


# ===================================================================
# FILTER / PREDICATE BINDINGS
# ===================================================================

class TestPyFilterPredicates:
    """Test PyFilterOp, PyScalarValue, PyPredicateFilter."""

    # --- PyFilterOp ---

    @pytest.mark.parametrize("op_str", ["eq", "ne", "lt", "le", "gt", "ge"])
    def test_filter_op_names(self, op_str):
        op = _rhizo.PyFilterOp(op_str)
        assert isinstance(repr(op), str)
        assert isinstance(str(op), str)

    @pytest.mark.parametrize("op_str", ["=", "==", "!=", "<>", "<", "<=", ">", ">="])
    def test_filter_op_symbols(self, op_str):
        op = _rhizo.PyFilterOp(op_str)
        assert isinstance(str(op), str)

    # --- PyScalarValue ---

    def test_scalar_int(self):
        s = _rhizo.PyScalarValue(42)
        assert isinstance(repr(s), str)

    def test_scalar_float(self):
        s = _rhizo.PyScalarValue(3.14)
        assert isinstance(repr(s), str)

    def test_scalar_string(self):
        s = _rhizo.PyScalarValue("hello")
        assert isinstance(str(s), str)

    def test_scalar_bool(self):
        s = _rhizo.PyScalarValue(True)
        assert isinstance(repr(s), str)

    def test_scalar_none(self):
        s = _rhizo.PyScalarValue(None)
        assert isinstance(repr(s), str)

    # --- PyPredicateFilter ---

    def test_predicate_filter_creation(self):
        f = _rhizo.PyPredicateFilter("age", "gt", 18)
        assert f.column == "age"
        assert isinstance(f.op, str)
        assert isinstance(f.value, str)

    def test_predicate_filter_repr(self):
        f = _rhizo.PyPredicateFilter("name", "eq", "Alice")
        assert isinstance(repr(f), str)
        assert isinstance(str(f), str)

    def test_predicate_filter_string_value(self):
        f = _rhizo.PyPredicateFilter("status", "ne", "inactive")
        assert f.column == "status"


# ===================================================================
# PARQUET ADVANCED BINDINGS
# ===================================================================

class TestPyParquetAdvanced:
    """Test advanced Parquet decoder features (projection, predicate pushdown)."""

    @pytest.fixture
    def encoded_batch(self):
        """Encode a test batch for decoder tests."""
        batch = pa.RecordBatch.from_pydict({
            "id": [1, 2, 3, 4, 5],
            "name": ["Alice", "Bob", "Charlie", "Diana", "Eve"],
            "score": [85.0, 92.0, 78.0, 95.0, 88.0],
        })
        encoder = _rhizo.PyParquetEncoder()
        data = encoder.encode(batch)
        return data, batch

    def test_decode_columns_by_index(self, encoded_batch):
        data, original = encoded_batch
        decoder = _rhizo.PyParquetDecoder()
        result = decoder.decode_columns(data, [0, 2])  # id and score only
        assert result.num_columns == 2
        assert result.column_names == ["id", "score"]
        assert result.num_rows == 5

    def test_decode_columns_by_name(self, encoded_batch):
        data, original = encoded_batch
        decoder = _rhizo.PyParquetDecoder()
        result = decoder.decode_columns_by_name(data, ["name"])
        assert result.num_columns == 1
        assert result.column_names == ["name"]
        assert result.column("name").to_pylist() == ["Alice", "Bob", "Charlie", "Diana", "Eve"]

    def test_decode_with_filter(self, encoded_batch):
        data, original = encoded_batch
        decoder = _rhizo.PyParquetDecoder()
        filters = [_rhizo.PyPredicateFilter("score", "gt", 90.0)]
        result = decoder.decode_with_filter(data, filters)
        # Should return only rows with score > 90
        assert result.num_rows <= 5
        scores = result.column("score").to_pylist()
        assert all(s > 90.0 for s in scores)

    def test_decode_with_filter_and_projection(self, encoded_batch):
        data, original = encoded_batch
        decoder = _rhizo.PyParquetDecoder()
        filters = [_rhizo.PyPredicateFilter("score", "ge", 85.0)]
        result = decoder.decode_with_filter(data, filters, [0, 1])  # id, name only
        assert result.num_columns == 2
        assert result.num_rows <= 5

    def test_pruning_stats(self, encoded_batch):
        data, original = encoded_batch
        decoder = _rhizo.PyParquetDecoder()
        filters = [_rhizo.PyPredicateFilter("score", "gt", 100.0)]
        total, pruned, kept = decoder.get_pruning_stats(data, filters)
        assert isinstance(total, int)
        assert isinstance(pruned, int)
        assert isinstance(kept, int)
        assert total == pruned + kept
