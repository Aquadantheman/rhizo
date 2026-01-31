"""
Tests for rhizo.metrics module.

Covers AlgebraicSignature, CommitMetrics, OperationClassifier,
MetricsCollector, and InstrumentedWriter.
"""

import csv
import json
import tempfile
import shutil
from pathlib import Path
from unittest.mock import MagicMock

import pyarrow as pa
import pytest

from rhizo.metrics import (
    AlgebraicSignature,
    CommitMetrics,
    OperationClassifier,
    MetricsCollector,
    InstrumentedWriter,
)


# ---------------------------------------------------------------------------
# AlgebraicSignature
# ---------------------------------------------------------------------------

class TestAlgebraicSignature:
    """Test AlgebraicSignature enum properties."""

    def test_semilattice_is_coordination_free(self):
        assert AlgebraicSignature.SEMILATTICE.coordination_free is True

    def test_abelian_is_coordination_free(self):
        assert AlgebraicSignature.ABELIAN.coordination_free is True

    def test_generic_is_not_coordination_free(self):
        assert AlgebraicSignature.GENERIC.coordination_free is False

    def test_semilattice_min_rounds(self):
        assert AlgebraicSignature.SEMILATTICE.theoretical_min_rounds == 0

    def test_abelian_min_rounds(self):
        assert AlgebraicSignature.ABELIAN.theoretical_min_rounds == 0

    def test_generic_min_rounds(self):
        assert AlgebraicSignature.GENERIC.theoretical_min_rounds == -1

    def test_semilattice_bound_string(self):
        assert AlgebraicSignature.SEMILATTICE.theoretical_bound == "0"

    def test_abelian_bound_string(self):
        assert AlgebraicSignature.ABELIAN.theoretical_bound == "0"

    def test_generic_bound_string(self):
        assert AlgebraicSignature.GENERIC.theoretical_bound == "Omega(log N)"

    def test_enum_values(self):
        assert AlgebraicSignature.SEMILATTICE.value == "semilattice"
        assert AlgebraicSignature.ABELIAN.value == "abelian"
        assert AlgebraicSignature.GENERIC.value == "generic"


# ---------------------------------------------------------------------------
# CommitMetrics
# ---------------------------------------------------------------------------

class TestCommitMetrics:
    """Test CommitMetrics dataclass and computed properties."""

    def _make_metric(self, **overrides):
        defaults = dict(
            table_name="test_table",
            operation_id="op_00000001",
            timestamp_ns=1_000_000_000,
            algebraic_signature=AlgebraicSignature.ABELIAN,
            operation_type="increment",
            issue_time_ns=100_000_000,
            commit_time_ns=101_000_000,
            coordination_rounds=0,
        )
        defaults.update(overrides)
        return CommitMetrics(**defaults)

    def test_commit_latency_ns(self):
        m = self._make_metric(issue_time_ns=100, commit_time_ns=350)
        assert m.commit_latency_ns == 250

    def test_commit_latency_ms(self):
        m = self._make_metric(issue_time_ns=0, commit_time_ns=2_000_000)
        assert m.commit_latency_ms == 2.0

    def test_matches_theory_algebraic_zero_rounds(self):
        m = self._make_metric(
            algebraic_signature=AlgebraicSignature.ABELIAN,
            coordination_rounds=0,
        )
        assert m.matches_theory is True

    def test_matches_theory_algebraic_nonzero_rounds_fails(self):
        m = self._make_metric(
            algebraic_signature=AlgebraicSignature.SEMILATTICE,
            coordination_rounds=1,
        )
        assert m.matches_theory is False

    def test_matches_theory_generic_with_rounds(self):
        m = self._make_metric(
            algebraic_signature=AlgebraicSignature.GENERIC,
            coordination_rounds=2,
        )
        assert m.matches_theory is True

    def test_matches_theory_generic_zero_rounds_fails(self):
        m = self._make_metric(
            algebraic_signature=AlgebraicSignature.GENERIC,
            coordination_rounds=0,
        )
        assert m.matches_theory is False

    def test_to_dict_keys(self):
        m = self._make_metric()
        d = m.to_dict()
        expected_keys = {
            "table_name", "operation_id", "timestamp_ns",
            "algebraic_signature", "operation_type",
            "issue_time_ns", "commit_time_ns", "propagation_time_ns",
            "commit_latency_ns", "commit_latency_ms",
            "coordination_rounds", "messages_sent", "messages_received",
            "bytes_sent", "bytes_received",
            "num_rows", "num_columns", "data_bytes",
            "matches_theory",
        }
        assert set(d.keys()) == expected_keys

    def test_to_dict_values(self):
        m = self._make_metric(
            table_name="orders",
            algebraic_signature=AlgebraicSignature.GENERIC,
            coordination_rounds=3,
        )
        d = m.to_dict()
        assert d["table_name"] == "orders"
        assert d["algebraic_signature"] == "generic"
        assert d["coordination_rounds"] == 3

    def test_propagation_time_default_none(self):
        m = self._make_metric()
        assert m.propagation_time_ns is None
        assert m.to_dict()["propagation_time_ns"] is None


