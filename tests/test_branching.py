"""
Tests for Armillaria Branching functionality.

Run with: pytest tests/test_branching.py -v
"""

import os
import tempfile
import shutil

import pytest

import armillaria


@pytest.fixture
def temp_storage():
    """Create temporary storage directories for testing."""
    base_dir = tempfile.mkdtemp(prefix="armillaria_branch_test_")
    branches_dir = os.path.join(base_dir, "branches")

    manager = armillaria.PyBranchManager(branches_dir)

    yield manager, base_dir

    # Cleanup
    shutil.rmtree(base_dir, ignore_errors=True)


class TestBranchManager:
    """Tests for PyBranchManager."""

    def test_default_main_branch(self, temp_storage):
        """Test that main branch is created by default."""
        manager, _ = temp_storage

        branches = manager.list()
        assert "main" in branches

        main = manager.get("main")
        assert main.name == "main"
        assert main.head == {}  # Empty initially
        assert main.parent_branch is None

    def test_create_branch(self, temp_storage):
        """Test creating a new branch."""
        manager, _ = temp_storage

        # Add a table to main first
        manager.update_head("main", "users", 1)

        # Create feature branch
        feature = manager.create(
            "feature/test",
            from_branch="main",
            description="Test feature branch"
        )

        assert feature.name == "feature/test"
        assert feature.head == {"users": 1}  # Copied from main
        assert feature.parent_branch == "main"
        assert feature.description == "Test feature branch"

    def test_create_branch_from_default(self, temp_storage):
        """Test creating branch without specifying source uses default."""
        manager, _ = temp_storage

        manager.update_head("main", "orders", 2)

        feature = manager.create("feature/orders")

        assert feature.head == {"orders": 2}
        assert feature.parent_branch == "main"

    def test_list_branches(self, temp_storage):
        """Test listing all branches."""
        manager, _ = temp_storage

        manager.create("alpha")
        manager.create("beta")
        manager.create("feature/gamma")

        branches = manager.list()

        assert "main" in branches
        assert "alpha" in branches
        assert "beta" in branches
        assert "feature/gamma" in branches

    def test_delete_branch(self, temp_storage):
        """Test deleting a branch."""
        manager, _ = temp_storage

        manager.create("to-delete")
        assert "to-delete" in manager.list()

        manager.delete("to-delete")
        assert "to-delete" not in manager.list()

    def test_cannot_delete_default_branch(self, temp_storage):
        """Test that default branch cannot be deleted."""
        manager, _ = temp_storage

        with pytest.raises(ValueError, match="Cannot delete default branch"):
            manager.delete("main")

    def test_branch_not_found(self, temp_storage):
        """Test error when branch doesn't exist."""
        manager, _ = temp_storage

        with pytest.raises(IOError, match="Branch not found"):
            manager.get("nonexistent")

    def test_branch_already_exists(self, temp_storage):
        """Test error when creating duplicate branch."""
        manager, _ = temp_storage

        manager.create("duplicate")

        with pytest.raises(ValueError, match="Branch already exists"):
            manager.create("duplicate")


class TestBranchHeadOperations:
    """Tests for branch head pointer operations."""

    def test_update_head(self, temp_storage):
        """Test updating head pointers."""
        manager, _ = temp_storage

        manager.update_head("main", "users", 1)
        manager.update_head("main", "users", 2)
        manager.update_head("main", "orders", 1)

        main = manager.get("main")
        assert main.head["users"] == 2
        assert main.head["orders"] == 1

    def test_get_table_version(self, temp_storage):
        """Test getting table version from branch."""
        manager, _ = temp_storage

        manager.update_head("main", "users", 5)

        version = manager.get_table_version("main", "users")
        assert version == 5

        # Non-existent table returns None
        version = manager.get_table_version("main", "nonexistent")
        assert version is None

    def test_branch_isolation(self, temp_storage):
        """Test that changes on one branch don't affect another."""
        manager, _ = temp_storage

        # Setup main with users v1
        manager.update_head("main", "users", 1)

        # Create feature branch
        manager.create("feature")

        # Update users on feature to v2
        manager.update_head("feature", "users", 2)

        # Main should still be v1
        assert manager.get_table_version("main", "users") == 1
        assert manager.get_table_version("feature", "users") == 2


