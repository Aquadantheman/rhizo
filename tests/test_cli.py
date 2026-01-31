"""
Tests for rhizo.cli module.

Covers all CLI commands (info, tables, versions, verify) and the main() parser.
"""

import tempfile
import shutil

import pandas as pd
import pytest

import rhizo
from rhizo.cli import main, cmd_info, cmd_tables, cmd_versions, cmd_verify


@pytest.fixture
def empty_db(tmp_path):
    """Create an empty Rhizo database."""
    path = str(tmp_path / "empty_db")
    with rhizo.open(path) as db:
        pass  # just create it
    return path


@pytest.fixture
def populated_db(tmp_path):
    """Create a Rhizo database with tables."""
    path = str(tmp_path / "pop_db")
    with rhizo.open(path) as db:
        df1 = pd.DataFrame({"id": [1, 2], "name": ["a", "b"]})
        db.write("users", df1)
        df2 = pd.DataFrame({"id": [10], "value": [99.9]})
        db.write("orders", df2)
        # Second version of users
        df3 = pd.DataFrame({"id": [3], "name": ["c"]})
        db.write("users", df3)
    return path


# ---------------------------------------------------------------------------
# main() parser
# ---------------------------------------------------------------------------

class TestMainParser:
    """Test the argparse-based main() entry point."""

    def test_no_command_returns_zero(self, capsys):
        """No subcommand prints help and returns 0."""
        ret = main([])
        assert ret == 0

    def test_version_flag(self, capsys):
        with pytest.raises(SystemExit) as exc_info:
            main(["--version"])
        assert exc_info.value.code == 0

    def test_nonexistent_path_returns_one(self, capsys):
        ret = main(["info", "/nonexistent/path/xyz"])
        assert ret == 1
        captured = capsys.readouterr()
        assert "Error" in captured.err


# ---------------------------------------------------------------------------
# cmd_info
# ---------------------------------------------------------------------------

class TestCmdInfo:
    """Test the 'info' command."""

    def test_info_empty_db(self, empty_db, capsys):
        ret = main(["info", empty_db])
        assert ret == 0
        out = capsys.readouterr().out
        assert "Tables: 0" in out

    def test_info_populated_db(self, populated_db, capsys):
        ret = main(["info", populated_db])
        assert ret == 0
        out = capsys.readouterr().out
        assert "Tables: 2" in out
        assert "users" in out
        assert "orders" in out

    def test_info_nonexistent_path(self, capsys):
        ret = main(["info", "/no/such/path"])
        assert ret == 1
        assert "Error" in capsys.readouterr().err

    def test_info_shows_version_count(self, populated_db, capsys):
        main(["info", populated_db])
        out = capsys.readouterr().out
        assert "2 version(s)" in out  # users has 2 versions


# ---------------------------------------------------------------------------
# cmd_tables
# ---------------------------------------------------------------------------

class TestCmdTables:
    """Test the 'tables' command."""

    def test_tables_empty(self, empty_db, capsys):
        ret = main(["tables", empty_db])
        assert ret == 0
        out = capsys.readouterr().out
        assert out.strip() == ""

    def test_tables_populated(self, populated_db, capsys):
        ret = main(["tables", populated_db])
        assert ret == 0
        lines = capsys.readouterr().out.strip().splitlines()
        assert sorted(lines) == ["orders", "users"]

    def test_tables_nonexistent_path(self, capsys):
        ret = main(["tables", "/no/such/path"])
        assert ret == 1


# ---------------------------------------------------------------------------
# cmd_versions
# ---------------------------------------------------------------------------

class TestCmdVersions:
    """Test the 'versions' command."""

    def test_versions_existing_table(self, populated_db, capsys):
        ret = main(["versions", populated_db, "users"])
        assert ret == 0
        out = capsys.readouterr().out
        assert "Versions: 2" in out
        assert "v1" in out
        assert "v2" in out

    def test_versions_single_version_table(self, populated_db, capsys):
        ret = main(["versions", populated_db, "orders"])
        assert ret == 0
        out = capsys.readouterr().out
        assert "Versions: 1" in out

    def test_versions_nonexistent_table(self, populated_db, capsys):
        ret = main(["versions", populated_db, "no_such_table"])
        assert ret == 1
        err = capsys.readouterr().err
        assert "not found" in err
        assert "Available tables" in err

    def test_versions_nonexistent_path(self, capsys):
        ret = main(["versions", "/no/such/path", "t"])
        assert ret == 1


# ---------------------------------------------------------------------------
# cmd_verify
# ---------------------------------------------------------------------------

class TestCmdVerify:
    """Test the 'verify' command."""

    def test_verify_empty_db(self, empty_db, capsys):
        ret = main(["verify", empty_db])
        assert ret == 0
        out = capsys.readouterr().out
        assert "empty" in out.lower() or "no tables" in out.lower()

    def test_verify_populated_db(self, populated_db, capsys):
        ret = main(["verify", populated_db])
        assert ret == 0
        out = capsys.readouterr().out
        assert "OK" in out
        assert "passed" in out.lower()

    def test_verify_nonexistent_path(self, capsys):
        ret = main(["verify", "/no/such/path"])
        assert ret == 1
        assert "Error" in capsys.readouterr().err

    def test_verify_reports_table_count(self, populated_db, capsys):
        main(["verify", populated_db])
        out = capsys.readouterr().out
        assert "2 table(s)" in out
