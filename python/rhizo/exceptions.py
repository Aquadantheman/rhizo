"""
Rhizo exception types for specific error conditions.

These custom exceptions provide:
1. Type-safe error handling (no string matching)
2. Structured error information (table_name, version, etc.)
3. Backwards compatibility (inherit from standard exceptions)
"""

from __future__ import annotations

import re


class RhizoError(Exception):
    """Base class for all Rhizo-specific errors."""
    pass


class TableNotFoundError(RhizoError, IOError):
    """
    Raised when a table does not exist in the catalog.

    Inherits from IOError for backwards compatibility with existing
    `except IOError` handlers.
    """

    def __init__(self, table_name: str):
        self.table_name = table_name
        super().__init__(f"Table not found: {table_name}")


class VersionNotFoundError(RhizoError, IOError):
    """
    Raised when a specific version does not exist for a table.

    Inherits from IOError for backwards compatibility.
    """

    def __init__(self, table_name: str, version: int):
        self.table_name = table_name
        self.version = version
        super().__init__(f"Version {version} not found for table: {table_name}")


class EmptyResultError(RhizoError, ValueError):
    """
    Raised when a query or filter returns no results.

    Inherits from ValueError for backwards compatibility with existing
    `except ValueError` handlers.
    """

    def __init__(self, message: str = "Query returned empty result"):
        super().__init__(message)


class SchemaEvolutionError(RhizoError, ValueError):
    """
    Raised when a schema change violates the table's evolution policy.

    Inherits from ValueError for backwards compatibility.
    """

    def __init__(self, table_name: str, message: str):
        self.table_name = table_name
        super().__init__(f"Schema evolution error on '{table_name}': {message}")


class PrimaryKeyViolationError(RhizoError, ValueError):
    """
    Raised when a write would create duplicate primary key values.

    Inherits from ValueError for backwards compatibility.
    """

    def __init__(self, table_name: str, key_columns: list, duplicate_count: int):
        self.table_name = table_name
        self.key_columns = key_columns
        self.duplicate_count = duplicate_count
        keys = ", ".join(key_columns)
        super().__init__(
            f"Primary key violation on '{table_name}': "
            f"{duplicate_count} duplicate(s) in columns ({keys})"
        )


class SizeLimitExceededError(RhizoError, ValueError):
    """
    Raised when input data exceeds configured size limits.

    Prevents OOM attacks from oversized inputs.
    """

    def __init__(self, actual: int, maximum: int, unit: str = "bytes"):
        self.actual = actual
        self.maximum = maximum
        self.unit = unit
        super().__init__(
            f"Size limit exceeded: {actual:,} {unit} > {maximum:,} {unit} maximum"
        )


# Pre-compiled regex for table name validation
_TABLE_NAME_PATTERN = re.compile(r'^[a-zA-Z_][a-zA-Z0-9_]*$')


def validate_table_name(table_name: str) -> str:
    """
    Validate and normalize a table name to prevent path traversal attacks.

    This should be called at every public entry point that accepts a table name.

    Args:
        table_name: Name of the table to validate

    Returns:
        Normalized (lowercase) table name

    Raises:
        ValueError: If table name is invalid
    """
    if not table_name:
        raise ValueError("Table name cannot be empty")

    # Normalize to lowercase
    normalized = table_name.lower()

    # Check length (reasonable limit to prevent issues)
    if len(normalized) > 128:
        raise ValueError(f"Table name too long (max 128 chars): {len(normalized)} chars")

    # Must be a valid identifier: start with letter/underscore, alphanumeric + underscore only
    if not _TABLE_NAME_PATTERN.match(normalized):
        raise ValueError(
            f"Invalid table name '{table_name}': must start with a letter or underscore "
            "and contain only letters, numbers, and underscores"
        )

    # Explicitly check for path traversal patterns (defense in depth)
    dangerous_patterns = ['..', '/', '\\', '\x00']
    for pattern in dangerous_patterns:
        if pattern in table_name:
            raise ValueError(f"Invalid table name '{table_name}': contains forbidden character sequence")

    return normalized
