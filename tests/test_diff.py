"""
Tests for Version & Branch Diff — rhizo.diff module.

Covers:
  - Schema diff (5 tests)
  - Row diff basics (8 tests)
  - Modified row detail (5 tests)
  - Merkle optimization (5 tests)
  - Semantic diffs (5 tests)
  - Branch diff (5 tests)
  - Edge cases (7 tests)
  - Display / summary (5 tests)
  - Database.diff() integration (5 tests)
"""

from __future__ import annotations

import os
import shutil
import tempfile

import pandas as pd
import pyarrow as pa
import pytest

import _rhizo
import rhizo
from rhizo.diff import DiffEngine, DiffResult, SchemaDiff, RowDiff


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def temp_dir():
    d = tempfile.mkdtemp(prefix="rhizo_diff_test_")
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def diff_env(temp_dir):
    """Low-level diff environment with catalog, store, reader, branches."""
    chunks_dir = os.path.join(temp_dir, "chunks")
    catalog_dir = os.path.join(temp_dir, "catalog")
    branches_dir = os.path.join(temp_dir, "branches")
    tx_dir = os.path.join(temp_dir, "transactions")

    store = _rhizo.PyChunkStore(chunks_dir)
    catalog = _rhizo.PyCatalog(catalog_dir)
    branch_mgr = _rhizo.PyBranchManager(branches_dir)
    tx_mgr = _rhizo.PyTransactionManager(tx_dir, catalog_dir, branches_dir)

    try:
        branch_mgr.create("main")
    except (ValueError, OSError):
        pass

    from rhizo.reader import TableReader
    reader = TableReader(store, catalog)

    yield {
        "store": store,
        "catalog": catalog,
        "branch_mgr": branch_mgr,
        "tx_mgr": tx_mgr,
        "reader": reader,
        "temp_dir": temp_dir,
    }


def _write(env, table_name, data_dict, metadata=None):
    """Helper: write a DataFrame as a new version."""
    from rhizo.writer import TableWriter
    writer = TableWriter(env["store"], env["catalog"])
    df = pd.DataFrame(data_dict)
    return writer.write(table_name, df, metadata=metadata)


def _make_diff_engine(env):
    return DiffEngine(
        catalog=env["catalog"],
        store=env["store"],
        reader=env["reader"],
        branch_manager=env["branch_mgr"],
    )


# ===========================================================================
# TestSchemaDiff
# ===========================================================================

class TestSchemaDiff:
    """Schema-level diff — 5 tests."""

    def test_no_schema_changes(self, diff_env):
        _write(diff_env, "t", {"id": [1, 2], "name": ["A", "B"]})
        _write(diff_env, "t", {"id": [3, 4], "name": ["C", "D"]})

        engine = _make_diff_engine(diff_env)
        result = engine.diff("t", 1, 2)
        assert not result.schema.has_changes
        assert result.schema.unchanged_columns == ["id", "name"]

    def test_added_columns(self, diff_env):
        _write(diff_env, "t", {"id": [1]})
        _write(diff_env, "t", {"id": [1], "name": ["A"]})

        engine = _make_diff_engine(diff_env)
        result = engine.diff("t", 1, 2)
        assert len(result.schema.added_columns) == 1
        assert result.schema.added_columns[0][0] == "name"

    def test_removed_columns(self, diff_env):
        _write(diff_env, "t", {"id": [1], "name": ["A"]})
        _write(diff_env, "t", {"id": [1]})

        engine = _make_diff_engine(diff_env)
        result = engine.diff("t", 1, 2)
        assert len(result.schema.removed_columns) == 1
        assert result.schema.removed_columns[0][0] == "name"

    def test_type_changes(self, diff_env):
        _write(diff_env, "t", {"id": [1], "val": [10]})
        _write(diff_env, "t", {"id": [1], "val": [10.5]})

        engine = _make_diff_engine(diff_env)
        result = engine.diff("t", 1, 2)
        assert len(result.schema.type_changes) == 1
        assert result.schema.type_changes[0][0] == "val"

    def test_unchanged_columns_listed(self, diff_env):
        _write(diff_env, "t", {"id": [1], "name": ["A"], "score": [10]})
        _write(diff_env, "t", {"id": [2], "name": ["B"], "score": [20]})

        engine = _make_diff_engine(diff_env)
        result = engine.diff("t", 1, 2)
        assert sorted(result.schema.unchanged_columns) == ["id", "name", "score"]


