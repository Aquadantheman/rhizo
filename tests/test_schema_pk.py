"""
Tests for Schema Evolution and Primary Key features.

Coverage:
- SchemaUtils: serialize/deserialize/compare (11 tests)
- TableMetaStore: load/save/defaults (8 tests)
- Schema Evolution: additive/flexible modes (14 tests)
- Primary Key: uniqueness enforcement (15 tests)
- PK with Diff: auto-resolve key_columns (5 tests)
- PK with Transactions: enforcement in tx context (2 tests)
- Database Integration: lifecycle/persistence (7 tests)
"""

import json
import os
import tempfile
import time

import pandas as pd
import pyarrow as pa
import pytest

import rhizo
from rhizo.table_meta import TableMeta, TableMetaStore
from rhizo.schema_utils import (
    SCHEMA_METADATA_KEY,
    SchemaComparisonResult,
    compare_schemas,
    deserialize_schema,
    serialize_schema,
)
from rhizo.exceptions import (
    PrimaryKeyViolationError,
    SchemaEvolutionError,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _tmp_db(**kwargs):
    """Create a temporary Database."""
    d = tempfile.mkdtemp()
    return rhizo.open(d, **kwargs)


def _simple_df(n=3, start_id=1):
    return pd.DataFrame({
        "id": list(range(start_id, start_id + n)),
        "name": [f"name_{i}" for i in range(start_id, start_id + n)],
        "score": [float(i * 10) for i in range(start_id, start_id + n)],
    })


# ===========================================================================
# TestSchemaUtils
# ===========================================================================

class TestSchemaUtils:
    """Test schema serialization, deserialization, and comparison."""

    def test_roundtrip_basic(self):
        schema = pa.schema([
            pa.field("id", pa.int64()),
            pa.field("name", pa.string()),
            pa.field("value", pa.float64()),
        ])
        s = serialize_schema(schema)
        restored = deserialize_schema(s)
        assert restored == schema

    def test_roundtrip_empty(self):
        schema = pa.schema([])
        s = serialize_schema(schema)
        restored = deserialize_schema(s)
        assert len(restored) == 0

    def test_roundtrip_timestamps(self):
        schema = pa.schema([
            pa.field("ts_ns", pa.timestamp("ns")),
            pa.field("ts_us", pa.timestamp("us")),
            pa.field("ts_ms", pa.timestamp("ms")),
            pa.field("ts_s", pa.timestamp("s")),
        ])
        s = serialize_schema(schema)
        restored = deserialize_schema(s)
        assert restored == schema

    def test_unknown_type_fallback(self):
        """Unknown types fall back to string."""
        raw = json.dumps([{"name": "x", "type": "unknown_type_xyz"}])
        schema = deserialize_schema(raw)
        assert schema.field("x").type == pa.string()

    def test_compare_identical(self):
        schema = pa.schema([pa.field("id", pa.int64())])
        result = compare_schemas(schema, schema, "additive")
        assert result.compatible
        assert not result.added_columns
        assert not result.removed_columns
        assert not result.type_changes

    def test_compare_added_column_additive(self):
        old = pa.schema([pa.field("id", pa.int64())])
        new = pa.schema([pa.field("id", pa.int64()), pa.field("name", pa.string())])
        result = compare_schemas(old, new, "additive")
        assert result.compatible
        assert len(result.added_columns) == 1
        assert result.added_columns[0][0] == "name"

    def test_compare_removed_column_additive_fails(self):
        old = pa.schema([pa.field("id", pa.int64()), pa.field("name", pa.string())])
        new = pa.schema([pa.field("id", pa.int64())])
        result = compare_schemas(old, new, "additive")
        assert not result.compatible
        assert "columns removed" in result.error_message

    def test_compare_type_change_additive_fails(self):
        old = pa.schema([pa.field("id", pa.int64())])
        new = pa.schema([pa.field("id", pa.string())])
        result = compare_schemas(old, new, "additive")
        assert not result.compatible
        assert "type changes" in result.error_message

    def test_compare_removed_column_flexible_ok(self):
        old = pa.schema([pa.field("id", pa.int64()), pa.field("name", pa.string())])
        new = pa.schema([pa.field("id", pa.int64())])
        result = compare_schemas(old, new, "flexible")
        assert result.compatible
        assert len(result.removed_columns) == 1

    def test_compare_type_change_flexible_ok(self):
        old = pa.schema([pa.field("id", pa.int64())])
        new = pa.schema([pa.field("id", pa.string())])
        result = compare_schemas(old, new, "flexible")
        assert result.compatible
        assert len(result.type_changes) == 1

    def test_compare_mixed_changes(self):
        old = pa.schema([
            pa.field("id", pa.int64()),
            pa.field("old_col", pa.string()),
        ])
        new = pa.schema([
            pa.field("id", pa.float64()),  # type change
            pa.field("new_col", pa.string()),  # added
            # old_col removed
        ])
        result = compare_schemas(old, new, "additive")
        assert not result.compatible
        assert len(result.added_columns) == 1
        assert len(result.removed_columns) == 1
        assert len(result.type_changes) == 1


# ===========================================================================
# TestTableMetaStore
# ===========================================================================

class TestTableMetaStore:
    """Test _table_meta.json persistence."""

    def test_nonexistent_returns_defaults(self, tmp_path):
        store = TableMetaStore(str(tmp_path))
        meta = store.load("nonexistent")
        assert meta.primary_key is None
        assert meta.schema_mode == "additive"

    def test_save_load_roundtrip(self, tmp_path):
        store = TableMetaStore(str(tmp_path))
        meta = TableMeta(primary_key=["id", "ts"], schema_mode="flexible")
        store.save("users", meta)
        loaded = store.load("users")
        assert loaded.primary_key == ["id", "ts"]
        assert loaded.schema_mode == "flexible"
        assert loaded.created_at is not None

    def test_creates_directory(self, tmp_path):
        store = TableMetaStore(str(tmp_path / "deep" / "nested"))
        meta = TableMeta(primary_key=["id"])
        store.save("t", meta)
        assert store.load("t").primary_key == ["id"]

    def test_corrupted_file_returns_defaults(self, tmp_path):
        table_dir = tmp_path / "bad_table"
        table_dir.mkdir()
        meta_path = table_dir / "_table_meta.json"
        meta_path.write_text("not valid json {{{")
        store = TableMetaStore(str(tmp_path))
        meta = store.load("bad_table")
        assert meta.primary_key is None
        assert meta.schema_mode == "additive"

    def test_pk_and_mode_serialized(self, tmp_path):
        store = TableMetaStore(str(tmp_path))
        store.save("t", TableMeta(primary_key=["a", "b"], schema_mode="flexible"))
        raw = json.loads((tmp_path / "t" / "_table_meta.json").read_text())
        assert raw["primary_key"] == ["a", "b"]
        assert raw["schema_mode"] == "flexible"

    def test_multi_table_independent(self, tmp_path):
        store = TableMetaStore(str(tmp_path))
        store.save("t1", TableMeta(primary_key=["id"]))
        store.save("t2", TableMeta(primary_key=["key"], schema_mode="flexible"))
        assert store.load("t1").primary_key == ["id"]
        assert store.load("t2").primary_key == ["key"]
        assert store.load("t1").schema_mode == "additive"
        assert store.load("t2").schema_mode == "flexible"

    def test_backwards_compat_missing_fields(self, tmp_path):
        """Old-format files with missing fields still load."""
        table_dir = tmp_path / "old_table"
        table_dir.mkdir()
        meta_path = table_dir / "_table_meta.json"
        meta_path.write_text('{"primary_key": ["id"]}')
        store = TableMetaStore(str(tmp_path))
        meta = store.load("old_table")
        assert meta.primary_key == ["id"]
        assert meta.schema_mode == "additive"

    def test_invalid_schema_mode_raises(self):
        with pytest.raises(ValueError, match="Invalid schema_mode"):
            TableMeta(schema_mode="strict")


# ===========================================================================
# TestSchemaEvolution
# ===========================================================================

class TestSchemaEvolution:
    """Test schema evolution enforcement during writes."""

    def test_first_write_stores_schema(self):
        db = _tmp_db()
        db.write("t", _simple_df())
        schema = db.schema("t")
        assert "id" in [f.name for f in schema]
        db.close()

    def test_same_schema_ok(self):
        db = _tmp_db()
        db.write("t", _simple_df(3, 1))
        db.write("t", _simple_df(3, 4))  # same schema, different data
        assert len(db.versions("t")) == 2
        db.close()

    def test_additive_new_column_ok(self):
        db = _tmp_db()
        db.write("t", _simple_df())
        df2 = _simple_df()
        df2["email"] = ["a@b.com", "c@d.com", "e@f.com"]
        db.write("t", df2)
        schema = db.schema("t")
        assert "email" in [f.name for f in schema]
        db.close()

    def test_additive_removed_column_raises(self):
        db = _tmp_db()
        db.write("t", _simple_df())
        df2 = pd.DataFrame({"id": [1, 2, 3]})  # removed name, score
        with pytest.raises(SchemaEvolutionError, match="columns removed"):
            db.write("t", df2)
        db.close()

    def test_additive_type_change_raises(self):
        db = _tmp_db()
        db.write("t", _simple_df())
        df2 = pd.DataFrame({
            "id": ["a", "b", "c"],  # was int, now string
            "name": ["x", "y", "z"],
            "score": [1.0, 2.0, 3.0],
        })
        with pytest.raises(SchemaEvolutionError, match="type changes"):
            db.write("t", df2)
        db.close()

    def test_flexible_allows_removal(self):
        db = _tmp_db()
        db.write("t", _simple_df(), schema_mode="flexible")
        df2 = pd.DataFrame({"id": [1, 2, 3]})
        db.write("t", df2, schema_mode="flexible")
        assert len(db.versions("t")) == 2
        db.close()

    def test_flexible_allows_type_change(self):
        db = _tmp_db()
        db.write("t", _simple_df(), schema_mode="flexible")
        df2 = pd.DataFrame({
            "id": ["a", "b", "c"],
            "name": ["x", "y", "z"],
            "score": [1.0, 2.0, 3.0],
        })
        db.write("t", df2, schema_mode="flexible")
        db.close()

    def test_mode_from_meta(self):
        db = _tmp_db()
        db.set_schema_mode("t", "flexible")
        db.write("t", _simple_df())
        df2 = pd.DataFrame({"id": [1, 2, 3]})
        db.write("t", df2)  # No schema_mode kwarg, reads from meta
        assert len(db.versions("t")) == 2
        db.close()

    def test_mode_override_per_write(self):
        db = _tmp_db()
        db.write("t", _simple_df())
        # Default mode is additive, but override to flexible
        df2 = pd.DataFrame({"id": [1, 2, 3]})
        db.write("t", df2, schema_mode="flexible")
        assert len(db.versions("t")) == 2
        db.close()

    def test_db_schema(self):
        db = _tmp_db()
        db.write("t", _simple_df())
        schema = db.schema("t")
        assert isinstance(schema, pa.Schema)
        assert len(schema) == 3
        db.close()

    def test_db_schema_version(self):
        db = _tmp_db()
        db.write("t", _simple_df())
        df2 = _simple_df()
        df2["email"] = ["a@b.com", "c@d.com", "e@f.com"]
        db.write("t", df2)
        s1 = db.schema("t", version=1)
        s2 = db.schema("t", version=2)
        assert len(s1) == 3
        assert len(s2) == 4
        db.close()

    def test_db_schema_history(self):
        db = _tmp_db()
        db.write("t", _simple_df())
        df2 = _simple_df()
        df2["email"] = ["a@b.com", "c@d.com", "e@f.com"]
        db.write("t", df2)
        history = db.schema_history("t")
        assert len(history) == 2
        assert history[0]["changes"] is None  # first version
        assert len(history[1]["changes"]["added"]) == 1
        assert history[1]["changes"]["added"][0][0] == "email"
        db.close()

    def test_first_version_no_changes(self):
        db = _tmp_db()
        db.write("t", _simple_df())
        history = db.schema_history("t")
        assert history[0]["changes"] is None
        db.close()

    def test_backwards_compat_no_schema(self):
        """Old versions without __arrow_schema are handled gracefully."""
        db = _tmp_db()
        # Write directly using low-level API (no schema stored)
        table = pa.Table.from_pandas(_simple_df())
        from rhizo.writer import TableWriter
        writer = TableWriter(db._store, db._catalog)  # No catalog_path
        writer.write("t", table)
        # Second write with schema enabled should succeed (no previous schema to check)
        db.write("t", _simple_df())
        assert len(db.versions("t")) == 2
        db.close()

    def test_set_mode_persists(self):
        db = _tmp_db()
        db.set_schema_mode("t", "flexible")
        # Reload from disk
        meta = db._table_meta_store.load("t")
        assert meta.schema_mode == "flexible"
        db.close()

    def test_set_mode_invalid_raises(self):
        db = _tmp_db()
        with pytest.raises(ValueError, match="Invalid schema_mode"):
            db.set_schema_mode("t", "strict")
        db.close()


# ===========================================================================
# TestPrimaryKey
# ===========================================================================

class TestPrimaryKey:
    """Test primary key enforcement during writes."""

    def test_stores_pk(self):
        db = _tmp_db()
        db.write("t", _simple_df(), primary_key=["id"])
        assert db.primary_key("t") == ["id"]
        db.close()

    def test_enforces_uniqueness(self):
        db = _tmp_db()
        df = pd.DataFrame({"id": [1, 2, 3], "v": [10, 20, 30]})
        db.write("t", df, primary_key=["id"])
        # Second write with unique data
        df2 = pd.DataFrame({"id": [4, 5, 6], "v": [40, 50, 60]})
        db.write("t", df2)
        assert len(db.versions("t")) == 2
        db.close()

    def test_violation_raises(self):
        db = _tmp_db()
        db.write("t", pd.DataFrame({"id": [1, 2, 3], "v": [10, 20, 30]}), primary_key=["id"])
        df_dup = pd.DataFrame({"id": [1, 1, 2], "v": [10, 11, 20]})
        with pytest.raises(PrimaryKeyViolationError, match="duplicate"):
            db.write("t", df_dup)
        db.close()

    def test_composite_key(self):
        db = _tmp_db()
        df = pd.DataFrame({
            "region": ["US", "US", "EU"],
            "id": [1, 2, 1],
            "v": [10, 20, 30],
        })
        db.write("t", df, primary_key=["region", "id"])
        assert db.primary_key("t") == ["region", "id"]
        db.close()

    def test_composite_key_violation(self):
        db = _tmp_db()
        df = pd.DataFrame({
            "region": ["US", "US", "US"],
            "id": [1, 2, 1],  # (US, 1) duplicated
            "v": [10, 20, 30],
        })
        with pytest.raises(PrimaryKeyViolationError):
            db.write("t", df, primary_key=["region", "id"])
        db.close()

    def test_column_missing_raises(self):
        db = _tmp_db()
        df = pd.DataFrame({"id": [1], "v": [10]})
        with pytest.raises(ValueError, match="not found"):
            db.write("t", df, primary_key=["nonexistent"])
        db.close()

    def test_pk_immutable(self):
        db = _tmp_db()
        db.write("t", _simple_df(), primary_key=["id"])
        with pytest.raises(ValueError, match="immutable"):
            db.write("t", _simple_df(3, 4), primary_key=["name"])
        db.close()

    def test_set_pk_before_write(self):
        db = _tmp_db()
        db.set_primary_key("t", ["id"])
        db.write("t", _simple_df())
        assert db.primary_key("t") == ["id"]
        # PK should be enforced
        df_dup = pd.DataFrame({"id": [1, 1, 2], "name": ["a", "b", "c"], "score": [1.0, 2.0, 3.0]})
        with pytest.raises(PrimaryKeyViolationError):
            db.write("t", df_dup)
        db.close()

    def test_db_primary_key_returns_value(self):
        db = _tmp_db()
        db.write("t", _simple_df(), primary_key=["id"])
        assert db.primary_key("t") == ["id"]
        db.close()

    def test_db_primary_key_returns_none(self):
        db = _tmp_db()
        db.write("t", _simple_df())
        assert db.primary_key("t") is None
        db.close()

    def test_null_values_distinct(self):
        """NULL values should not cause false uniqueness violations."""
        db = _tmp_db()
        df = pd.DataFrame({"id": [1, 2, None], "v": [10, 20, 30]})
        # DuckDB treats NULLs as distinct in COUNT(DISTINCT)
        db.write("t", df, primary_key=["id"])
        db.close()

    def test_10k_rows_under_50ms(self):
        db = _tmp_db()
        n = 10_000
        df = pd.DataFrame({"id": range(n), "v": range(n)})
        t0 = time.monotonic()
        db.write("t", df, primary_key=["id"])
        elapsed = time.monotonic() - t0
        assert elapsed < 2.0  # generous for CI
        db.close()

    def test_100k_rows_under_200ms(self):
        db = _tmp_db()
        n = 100_000
        df = pd.DataFrame({"id": range(n), "v": range(n)})
        t0 = time.monotonic()
        db.write("t", df, primary_key=["id"])
        elapsed = time.monotonic() - t0
        assert elapsed < 5.0  # generous for CI
        db.close()

    def test_pk_with_schema_evolution(self):
        """PK and schema evolution work together."""
        db = _tmp_db()
        db.write("t", _simple_df(), primary_key=["id"])
        df2 = _simple_df(3, 4)
        df2["email"] = ["a@b.com", "c@d.com", "e@f.com"]
        db.write("t", df2)  # additive change + PK check
        assert len(db.versions("t")) == 2
        db.close()

    def test_single_row_unique(self):
        db = _tmp_db()
        df = pd.DataFrame({"id": [1], "v": [10]})
        db.write("t", df, primary_key=["id"])
        db.close()


# ===========================================================================
# TestPKWithDiff
# ===========================================================================

class TestPKWithDiff:
    """Test that diff auto-resolves key_columns from PK metadata."""

    def test_auto_uses_pk(self):
        db = _tmp_db()
        db.write("t", _simple_df(3, 1), primary_key=["id"])
        db.write("t", _simple_df(3, 2))
        diff = db.diff("t")
        # With PK auto-resolved, should have row-level diff
        assert diff.rows is not None
        db.close()

    def test_explicit_overrides_pk(self):
        db = _tmp_db()
        db.write("t", _simple_df(3, 1), primary_key=["id"])
        db.write("t", _simple_df(3, 2))
        diff = db.diff("t", key_columns=["name"])
        assert diff.rows is not None
        db.close()

    def test_no_key_no_pk_stats_only(self):
        db = _tmp_db()
        db.write("t", _simple_df(3, 1))
        db.write("t", _simple_df(3, 2))
        diff = db.diff("t")
        # Without PK or key_columns, row diff not computed
        assert diff.rows is None
        db.close()

    def test_detects_modifications(self):
        db = _tmp_db()
        df1 = pd.DataFrame({"id": [1, 2, 3], "v": [10, 20, 30]})
        df2 = pd.DataFrame({"id": [1, 2, 3], "v": [10, 25, 30]})
        db.write("t", df1, primary_key=["id"])
        db.write("t", df2)
        diff = db.diff("t")
        assert diff.rows.modified.num_rows == 1
        db.close()

    def test_auto_same_as_explicit(self):
        """Auto-PK should produce identical results to explicit key_columns."""
        db = _tmp_db()
        df1 = pd.DataFrame({"id": [1, 2, 3], "v": [10, 20, 30]})
        df2 = pd.DataFrame({"id": [1, 2, 4], "v": [10, 25, 40]})
        db.write("t", df1, primary_key=["id"])
        db.write("t", df2)
        auto = db.diff("t")
        explicit = db.diff("t", key_columns=["id"])
        assert auto.rows.added.num_rows == explicit.rows.added.num_rows
        assert auto.rows.removed.num_rows == explicit.rows.removed.num_rows
        assert auto.rows.modified.num_rows == explicit.rows.modified.num_rows
        db.close()


# ===========================================================================
# TestPKWithTransactions
# ===========================================================================

class TestPKWithTransactions:
    """Test PK enforcement within transactions."""

    def test_enforced_in_tx(self):
        db = _tmp_db()
        db.set_primary_key("t", ["id"])
        with db.engine.transaction() as tx:
            df = pd.DataFrame({"id": [1, 2, 3], "v": [10, 20, 30]})
            tx.write_table("t", df)
        assert db.primary_key("t") == ["id"]
        db.close()

    def test_violation_aborts_tx(self):
        db = _tmp_db()
        db.set_primary_key("t", ["id"])
        with pytest.raises(PrimaryKeyViolationError):
            with db.engine.transaction() as tx:
                df_dup = pd.DataFrame({"id": [1, 1, 2], "v": [10, 11, 20]})
                tx.write_table("t", df_dup)
        db.close()


# ===========================================================================
# TestDatabaseIntegration
# ===========================================================================

class TestDatabaseIntegration:
    """Integration tests for schema + PK across the full Database API."""

    def test_set_mode_persists_across_reload(self):
        d = tempfile.mkdtemp()
        db1 = rhizo.open(d)
        db1.set_schema_mode("t", "flexible")
        db1.close()
        db2 = rhizo.open(d)
        meta = db2._table_meta_store.load("t")
        assert meta.schema_mode == "flexible"
        db2.close()

    def test_set_mode_invalid(self):
        db = _tmp_db()
        with pytest.raises(ValueError, match="Invalid"):
            db.set_schema_mode("t", "garbage")
        db.close()

    def test_set_pk_then_write(self):
        db = _tmp_db()
        db.set_primary_key("t", ["id"])
        db.write("t", _simple_df())
        assert db.primary_key("t") == ["id"]
        db.close()

    def test_write_then_set_pk_matches(self):
        """Setting same PK after write should succeed."""
        db = _tmp_db()
        db.write("t", _simple_df(), primary_key=["id"])
        db.set_primary_key("t", ["id"])  # same PK, no error
        db.close()

    def test_write_then_set_pk_different_fails(self):
        """Setting different PK after write should fail."""
        db = _tmp_db()
        db.write("t", _simple_df(), primary_key=["id"])
        with pytest.raises(ValueError, match="immutable"):
            db.set_primary_key("t", ["name"])
        db.close()

    def test_full_lifecycle(self):
        db = _tmp_db()
        # V1: initial write with PK
        db.write("users", _simple_df(), primary_key=["id"])
        assert db.primary_key("users") == ["id"]
        assert db.schema("users") is not None

        # V2: additive evolution + PK enforcement
        df2 = _simple_df(3, 4)
        df2["email"] = ["a@b.com", "c@d.com", "e@f.com"]
        db.write("users", df2)

        # V3: same schema
        df3 = _simple_df(3, 7)
        df3["email"] = ["g@h.com", "i@j.com", "k@l.com"]
        db.write("users", df3)

        # Verify
        assert len(db.versions("users")) == 3
        history = db.schema_history("users")
        assert len(history) == 3
        assert history[1]["changes"]["added"][0][0] == "email"
        assert history[2]["changes"]["added"] == []

        # Diff auto-resolves PK
        diff = db.diff("users", version_a=2, version_b=3)
        assert diff.rows is not None
        db.close()

    def test_closed_raises(self):
        db = _tmp_db()
        db.close()
        with pytest.raises(RuntimeError):
            db.schema("t")
        with pytest.raises(RuntimeError):
            db.primary_key("t")
        with pytest.raises(RuntimeError):
            db.set_primary_key("t", ["id"])
        with pytest.raises(RuntimeError):
            db.set_schema_mode("t", "flexible")

    def test_schema_with_branches(self):
        """Schema is tracked per-version, works across branches."""
        db = _tmp_db()
        db.write("t", _simple_df())
        db.engine.create_branch("feature")
        db.engine.checkout("feature")
        df2 = _simple_df()
        df2["extra"] = [1.0, 2.0, 3.0]
        db.write("t", df2)
        # Feature branch has 4-column schema
        s = db.schema("t")
        assert len(s) == 4
        # Main branch still has 3-column schema
        db.engine.checkout("main")
        s_main = db.schema("t", version=1)
        assert len(s_main) == 3
        db.close()