# ---------------------------------------------------------------------------
# OperationClassifier
# ---------------------------------------------------------------------------

class TestOperationClassifier:
    """Test OperationClassifier classification logic."""

    # --- classify() ---

    @pytest.mark.parametrize("op,expected", [
        ("add", AlgebraicSignature.ABELIAN),
        ("increment", AlgebraicSignature.ABELIAN),
        ("decrement", AlgebraicSignature.ABELIAN),
        ("sum", AlgebraicSignature.ABELIAN),
        ("multiply", AlgebraicSignature.ABELIAN),
    ])
    def test_classify_abelian(self, op, expected):
        assert OperationClassifier.classify(op) == expected

    @pytest.mark.parametrize("op,expected", [
        ("max", AlgebraicSignature.SEMILATTICE),
        ("min", AlgebraicSignature.SEMILATTICE),
        ("union", AlgebraicSignature.SEMILATTICE),
        ("or", AlgebraicSignature.SEMILATTICE),
        ("and", AlgebraicSignature.SEMILATTICE),
        ("greatest", AlgebraicSignature.SEMILATTICE),
        ("least", AlgebraicSignature.SEMILATTICE),
    ])
    def test_classify_semilattice(self, op, expected):
        assert OperationClassifier.classify(op) == expected

    @pytest.mark.parametrize("op,expected", [
        ("overwrite", AlgebraicSignature.GENERIC),
        ("set", AlgebraicSignature.GENERIC),
        ("replace", AlgebraicSignature.GENERIC),
        ("cas", AlgebraicSignature.GENERIC),
        ("compare_and_swap", AlgebraicSignature.GENERIC),
        ("delete", AlgebraicSignature.GENERIC),
        ("insert_unique", AlgebraicSignature.GENERIC),
    ])
    def test_classify_generic(self, op, expected):
        assert OperationClassifier.classify(op) == expected

    def test_classify_unknown_defaults_to_generic(self):
        assert OperationClassifier.classify("unknown_op") == AlgebraicSignature.GENERIC

    def test_classify_case_insensitive(self):
        assert OperationClassifier.classify("MAX") == AlgebraicSignature.SEMILATTICE
        assert OperationClassifier.classify("Add") == AlgebraicSignature.ABELIAN

    def test_classify_strips_whitespace(self):
        assert OperationClassifier.classify("  max  ") == AlgebraicSignature.SEMILATTICE

    # --- classify_write() ---

    def test_classify_write_no_merge_columns(self):
        table = pa.table({"x": [1, 2]})
        assert OperationClassifier.classify_write(table) == AlgebraicSignature.GENERIC

    def test_classify_write_all_abelian(self):
        table = pa.table({"count": [1], "total": [10]})
        merge = {"count": "sum", "total": "add"}
        assert OperationClassifier.classify_write(table, merge) == AlgebraicSignature.ABELIAN

    def test_classify_write_all_semilattice(self):
        table = pa.table({"high": [1], "low": [0]})
        merge = {"high": "max", "low": "min"}
        assert OperationClassifier.classify_write(table, merge) == AlgebraicSignature.SEMILATTICE

    def test_classify_write_mixed_algebraic_returns_abelian(self):
        """When mixing abelian + semilattice, abelian (weaker) wins."""
        table = pa.table({"count": [1], "high": [10]})
        merge = {"count": "sum", "high": "max"}
        assert OperationClassifier.classify_write(table, merge) == AlgebraicSignature.ABELIAN

    def test_classify_write_missing_column_generic(self):
        """If a column has no merge strategy, the whole write is generic."""
        table = pa.table({"count": [1], "name": ["alice"]})
        merge = {"count": "sum"}  # name not covered
        assert OperationClassifier.classify_write(table, merge) == AlgebraicSignature.GENERIC

    def test_classify_write_any_generic_strategy_makes_all_generic(self):
        table = pa.table({"count": [1], "id": [10]})
        merge = {"count": "sum", "id": "overwrite"}
        assert OperationClassifier.classify_write(table, merge) == AlgebraicSignature.GENERIC

    # --- classify_transaction() ---

    def test_classify_transaction_empty(self):
        assert OperationClassifier.classify_transaction([]) == AlgebraicSignature.SEMILATTICE

    def test_classify_transaction_all_semilattice(self):
        ops = [AlgebraicSignature.SEMILATTICE, AlgebraicSignature.SEMILATTICE]
        assert OperationClassifier.classify_transaction(ops) == AlgebraicSignature.SEMILATTICE

    def test_classify_transaction_all_abelian(self):
        ops = [AlgebraicSignature.ABELIAN, AlgebraicSignature.ABELIAN]
        assert OperationClassifier.classify_transaction(ops) == AlgebraicSignature.ABELIAN

    def test_classify_transaction_mixed_algebraic(self):
        ops = [AlgebraicSignature.SEMILATTICE, AlgebraicSignature.ABELIAN]
        assert OperationClassifier.classify_transaction(ops) == AlgebraicSignature.ABELIAN

    def test_classify_transaction_any_generic_poisons(self):
        ops = [AlgebraicSignature.ABELIAN, AlgebraicSignature.GENERIC]
        assert OperationClassifier.classify_transaction(ops) == AlgebraicSignature.GENERIC

    def test_classify_transaction_single_generic(self):
        assert OperationClassifier.classify_transaction(
            [AlgebraicSignature.GENERIC]
        ) == AlgebraicSignature.GENERIC