# ===========================================================================
# TestRowDiffBasic
# ===========================================================================

class TestRowDiffBasic:
    """Row-level diff basics — 8 tests."""

    def test_no_changes(self, diff_env):
        """Identical data → no row changes."""
        _write(diff_env, "t", {"id": [1, 2], "v": [10, 20]})
        _write(diff_env, "t", {"id": [1, 2], "v": [10, 20]})

        engine = _make_diff_engine(diff_env)
        result = engine.diff("t", 1, 2, key_columns=["id"])
        assert result.rows.added.num_rows == 0
        assert result.rows.removed.num_rows == 0
        assert result.rows.modified.num_rows == 0
        assert result.rows.unchanged_count == 2

    def test_all_rows_added(self, diff_env):
        """v1 has one sentinel row, v2 has sentinel + 3 new rows → 3 added."""
        _write(diff_env, "t", {"id": [0], "v": [0]})
        _write(diff_env, "t", {"id": [0, 1, 2, 3], "v": [0, 10, 20, 30]})

        engine = _make_diff_engine(diff_env)
        result = engine.diff("t", 1, 2, key_columns=["id"])
        assert result.rows.added.num_rows == 3
        assert result.rows.removed.num_rows == 0
        assert result.rows.unchanged_count == 1

    def test_all_rows_removed(self, diff_env):
        """v1 has sentinel + 3 data rows, v2 has only sentinel → 3 removed."""
        _write(diff_env, "t", {"id": [0, 1, 2, 3], "v": [0, 10, 20, 30]})
        _write(diff_env, "t", {"id": [0], "v": [0]})

        engine = _make_diff_engine(diff_env)
        result = engine.diff("t", 1, 2, key_columns=["id"])
        assert result.rows.added.num_rows == 0
        assert result.rows.removed.num_rows == 3
        assert result.rows.unchanged_count == 1

    def test_rows_added(self, diff_env):
        """Some new rows in v2."""
        _write(diff_env, "t", {"id": [1, 2], "v": [10, 20]})
        _write(diff_env, "t", {"id": [1, 2, 3, 4], "v": [10, 20, 30, 40]})

        engine = _make_diff_engine(diff_env)
        result = engine.diff("t", 1, 2, key_columns=["id"])
        assert result.rows.added.num_rows == 2
        assert result.rows.removed.num_rows == 0

    def test_rows_removed(self, diff_env):
        """Some rows missing in v2."""
        _write(diff_env, "t", {"id": [1, 2, 3], "v": [10, 20, 30]})
        _write(diff_env, "t", {"id": [1], "v": [10]})

        engine = _make_diff_engine(diff_env)
        result = engine.diff("t", 1, 2, key_columns=["id"])
        assert result.rows.removed.num_rows == 2
        assert result.rows.added.num_rows == 0

    def test_rows_modified(self, diff_env):
        """Same keys, different values."""
        _write(diff_env, "t", {"id": [1, 2, 3], "v": [10, 20, 30]})
        _write(diff_env, "t", {"id": [1, 2, 3], "v": [10, 25, 35]})

        engine = _make_diff_engine(diff_env)
        result = engine.diff("t", 1, 2, key_columns=["id"])
        assert result.rows.modified.num_rows == 2  # id=2 and id=3
        assert result.rows.unchanged_count == 1    # id=1

    def test_mixed_add_remove_modify(self, diff_env):
        """Realistic: some added, removed, modified."""
        _write(diff_env, "t", {"id": [1, 2, 3], "name": ["A", "B", "C"], "score": [10, 20, 30]})
        _write(diff_env, "t", {"id": [1, 3, 4], "name": ["A", "C2", "D"], "score": [10, 35, 40]})

        engine = _make_diff_engine(diff_env)
        result = engine.diff("t", 1, 2, key_columns=["id"])
        assert result.rows.added.num_rows == 1    # id=4
        assert result.rows.removed.num_rows == 1  # id=2
        assert result.rows.modified.num_rows == 1 # id=3 (name+score changed)
        assert result.rows.unchanged_count == 1   # id=1

    def test_unchanged_count_correct(self, diff_env):
        """Verify unchanged_count = total - removed - modified."""
        _write(diff_env, "t", {"id": list(range(10)), "v": list(range(10))})
        # Modify v[5] and v[7], remove v[9], add v[10]
        new_v = list(range(10))
        new_v[5] = 999
        new_v[7] = 888
        ids = list(range(9)) + [10]  # remove 9, add 10
        vals = new_v[:9] + [100]
        _write(diff_env, "t", {"id": ids, "v": vals})

        engine = _make_diff_engine(diff_env)
        result = engine.diff("t", 1, 2, key_columns=["id"])
        assert result.rows.unchanged_count == 7  # 10 - 1 removed - 2 modified