class TestBranchDiff:
    """Tests for comparing branches."""

    def test_diff_identical(self, temp_storage):
        """Test diff of identical branches."""
        manager, _ = temp_storage

        manager.update_head("main", "users", 1)
        manager.create("feature")

        diff = manager.diff("feature", "main")

        assert diff.unchanged == ["users"]
        assert diff.modified == []
        assert diff.added_in_source == []
        assert diff.added_in_target == []
        assert diff.has_conflicts is False

    def test_diff_modified(self, temp_storage):
        """Test diff when table versions differ."""
        manager, _ = temp_storage

        manager.update_head("main", "users", 1)
        manager.create("feature")
        manager.update_head("feature", "users", 2)

        diff = manager.diff("feature", "main")

        assert diff.unchanged == []
        assert ("users", 2, 1) in diff.modified
        assert diff.has_conflicts is True

    def test_diff_added_tables(self, temp_storage):
        """Test diff with tables added on one branch."""
        manager, _ = temp_storage

        manager.update_head("main", "users", 1)
        manager.create("feature")
        manager.update_head("feature", "orders", 1)  # New table on feature

        diff = manager.diff("feature", "main")

        assert diff.unchanged == ["users"]
        assert ("orders", 1) in diff.added_in_source
        assert diff.has_conflicts is False


class TestBranchMerge:
    """Tests for merging branches."""

    def test_fast_forward_merge(self, temp_storage):
        """Test fast-forward merge."""
        manager, _ = temp_storage

        manager.update_head("main", "users", 1)
        manager.create("feature")
        manager.update_head("feature", "orders", 1)  # Add new table

        # Should be able to fast-forward
        assert manager.can_fast_forward("feature", "main") is True

        manager.merge("feature", into="main")

        # Main should now have orders
        main = manager.get("main")
        assert main.head["users"] == 1
        assert main.head["orders"] == 1

    def test_cannot_fast_forward_diverged(self, temp_storage):
        """Test that diverged branches cannot fast-forward."""
        manager, _ = temp_storage

        manager.update_head("main", "users", 1)
        manager.create("feature")

        # Both branches modify users
        manager.update_head("feature", "users", 2)
        manager.update_head("main", "users", 3)

        # Should not be able to fast-forward
        assert manager.can_fast_forward("feature", "main") is False

        with pytest.raises(ValueError, match="Merge conflict"):
            manager.merge("feature", into="main")


class TestZeroCopyBranching:
    """Tests to verify zero-copy semantics."""

    def test_branch_creation_is_instant(self, temp_storage):
        """Test that branch creation doesn't copy data."""
        manager, base_dir = temp_storage

        # Add many tables to main
        for i in range(100):
            manager.update_head("main", f"table_{i}", i)

        # Measure size before branch
        import time
        start = time.perf_counter()

        # Create many branches (should be instant)
        for i in range(50):
            manager.create(f"branch_{i}")

        elapsed = time.perf_counter() - start

        # 50 branches should be created in under 1 second
        # (actual creation should be < 100ms, but we allow margin)
        assert elapsed < 1.0, f"Branch creation took {elapsed:.2f}s, expected < 1s"

    def test_branch_storage_overhead(self, temp_storage):
        """Test that branch metadata is small."""
        manager, base_dir = temp_storage

        # Add tables to main
        for i in range(10):
            manager.update_head("main", f"table_{i}", i)

        # Create a branch
        manager.create("feature")

        # Get branch file size
        branches_dir = os.path.join(base_dir, "branches", "_branches")
        feature_file = os.path.join(branches_dir, "feature.json")

        file_size = os.path.getsize(feature_file)

        # Branch file should be small (just JSON with pointers)
        # 10 tables * ~20 bytes each + overhead = ~500 bytes max
        assert file_size < 1024, f"Branch file is {file_size} bytes, expected < 1KB"


class TestBranchValidation:
    """Tests for branch name validation."""

    def test_valid_branch_names(self, temp_storage):
        """Test various valid branch name formats."""
        manager, _ = temp_storage

        valid_names = [
            "feature",
            "feature-123",
            "feature_test",
            "feature/test",
            "feature/test/nested",
            "UPPERCASE",
            "MixedCase123",
        ]

        for name in valid_names:
            branch = manager.create(name)
            assert branch.name == name
            manager.delete(name)

    def test_invalid_branch_names(self, temp_storage):
        """Test that invalid branch names are rejected."""
        manager, _ = temp_storage

        invalid_names = [
            "",  # Empty
            "_hidden",  # Starts with underscore
            "feature//test",  # Double slashes
        ]

        for name in invalid_names:
            with pytest.raises(ValueError, match="Invalid branch name"):
                manager.create(name)