# ---------------------------------------------------------------------------
# MetricsCollector
# ---------------------------------------------------------------------------

class TestMetricsCollector:
    """Test MetricsCollector recording, aggregation, and export."""

    def _metric(self, sig=AlgebraicSignature.ABELIAN, rounds=0,
                issue=100, commit=200, table="t"):
        return CommitMetrics(
            table_name=table,
            operation_id="op_1",
            timestamp_ns=0,
            algebraic_signature=sig,
            operation_type="test",
            issue_time_ns=issue,
            commit_time_ns=commit,
            coordination_rounds=rounds,
        )

    def test_record_and_count(self):
        c = MetricsCollector()
        c.record(self._metric())
        c.record(self._metric())
        assert len(c.metrics) == 2

    def test_generate_operation_id_sequential(self):
        c = MetricsCollector()
        assert c.generate_operation_id() == "op_00000001"
        assert c.generate_operation_id() == "op_00000002"

    def test_clear(self):
        c = MetricsCollector()
        c.record(self._metric())
        c.generate_operation_id()
        c.clear()
        assert len(c.metrics) == 0
        assert c.generate_operation_id() == "op_00000001"

    def test_by_signature_groups(self):
        c = MetricsCollector()
        c.record(self._metric(sig=AlgebraicSignature.ABELIAN))
        c.record(self._metric(sig=AlgebraicSignature.SEMILATTICE))
        c.record(self._metric(sig=AlgebraicSignature.GENERIC, rounds=1))
        grouped = c.by_signature()
        assert len(grouped[AlgebraicSignature.ABELIAN]) == 1
        assert len(grouped[AlgebraicSignature.SEMILATTICE]) == 1
        assert len(grouped[AlgebraicSignature.GENERIC]) == 1

    def test_summary_empty(self):
        c = MetricsCollector()
        assert c.summary() == {"total_operations": 0}

    def test_summary_with_metrics(self):
        c = MetricsCollector()
        c.record(self._metric(sig=AlgebraicSignature.ABELIAN, issue=0, commit=1_000_000))
        c.record(self._metric(sig=AlgebraicSignature.GENERIC, rounds=1, issue=0, commit=5_000_000))
        s = c.summary()
        assert s["total_operations"] == 2
        assert s["abelian"]["count"] == 1
        assert s["generic"]["count"] == 1
        assert s["algebraic_total"]["count"] == 1
        assert "speedup_ratio" in s

    def test_summary_validation_all_match(self):
        c = MetricsCollector()
        c.record(self._metric(sig=AlgebraicSignature.ABELIAN, rounds=0))
        s = c.summary()
        assert s["validation"]["all_match_theory"] is True
        assert s["validation"]["algebraic_coordination_free"] is True

    def test_summary_validation_mismatch(self):
        c = MetricsCollector()
        # Algebraic op with coordination rounds — violates theory
        c.record(self._metric(sig=AlgebraicSignature.ABELIAN, rounds=1))
        s = c.summary()
        assert s["validation"]["all_match_theory"] is False
        assert s["validation"]["algebraic_coordination_free"] is False

    def test_summary_latency_stats(self):
        c = MetricsCollector()
        c.record(self._metric(issue=0, commit=2_000_000))  # 2ms
        c.record(self._metric(issue=0, commit=4_000_000))  # 4ms
        s = c.summary()
        assert s["abelian"]["mean_latency_ms"] == pytest.approx(3.0)
        assert s["abelian"]["min_latency_ms"] == pytest.approx(2.0)
        assert s["abelian"]["max_latency_ms"] == pytest.approx(4.0)

    def test_export_json(self, tmp_path):
        c = MetricsCollector()
        c.record(self._metric())
        out = tmp_path / "metrics.json"
        c.export_json(out)
        data = json.loads(out.read_text())
        assert "metrics" in data
        assert "summary" in data
        assert len(data["metrics"]) == 1

    def test_export_csv(self, tmp_path):
        c = MetricsCollector()
        c.record(self._metric())
        c.record(self._metric())
        out = tmp_path / "metrics.csv"
        c.export_csv(out)
        with open(out) as f:
            reader = csv.DictReader(f)
            rows = list(reader)
        assert len(rows) == 2
        assert "table_name" in rows[0]

    def test_export_csv_empty(self, tmp_path):
        c = MetricsCollector()
        out = tmp_path / "metrics.csv"
        c.export_csv(out)
        assert not out.exists()  # empty collector writes nothing