# ===========================================================================
# TestModifiedRowsDetail
# ===========================================================================

class TestModifiedRowsDetail:
    """Modified row detail columns — 5 tests."""

    def test_modified_table_has_old_new_columns(self, diff_env):
        """Modified table has __old_ and __new_ prefixed columns."""
        _write(diff_env, "t", {"id": [1], "name": ["A"], "score": [10]})
        _write(diff_env, "t", {"id": [1], "name": ["B"], "score": [20]})

        engine = _make_diff_engine(diff_env)
        result = engine.diff("t", 1, 2, key_columns=["id"])
        cols = result.rows.modified.column_names
        assert "id" in cols
        assert "__old_name" in cols
        assert "__new_name" in cols
        assert "__old_score" in cols
        assert "__new_score" in cols

    def test_single_column_change(self, diff_env):
        """Only the changed column has different old/new values."""
        _write(diff_env, "t", {"id": [1], "a": [10], "b": [20]})
        _write(diff_env, "t", {"id": [1], "a": [10], "b": [25]})

        engine = _make_diff_engine(diff_env)
        result = engine.diff("t", 1, 2, key_columns=["id"])
        mod = result.rows.modified
        assert mod.num_rows == 1
        # b changed
        assert mod.column("__old_b")[0].as_py() == 20
        assert mod.column("__new_b")[0].as_py() == 25
        # a unchanged but still present in modified table
        assert mod.column("__old_a")[0].as_py() == 10
        assert mod.column("__new_a")[0].as_py() == 10

    def test_multiple_columns_changed(self, diff_env):
        """Multiple columns changed in same row."""
        _write(diff_env, "t", {"id": [1], "x": [10], "y": [20], "z": [30]})
        _write(diff_env, "t", {"id": [1], "x": [11], "y": [22], "z": [30]})

        engine = _make_diff_engine(diff_env)
        result = engine.diff("t", 1, 2, key_columns=["id"])
        mod = result.rows.modified
        assert mod.num_rows == 1
        assert mod.column("__old_x")[0].as_py() == 10
        assert mod.column("__new_x")[0].as_py() == 11
        assert mod.column("__old_y")[0].as_py() == 20
        assert mod.column("__new_y")[0].as_py() == 22

    def test_null_to_value_change(self, diff_env):
        """NULL → value is detected as modification."""
        _write(diff_env, "t", {"id": [1], "v": pa.array([None], type=pa.int64())})
        _write(diff_env, "t", {"id": [1], "v": [42]})

        engine = _make_diff_engine(diff_env)
        result = engine.diff("t", 1, 2, key_columns=["id"])
        assert result.rows.modified.num_rows == 1

    def test_value_to_null_change(self, diff_env):
        """Value → NULL is detected as modification."""
        _write(diff_env, "t", {"id": [1], "v": [42]})
        _write(diff_env, "t", {"id": [1], "v": pa.array([None], type=pa.int64())})

        engine = _make_diff_engine(diff_env)
        result = engine.diff("t", 1, 2, key_columns=["id"])
        assert result.rows.modified.num_rows == 1


