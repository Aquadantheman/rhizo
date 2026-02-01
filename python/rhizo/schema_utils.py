"""
Arrow schema serialization, deserialization, and comparison utilities.

Supports storing schemas as JSON in TableVersion.metadata and comparing
schemas across versions for evolution enforcement.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import pyarrow as pa

SCHEMA_METADATA_KEY = "__arrow_schema"

# Maps Arrow type strings to constructors
_TYPE_MAP: Dict[str, pa.DataType] = {
    "int8": pa.int8(),
    "int16": pa.int16(),
    "int32": pa.int32(),
    "int64": pa.int64(),
    "uint8": pa.uint8(),
    "uint16": pa.uint16(),
    "uint32": pa.uint32(),
    "uint64": pa.uint64(),
    "float16": pa.float16(),
    "float32": pa.float32(),
    "float64": pa.float64(),
    "double": pa.float64(),
    "bool": pa.bool_(),
    "string": pa.string(),
    "utf8": pa.utf8(),
    "large_string": pa.large_string(),
    "large_utf8": pa.large_utf8(),
    "binary": pa.binary(),
    "large_binary": pa.large_binary(),
    "date32": pa.date32(),
    "date32[day]": pa.date32(),
    "date64": pa.date64(),
    "timestamp[ns]": pa.timestamp("ns"),
    "timestamp[us]": pa.timestamp("us"),
    "timestamp[ms]": pa.timestamp("ms"),
    "timestamp[s]": pa.timestamp("s"),
    "duration[ns]": pa.duration("ns"),
    "duration[us]": pa.duration("us"),
    "duration[ms]": pa.duration("ms"),
    "duration[s]": pa.duration("s"),
    "null": pa.null(),
}


def _type_to_string(t: pa.DataType) -> str:
    """Convert an Arrow type to its string representation."""
    return str(t)


def _string_to_type(s: str) -> pa.DataType:
    """Convert a type string back to an Arrow type. Falls back to string for unknown types."""
    if s in _TYPE_MAP:
        return _TYPE_MAP[s]
    # Handle parameterized types like "timestamp[ns, tz=UTC]"
    if s.startswith("timestamp["):
        inner = s[len("timestamp["):-1]
        parts = [p.strip() for p in inner.split(",")]
        unit = parts[0]
        tz = parts[1].split("=")[1] if len(parts) > 1 else None
        return pa.timestamp(unit, tz=tz)
    # Unknown type: fall back to string
    return pa.string()


def serialize_schema(schema: pa.Schema) -> str:
    """Serialize an Arrow schema to a JSON string."""
    fields = []
    for f in schema:
        fields.append({"name": f.name, "type": _type_to_string(f.type)})
    return json.dumps(fields)


def deserialize_schema(s: str) -> pa.Schema:
    """Deserialize a JSON string back to an Arrow schema."""
    fields_data = json.loads(s)
    fields = []
    for f in fields_data:
        fields.append(pa.field(f["name"], _string_to_type(f["type"])))
    return pa.schema(fields)


@dataclass
class SchemaComparisonResult:
    """Result of comparing two schemas."""

    compatible: bool = True
    added_columns: List[Tuple[str, str]] = field(default_factory=list)
    removed_columns: List[Tuple[str, str]] = field(default_factory=list)
    type_changes: List[Tuple[str, str, str]] = field(default_factory=list)
    error_message: Optional[str] = None


def compare_schemas(
    existing: pa.Schema,
    new: pa.Schema,
    mode: str = "additive",
) -> SchemaComparisonResult:
    """Compare two schemas and check compatibility under the given mode.

    Args:
        existing: The current schema.
        new: The proposed new schema.
        mode: "additive" (new columns OK, removals/type changes error)
              or "flexible" (everything allowed).

    Returns:
        SchemaComparisonResult with compatibility info.
    """
    result = SchemaComparisonResult()

    existing_fields = {f.name: f.type for f in existing}
    new_fields = {f.name: f.type for f in new}

    # Added columns
    for name in new_fields:
        if name not in existing_fields:
            result.added_columns.append((name, str(new_fields[name])))

    # Removed columns
    for name in existing_fields:
        if name not in new_fields:
            result.removed_columns.append((name, str(existing_fields[name])))

    # Type changes
    for name in existing_fields:
        if name in new_fields and existing_fields[name] != new_fields[name]:
            result.type_changes.append(
                (name, str(existing_fields[name]), str(new_fields[name]))
            )

    # Check compatibility based on mode
    if mode == "additive":
        errors = []
        if result.removed_columns:
            cols = ", ".join(f"'{c[0]}'" for c in result.removed_columns)
            errors.append(f"columns removed: {cols}")
        if result.type_changes:
            changes = ", ".join(
                f"'{c[0]}' ({c[1]} -> {c[2]})" for c in result.type_changes
            )
            errors.append(f"type changes: {changes}")
        if errors:
            result.compatible = False
            result.error_message = (
                f"Schema evolution blocked (mode='additive'): {'; '.join(errors)}. "
                f"Use schema_mode='flexible' to allow breaking changes."
            )

    # In flexible mode, everything is compatible
    return result
