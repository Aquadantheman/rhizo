"""
Tests for the Database high-level interface.

Covers resource lifecycle, __del__ finalizer, context manager,
and basic operations through the Database API.
"""

import gc
import warnings
import tempfile
import shutil

import pandas as pd
import pytest

import rhizo


@pytest.fixture
def temp_dir():
    """Create a temporary directory for database tests."""
    d = tempfile.mkdtemp(prefix="rhizo_test_db_")
    yield d
    shutil.rmtree(d, ignore_errors=True)


class TestDatabaseLifecycle:
    """Test Database resource lifecycle and cleanup."""

    def test_explicit_close(self, temp_dir):
        """Database.close() sets _closed flag and prevents further operations."""
        db = rhizo.open(temp_dir)
        assert not db._closed
        db.close()
        assert db._closed

    def test_double_close_is_safe(self, temp_dir):
        """Calling close() twice does not raise."""
        db = rhizo.open(temp_dir)
        db.close()
        db.close()  # Should not raise
        assert db._closed

    def test_context_manager_closes(self, temp_dir):
        """Context manager calls close() on exit."""
        with rhizo.open(temp_dir) as db:
            assert not db._closed
        assert db._closed

    def test_context_manager_closes_on_exception(self, temp_dir):
        """Context manager calls close() even when an exception occurs."""
        with pytest.raises(ValueError):
            with rhizo.open(temp_dir) as db:
                raise ValueError("test error")
        assert db._closed

    def test_operations_after_close_raise(self, temp_dir):
        """All operations raise RuntimeError after close."""
        db = rhizo.open(temp_dir)
        df = pd.DataFrame({"id": [1], "name": ["Alice"]})
        db.write("users", df)
        db.close()

        with pytest.raises(RuntimeError, match="closed"):
            db.sql("SELECT * FROM users")

        with pytest.raises(RuntimeError, match="closed"):
            db.write("users", df)

        with pytest.raises(RuntimeError, match="closed"):
            db.read("users")

        with pytest.raises(RuntimeError, match="closed"):
            db.tables()

        with pytest.raises(RuntimeError, match="closed"):
            _ = db.engine


class TestDelFinalizer:
    """Test __del__ finalizer for resource leak prevention."""

    def test_del_warns_if_not_closed(self, temp_dir):
        """__del__ emits ResourceWarning if database was not explicitly closed."""
        db = rhizo.open(temp_dir)
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            db.__del__()
            assert len(w) == 1
            assert issubclass(w[0].category, ResourceWarning)
            assert "not closed explicitly" in str(w[0].message)
        # __del__ should have closed it
        assert db._closed

    def test_del_silent_if_already_closed(self, temp_dir):
        """__del__ does not warn if database was already closed."""
        db = rhizo.open(temp_dir)
        db.close()
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            db.__del__()
            resource_warnings = [x for x in w if issubclass(x.category, ResourceWarning)]
            assert len(resource_warnings) == 0

    def test_del_silent_after_context_manager(self, temp_dir):
        """__del__ does not warn after context manager exit."""
        with rhizo.open(temp_dir) as db:
            pass
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            db.__del__()
            resource_warnings = [x for x in w if issubclass(x.category, ResourceWarning)]
            assert len(resource_warnings) == 0

    def test_gc_triggers_cleanup(self, temp_dir):
        """Garbage collection triggers __del__ and closes the database."""
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            db = rhizo.open(temp_dir)
            # Drop the only reference and force GC
            del db
            gc.collect()
            # We should see a ResourceWarning
            resource_warnings = [x for x in w if issubclass(x.category, ResourceWarning)]
            assert len(resource_warnings) == 1
            assert "not closed explicitly" in str(resource_warnings[0].message)

    def test_del_handles_partial_init(self, temp_dir):
        """__del__ does not crash if object was only partially initialized."""
        db = object.__new__(rhizo.Database)
        # _closed attribute may not exist if __init__ failed midway
        # __del__ should handle this gracefully
        try:
            db.__del__()
        except Exception:
            pytest.fail("__del__ raised on partially initialized Database")


class TestDatabaseBasicOperations:
    """Test basic Database operations to ensure __del__ changes don't break anything."""

    def test_write_and_read(self, temp_dir):
        """Write and read round-trips correctly through Database API."""
        with rhizo.open(temp_dir) as db:
            df = pd.DataFrame({"id": [1, 2, 3], "score": [85.5, 92.0, 78.5]})
            db.write("scores", df)
            result = db.read("scores")
            assert result.num_rows == 3

    def test_sql_query(self, temp_dir):
        """SQL query works through Database API."""
        with rhizo.open(temp_dir) as db:
            df = pd.DataFrame({"id": [1, 2, 3], "val": [10, 20, 30]})
            db.write("data", df)
            result = db.sql("SELECT * FROM data WHERE val > 15")
            assert result.to_pandas().shape[0] == 2

    def test_tables_list(self, temp_dir):
        """tables() returns written table names."""
        with rhizo.open(temp_dir) as db:
            df = pd.DataFrame({"x": [1]})
            db.write("alpha", df)
            db.write("beta", df)
            tables = db.tables()
            assert "alpha" in tables
            assert "beta" in tables

    def test_repr(self, temp_dir):
        """repr shows open/closed status."""
        db = rhizo.open(temp_dir)
        assert "open" in repr(db)
        db.close()
        assert "closed" in repr(db)