# ===========================================================================
# TestMerkleOptimization
# ===========================================================================

class TestMerkleOptimization:
    """Merkle chunk optimization — 5 tests."""

    def test_identical_chunks_fast_path(self, diff_env):
        """Same data → chunks_scanned=0, all skipped."""
        data = {"id": list(range(100)), "v": list(range(100))}
        _write(diff_env, "t", data)
        _write(diff_env, "t", data)

        engine = _make_diff_engine(diff_env)
        result = engine.diff("t", 1, 2, key_columns=["id"])
        assert result.chunks_scanned == 0
        assert result.chunks_skipped > 0
        assert result.rows.unchanged_count == 100

    def test_partial_overlap_stats(self, diff_env):
        """Different data → chunks_scanned > 0."""
        _write(diff_env, "t", {"id": list(range(100)), "v": list(range(100))})
        _write(diff_env, "t", {"id": list(range(100)), "v": list(range(100, 200))})

        engine = _make_diff_engine(diff_env)
        result = engine.diff("t", 1, 2)
        assert result.chunks_scanned > 0

    def test_no_overlap_all_scanned(self, diff_env):
        """Completely different data → all chunks scanned."""
        _write(diff_env, "t", {"id": [1], "v": [10]})
        _write(diff_env, "t", {"id": [2], "v": [20]})

        engine = _make_diff_engine(diff_env)
        result = engine.diff("t", 1, 2)
        assert result.chunks_skipped == 0
        assert result.chunks_scanned == 2

    def test_same_data_rewrite_fast_path(self, diff_env):
        """Writing identical data produces identical chunk hashes → fast path."""
        data = {"id": list(range(50)), "v": list(range(50))}
        _write(diff_env, "t", data)
        _write(diff_env, "t", data)

        engine = _make_diff_engine(diff_env)
        result = engine.diff("t", 1, 2, key_columns=["id"])
        assert result.chunks_scanned == 0
        assert result.rows.added.num_rows == 0
        assert result.rows.removed.num_rows == 0
        assert result.rows.modified.num_rows == 0

    def test_chunk_counts_match_metadata(self, diff_env):
        """chunks_a and chunks_b match version metadata."""
        _write(diff_env, "t", {"id": list(range(100)), "v": list(range(100))})
        _write(diff_env, "t", {"id": list(range(200)), "v": list(range(200))})

        engine = _make_diff_engine(diff_env)
        result = engine.diff("t", 1, 2)
        meta_a = diff_env["catalog"].get_version("t", 1)
        meta_b = diff_env["catalog"].get_version("t", 2)
        assert result.chunks_a == len(meta_a.chunk_hashes)
        assert result.chunks_b == len(meta_b.chunk_hashes)


# ===========================================================================
# TestSemanticDiffs
# ===========================================================================

