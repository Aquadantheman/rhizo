"""Type stubs for the armillaria Rust extension module."""

from typing import List, Dict, Optional, Tuple

class PyChunkStore:
    def __init__(self, path: str) -> None: ...
    def put(self, data: bytes) -> str: ...
    def get(self, hash: str) -> bytes: ...
    def get_verified(self, hash: str) -> bytes: ...
    def exists(self, hash: str) -> bool: ...
    def delete(self, hash: str) -> None: ...

class PyTableVersion:
    table_name: str
    version: int
    chunk_hashes: List[str]
    schema_hash: Optional[str]
    created_at: int
    parent_version: Optional[int]
    metadata: Dict[str, str]

    def __init__(
        self,
        table_name: str,
        version: int,
        chunk_hashes: List[str],
    ) -> None: ...

class PyCatalog:
    def __init__(self, path: str) -> None: ...
    def commit(self, version: PyTableVersion) -> int: ...
    def get_version(
        self,
        table_name: str,
        version: Optional[int] = None,
    ) -> PyTableVersion: ...
    def list_versions(self, table_name: str) -> List[int]: ...
    def list_tables(self) -> List[str]: ...

class PyBranch:
    """A branch represents a named pointer to table versions."""
    name: str
    head: Dict[str, int]
    created_at: int
    parent_branch: Optional[str]
    description: Optional[str]

class PyBranchDiff:
    """Result of comparing two branches."""
    source_branch: str
    target_branch: str
    unchanged: List[str]
    modified: List[Tuple[str, int, int]]  # (table_name, source_version, target_version)
    added_in_source: List[Tuple[str, int]]
    added_in_target: List[Tuple[str, int]]
    has_conflicts: bool

class PyBranchManager:
    """Manages branches for UDR tables."""
    def __init__(self, path: str) -> None: ...
    def create(
        self,
        name: str,
        from_branch: Optional[str] = None,
        description: Optional[str] = None,
    ) -> PyBranch: ...
    def get(self, name: str) -> PyBranch: ...
    def list(self) -> List[str]: ...
    def delete(self, name: str) -> None: ...
    def update_head(self, branch_name: str, table_name: str, version: int) -> None: ...
    def get_table_version(self, branch_name: str, table_name: str) -> Optional[int]: ...
    def diff(self, source: str, target: str) -> PyBranchDiff: ...
    def can_fast_forward(self, source: str, target: str) -> bool: ...
    def merge(self, source: str, into: str) -> None: ...
    def get_default(self) -> Optional[str]: ...
    def set_default(self, name: str) -> None: ...

class PyTransactionInfo:
    """Information about a transaction."""
    tx_id: int
    epoch_id: int
    status: str
    branch: str
    started_at: int
    committed_at: Optional[int]
    read_snapshot: Dict[str, int]
    written_tables: List[str]

class PyRecoveryReport:
    """Result of transaction recovery process."""
    last_committed_epoch: Optional[int]
    replayed: List[int]
    rolled_back: List[int]
    already_aborted: List[int]
    already_committed: List[int]
    warnings: List[str]
    errors: List[str]
    is_clean: bool

class PyTransactionManager:
    """Manages cross-table ACID transactions."""
    def __init__(
        self,
        base_path: str,
        catalog_path: str,
        branch_path: Optional[str] = None,
        auto_recover: bool = False,
    ) -> None: ...
    def begin(self, branch: Optional[str] = None) -> int: ...
    def add_write(
        self,
        tx_id: int,
        table_name: str,
        new_version: int,
        chunk_hashes: List[str],
    ) -> None: ...
    def record_read(self, tx_id: int, table_name: str, version: int) -> None: ...
    def commit(self, tx_id: int) -> None: ...
    def abort(self, tx_id: int, reason: str = "User requested") -> None: ...
    def get_transaction(self, tx_id: int) -> PyTransactionInfo: ...
    def active_transactions(self) -> List[PyTransactionInfo]: ...
    def active_count(self) -> int: ...
    def recover(self) -> PyRecoveryReport: ...
    def recover_and_apply(self) -> PyRecoveryReport: ...
    def verify_consistency(self) -> List[str]: ...
    def get_changelog(
        self,
        since_tx_id: Optional[int] = None,
        since_timestamp: Optional[int] = None,
        tables: Optional[List[str]] = None,
        branch: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> List["PyChangelogEntry"]: ...
    def latest_tx_id(self) -> Optional[int]: ...

class PyTableChange:
    """A single table change within a committed transaction."""
    table_name: str
    old_version: Optional[int]
    new_version: int
    chunk_hashes: List[str]

    def is_new_table(self) -> bool: ...

class PyChangelogEntry:
    """Entry in the changelog representing a committed transaction."""
    tx_id: int
    epoch_id: int
    committed_at: int
    branch: str
    changes: List[PyTableChange]

    def changed_tables(self) -> List[str]: ...
    def contains_table(self, table_name: str) -> bool: ...
    def get_change(self, table_name: str) -> Optional[PyTableChange]: ...
    def change_count(self) -> int: ...