# ---------------------------------------------------------------------------
# InstrumentedWriter
# ---------------------------------------------------------------------------

class TestInstrumentedWriter:
    """Test InstrumentedWriter wrapping and metric recording."""

    def _mock_writer(self):
        writer = MagicMock()
        result = MagicMock()
        result.total_rows = 5
        result.total_bytes = 200
        writer.write.return_value = result
        return writer

    def test_write_records_metric(self):
        w = self._mock_writer()
        iw = InstrumentedWriter(w)
        table = pa.table({"x": [1, 2, 3]})
        iw.write("t1", table)
        assert len(iw.collector.metrics) == 1
        m = iw.collector.metrics[0]
        assert m.table_name == "t1"
        assert m.algebraic_signature == AlgebraicSignature.GENERIC  # no merge columns

    def test_write_with_merge_columns(self):
        w = self._mock_writer()
        iw = InstrumentedWriter(w)
        table = pa.table({"count": [1]})
        iw.write("t1", table, merge_columns={"count": "sum"})
        m = iw.collector.metrics[0]
        assert m.algebraic_signature == AlgebraicSignature.ABELIAN

    def test_write_passes_through_to_underlying(self):
        w = self._mock_writer()
        iw = InstrumentedWriter(w)
        table = pa.table({"x": [1]})
        result = iw.write("t1", table, metadata={"k": "v"})
        w.write.assert_called_once_with("t1", table, {"k": "v"})
        assert result.total_rows == 5

    def test_increment_records_abelian_metric(self):
        w = self._mock_writer()
        iw = InstrumentedWriter(w)
        iw.increment("counters", "count", delta=1)
        m = iw.collector.metrics[0]
        assert m.algebraic_signature == AlgebraicSignature.ABELIAN
        assert m.operation_type == "increment"
        assert m.coordination_rounds == 0

    def test_max_update_records_semilattice_metric(self):
        w = self._mock_writer()
        iw = InstrumentedWriter(w)
        iw.max_update("peaks", "high", value=100)
        m = iw.collector.metrics[0]
        assert m.algebraic_signature == AlgebraicSignature.SEMILATTICE
        assert m.operation_type == "max"
        assert m.coordination_rounds == 0

    def test_collector_property(self):
        collector = MetricsCollector()
        iw = InstrumentedWriter(self._mock_writer(), collector)
        assert iw.collector is collector

    def test_default_collector_created(self):
        iw = InstrumentedWriter(self._mock_writer())
        assert isinstance(iw.collector, MetricsCollector)

    def test_operation_ids_sequential_across_methods(self):
        w = self._mock_writer()
        iw = InstrumentedWriter(w)
        iw.increment("t", "c")
        iw.max_update("t", "c", value=1)
        table = pa.table({"x": [1]})
        iw.write("t", table)
        ids = [m.operation_id for m in iw.collector.metrics]
        assert ids == ["op_00000001", "op_00000002", "op_00000003"]

    def test_write_latency_is_non_negative(self):
        w = self._mock_writer()
        iw = InstrumentedWriter(w)
        table = pa.table({"x": [1]})
        iw.write("t", table)
        m = iw.collector.metrics[0]
        assert m.commit_latency_ns >= 0