class TestSemanticDiffs:
    """Semantic diffs with algebraic schemas — 5 tests."""

    def test_additive_counter_delta(self, diff_env):
        """Abelian add column shows increment."""
        _write(diff_env, "t", {"id": [1], "count": [100]})
        _write(diff_env, "t", {"id": [1], "count": [147]})

        schema = _rhizo.PyTableAlgebraicSchema("t")
        schema.add_column("count", _rhizo.PyOpType("add"))

        engine = _make_diff_engine(diff_env)
        result = engine.diff("t", 1, 2, key_columns=["id"], schema=schema)
        assert result.semantic_changes is not None
        assert "count" in result.semantic_changes
        assert "incremented by 47" in result.semantic_changes["count"][0]

    def test_max_column_description(self, diff_env):
        """Max column shows new maximum."""
        _write(diff_env, "t", {"id": [1], "high_score": [150]})
        _write(diff_env, "t", {"id": [1], "high_score": [200]})

        schema = _rhizo.PyTableAlgebraicSchema("t")
        schema.add_column("high_score", _rhizo.PyOpType("max"))

        engine = _make_diff_engine(diff_env)
        result = engine.diff("t", 1, 2, key_columns=["id"], schema=schema)
        assert result.semantic_changes is not None
        assert "new maximum" in result.semantic_changes["high_score"][0]

    def test_overwrite_column_shows_change(self, diff_env):
        """Non-algebraic column shows raw change."""
        _write(diff_env, "t", {"id": [1], "name": ["Alice"]})
        _write(diff_env, "t", {"id": [1], "name": ["Bob"]})

        schema = _rhizo.PyTableAlgebraicSchema("t")
        schema.add_column("name", _rhizo.PyOpType("overwrite"))

        engine = _make_diff_engine(diff_env)
        result = engine.diff("t", 1, 2, key_columns=["id"], schema=schema)
        assert result.semantic_changes is not None
        assert "changed from" in result.semantic_changes["name"][0]

    def test_no_schema_no_semantics(self, diff_env):
        """Without schema, semantic_changes is None."""
        _write(diff_env, "t", {"id": [1], "v": [10]})
        _write(diff_env, "t", {"id": [1], "v": [20]})

        engine = _make_diff_engine(diff_env)
        result = engine.diff("t", 1, 2, key_columns=["id"])
        assert result.semantic_changes is None

    def test_mixed_algebraic_types(self, diff_env):
        """Multiple columns with different op types."""
        _write(diff_env, "t", {"id": [1], "count": [10], "max_val": [50], "label": ["x"]})
        _write(diff_env, "t", {"id": [1], "count": [15], "max_val": [60], "label": ["y"]})

        schema = _rhizo.PyTableAlgebraicSchema("t")
        schema.add_column("count", _rhizo.PyOpType("add"))
        schema.add_column("max_val", _rhizo.PyOpType("max"))
        schema.add_column("label", _rhizo.PyOpType("overwrite"))

        engine = _make_diff_engine(diff_env)
        result = engine.diff("t", 1, 2, key_columns=["id"], schema=schema)
        assert "count" in result.semantic_changes
        assert "max_val" in result.semantic_changes
        assert "label" in result.semantic_changes


# ===========================================================================
# TestBranchDiff
# ===========================================================================

