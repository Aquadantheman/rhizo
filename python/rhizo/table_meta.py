"""
Table-level metadata storage for primary keys and schema evolution modes.

Stores persistent per-table configuration in _table_meta.json alongside
version files. The underscore prefix ensures FileCatalog.list_versions()
ignores it (only parses numeric .json files).
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import List, Optional


@dataclass
class TableMeta:
    """Per-table metadata: primary key and schema evolution mode."""

    primary_key: Optional[List[str]] = None
    schema_mode: str = "additive"
    created_at: Optional[float] = None

    def __post_init__(self):
        if self.schema_mode not in ("additive", "flexible"):
            raise ValueError(
                f"Invalid schema_mode '{self.schema_mode}': must be 'additive' or 'flexible'"
            )


class TableMetaStore:
    """Reads and writes _table_meta.json files in the catalog directory."""

    META_FILENAME = "_table_meta.json"

    def __init__(self, catalog_dir: str):
        self._catalog_dir = Path(catalog_dir)

    def load(self, table_name: str) -> TableMeta:
        """Load table metadata. Returns defaults if file missing or corrupted."""
        path = self._meta_path(table_name)
        if not path.exists():
            return TableMeta()
        try:
            with open(path, "r") as f:
                data = json.load(f)
            return TableMeta(
                primary_key=data.get("primary_key"),
                schema_mode=data.get("schema_mode", "additive"),
                created_at=data.get("created_at"),
            )
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            return TableMeta()

    def save(self, table_name: str, meta: TableMeta) -> None:
        """Save table metadata atomically (write-then-rename)."""
        path = self._meta_path(table_name)
        path.parent.mkdir(parents=True, exist_ok=True)

        if meta.created_at is None:
            meta.created_at = time.time()

        tmp_path = path.with_suffix(".json.tmp")
        data = asdict(meta)
        with open(tmp_path, "w") as f:
            json.dump(data, f, indent=2)

        # Atomic rename (on Windows, need to remove target first if exists)
        if os.name == "nt" and path.exists():
            path.unlink()
        os.rename(tmp_path, path)

    def _meta_path(self, table_name: str) -> Path:
        return self._catalog_dir / table_name / self.META_FILENAME