class TestBranchDiff:
    """Branch diff — 5 tests."""

    def test_diff_between_branches(self, diff_env):
        """Basic branch diff resolves versions correctly."""
        _write(diff_env, "t", {"id": [1, 2], "v": [10, 20]})
        _write(diff_env, "t", {"id": [1, 2], "v": [10, 25]})

        # Set branch heads
        diff_env["branch_mgr"].update_head("main", "t", 1)
        diff_env["branch_mgr"].create("feature", from_branch="main")
        diff_env["branch_mgr"].update_head("feature", "t", 2)

        engine = _make_diff_engine(diff_env)
        # Diff via branch manager version resolution
        v_main = diff_env["branch_mgr"].get_table_version("main", "t")
        v_feat = diff_env["branch_mgr"].get_table_version("feature", "t")
        result = engine.diff("t", v_main, v_feat, key_columns=["id"])
        assert result.rows.modified.num_rows == 1

    def test_branch_same_table_version(self, diff_env):
        """Branches at same version → no diff."""
        _write(diff_env, "t", {"id": [1], "v": [10]})

        diff_env["branch_mgr"].update_head("main", "t", 1)
        diff_env["branch_mgr"].create("feature", from_branch="main")

        v_main = diff_env["branch_mgr"].get_table_version("main", "t")
        v_feat = diff_env["branch_mgr"].get_table_version("feature", "t")

        engine = _make_diff_engine(diff_env)
        result = engine.diff("t", v_main, v_feat, key_columns=["id"])
        assert result.rows.unchanged_count == 1
        assert result.rows.modified.num_rows == 0

    def test_branch_with_key_columns(self, diff_env):
        """Full row-level branch diff with key columns."""
        _write(diff_env, "t", {"id": [1, 2, 3], "v": [10, 20, 30]})
        _write(diff_env, "t", {"id": [1, 2, 4], "v": [10, 25, 40]})

        diff_env["branch_mgr"].update_head("main", "t", 1)
        diff_env["branch_mgr"].create("feature", from_branch="main")
        diff_env["branch_mgr"].update_head("feature", "t", 2)

        v_main = diff_env["branch_mgr"].get_table_version("main", "t")
        v_feat = diff_env["branch_mgr"].get_table_version("feature", "t")

        engine = _make_diff_engine(diff_env)
        result = engine.diff("t", v_main, v_feat, key_columns=["id"])
        assert result.rows.added.num_rows == 1    # id=4
        assert result.rows.removed.num_rows == 1  # id=3
        assert result.rows.modified.num_rows == 1 # id=2

    def test_db_diff_branch_params(self, temp_dir):
        """Database.diff() with branch_a/branch_b params."""
        with rhizo.open(temp_dir) as db:
            db.write("t", pd.DataFrame({"id": [1, 2], "v": [10, 20]}))  # v1
            db.write("t", pd.DataFrame({"id": [1, 2], "v": [15, 20]}))  # v2
            # Set branch heads after all writes to avoid auto-update interference
            db._branch_manager.update_head("main", "t", 1)
            db._branch_manager.create("feature", from_branch="main")
            db._branch_manager.update_head("feature", "t", 2)

            diff = db.diff("t", branch_a="main", branch_b="feature", key_columns=["id"])
            assert diff.rows.modified.num_rows == 1

    def test_branch_version_conflict_raises(self, temp_dir):
        """Cannot specify both version and branch params."""
        with rhizo.open(temp_dir) as db:
            db.write("t", pd.DataFrame({"id": [1]}))
            with pytest.raises(ValueError, match="Cannot specify both"):
                db.diff("t", version_a=1, branch_b="main")


# ===========================================================================
# TestEdgeCases
# ===========================================================================

class TestEdgeCases:
    """Edge cases — 7 tests."""

    def test_minimal_identical_tables(self, diff_env):
        """Both versions have same single row → no changes."""
        _write(diff_env, "t", {"id": [1]})
        _write(diff_env, "t", {"id": [1]})

        engine = _make_diff_engine(diff_env)
        result = engine.diff("t", 1, 2, key_columns=["id"])
        assert result.rows.added.num_rows == 0
        assert result.rows.removed.num_rows == 0
        assert result.rows.unchanged_count == 1

    def test_single_row(self, diff_env):
        """Single row diff works."""
        _write(diff_env, "t", {"id": [1], "v": [10]})
        _write(diff_env, "t", {"id": [1], "v": [20]})

        engine = _make_diff_engine(diff_env)
        result = engine.diff("t", 1, 2, key_columns=["id"])
        assert result.rows.modified.num_rows == 1

    def test_same_version_diff(self, diff_env):
        """Diffing a version against itself → empty diff."""
        _write(diff_env, "t", {"id": [1, 2, 3], "v": [10, 20, 30]})

        engine = _make_diff_engine(diff_env)
        result = engine.diff("t", 1, 1, key_columns=["id"])
        assert result.rows.added.num_rows == 0
        assert result.rows.removed.num_rows == 0
        assert result.rows.modified.num_rows == 0
        assert result.rows.unchanged_count == 3

    def test_missing_key_column_raises(self, diff_env):
        """Key column not in table → ValueError."""
        _write(diff_env, "t", {"id": [1], "v": [10]})
        _write(diff_env, "t", {"id": [1], "v": [20]})

        engine = _make_diff_engine(diff_env)
        with pytest.raises(ValueError, match="not found"):
            engine.diff("t", 1, 2, key_columns=["nonexistent"])

    def test_no_key_columns_stats_only(self, diff_env):
        """Without key_columns, only schema and counts returned."""
        _write(diff_env, "t", {"id": [1, 2], "v": [10, 20]})
        _write(diff_env, "t", {"id": [1, 2, 3], "v": [10, 20, 30]})

        engine = _make_diff_engine(diff_env)
        result = engine.diff("t", 1, 2)
        assert result.rows is None
        assert result.rows_a == 2
        assert result.rows_b == 3

    def test_large_key_space(self, diff_env):
        """Diff with many unique keys works correctly."""
        n = 1000
        _write(diff_env, "t", {"id": list(range(n)), "v": list(range(n))})
        # Modify 10%, add 5%, remove 5%
        new_ids = list(range(50, n)) + list(range(n, n + 50))
        new_vals = [v + 1 if v < 150 else v for v in range(50, n)] + list(range(n, n + 50))
        _write(diff_env, "t", {"id": new_ids, "v": new_vals})

        engine = _make_diff_engine(diff_env)
        result = engine.diff("t", 1, 2, key_columns=["id"])
        assert result.rows.added.num_rows == 50    # ids n..n+50
        assert result.rows.removed.num_rows == 50  # ids 0..50
        assert result.rows.modified.num_rows == 100 # ids 50..150

    def test_version_not_found_raises(self, diff_env):
        """Invalid version → error."""
        _write(diff_env, "t", {"id": [1]})

        engine = _make_diff_engine(diff_env)
        with pytest.raises(Exception):
            engine.diff("t", 1, 999)


# ===========================================================================
# TestDiffDisplay
# ===========================================================================

class TestDiffDisplay:
    """Display and summary — 5 tests."""

    def test_summary_format(self, diff_env):
        """summary() returns a multi-line string."""
        _write(diff_env, "t", {"id": [1, 2], "v": [10, 20]})
        _write(diff_env, "t", {"id": [1, 3], "v": [15, 30]})

        engine = _make_diff_engine(diff_env)
        result = engine.diff("t", 1, 2, key_columns=["id"])
        s = result.summary()
        assert "Diff: t v1 -> v2" in s
        assert "Schema:" in s
        assert "Rows:" in s

    def test_repr_not_empty(self, diff_env):
        """__repr__ produces output."""
        _write(diff_env, "t", {"id": [1], "v": [10]})
        _write(diff_env, "t", {"id": [1], "v": [20]})

        engine = _make_diff_engine(diff_env)
        result = engine.diff("t", 1, 2, key_columns=["id"])
        assert len(repr(result)) > 0

    def test_summary_without_row_diff(self, diff_env):
        """summary() works without key_columns."""
        _write(diff_env, "t", {"id": [1], "v": [10]})
        _write(diff_env, "t", {"id": [1, 2], "v": [10, 20]})

        engine = _make_diff_engine(diff_env)
        result = engine.diff("t", 1, 2)
        s = result.summary()
        assert "Rows:" in s
        assert "v1" in s

    def test_summary_with_semantics(self, diff_env):
        """summary() includes semantic changes."""
        _write(diff_env, "t", {"id": [1], "count": [100]})
        _write(diff_env, "t", {"id": [1], "count": [150]})

        schema = _rhizo.PyTableAlgebraicSchema("t")
        schema.add_column("count", _rhizo.PyOpType("add"))

        engine = _make_diff_engine(diff_env)
        result = engine.diff("t", 1, 2, key_columns=["id"], schema=schema)
        s = result.summary()
        assert "incremented by 50" in s

    def test_summary_merkle_stats(self, diff_env):
        """summary() includes chunk/Merkle stats."""
        _write(diff_env, "t", {"id": [1], "v": [10]})
        _write(diff_env, "t", {"id": [2], "v": [20]})

        engine = _make_diff_engine(diff_env)
        result = engine.diff("t", 1, 2)
        s = result.summary()
        assert "Chunks:" in s


# ===========================================================================
# TestDatabaseDiff
# ===========================================================================

class TestDatabaseDiff:
    """Database.diff() integration — 5 tests."""

    def test_db_diff_works(self, temp_dir):
        """Basic db.diff() call."""
        with rhizo.open(temp_dir) as db:
            db.write("t", pd.DataFrame({"id": [1, 2], "v": [10, 20]}))
            db.write("t", pd.DataFrame({"id": [1, 3], "v": [15, 30]}))

            result = db.diff("t", version_a=1, version_b=2, key_columns=["id"])
            assert isinstance(result, DiffResult)
            assert result.rows.added.num_rows == 1
            assert result.rows.removed.num_rows == 1
            assert result.rows.modified.num_rows == 1

    def test_db_diff_default_versions(self, temp_dir):
        """No versions = latest vs previous."""
        with rhizo.open(temp_dir) as db:
            db.write("t", pd.DataFrame({"id": [1], "v": [10]}))
            db.write("t", pd.DataFrame({"id": [1], "v": [20]}))

            result = db.diff("t", key_columns=["id"])
            assert result.version_a == 1
            assert result.version_b == 2
            assert result.rows.modified.num_rows == 1

    def test_db_diff_closed_raises(self, temp_dir):
        """diff() on closed DB raises RuntimeError."""
        db = rhizo.open(temp_dir)
        db.write("t", pd.DataFrame({"id": [1]}))
        db.write("t", pd.DataFrame({"id": [2]}))
        db.close()
        with pytest.raises(RuntimeError):
            db.diff("t", version_a=1, version_b=2)

    def test_db_diff_no_key_columns(self, temp_dir):
        """db.diff() without key_columns returns stats only."""
        with rhizo.open(temp_dir) as db:
            db.write("t", pd.DataFrame({"id": [1, 2], "v": [10, 20]}))
            db.write("t", pd.DataFrame({"id": [1, 2, 3], "v": [10, 20, 30]}))

            result = db.diff("t", version_a=1, version_b=2)
            assert result.rows is None
            assert result.rows_a == 2
            assert result.rows_b == 3

    def test_db_diff_full_cycle(self, temp_dir):
        """Write → modify → diff → verify full cycle."""
        with rhizo.open(temp_dir) as db:
            # Version 1: initial data
            db.write("users", pd.DataFrame({
                "id": [1, 2, 3],
                "name": ["Alice", "Bob", "Carol"],
                "score": [85, 92, 78],
            }))
            # Version 2: modify Bob, remove Carol, add Dave
            db.write("users", pd.DataFrame({
                "id": [1, 2, 4],
                "name": ["Alice", "Bob", "Dave"],
                "score": [85, 95, 88],
            }))

            diff = db.diff("users", version_a=1, version_b=2, key_columns=["id"])
            assert diff.rows.added.num_rows == 1    # Dave
            assert diff.rows.removed.num_rows == 1  # Carol
            assert diff.rows.modified.num_rows == 1 # Bob (score 92→95)
            assert diff.rows.unchanged_count == 1   # Alice

            # Verify modified row details
            mod = diff.rows.modified
            assert mod.column("__old_score")[0].as_py() == 92
            assert mod.column("__new_score")[0].as_py() == 95
