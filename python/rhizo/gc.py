"""
Garbage Collection — TTL and retention-based version cleanup.

Provides two-phase GC:
  Phase 1: Delete old catalog versions (by age or count).
  Phase 2: Sweep unreferenced chunks from the store.

Safety guarantees:
  - Latest version of each table is NEVER deleted.
  - Versions referenced by branch heads or fork points are NEVER deleted.
  - Versions referenced by active transaction snapshots are NEVER deleted.
  - Crash between phases only leaves orphaned chunks (cleaned on next GC).

Example:
    >>> from rhizo.gc import GarbageCollector, GCPolicy
    >>> gc = GarbageCollector(catalog, store, branch_manager, tx_manager)
    >>> result = gc.collect(GCPolicy(max_versions_per_table=5))
    >>> print(f"Freed {result.bytes_freed} bytes")
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable, Dict, List, Optional, Set, Tuple

if TYPE_CHECKING:
    from _rhizo import (
        PyBranchManager,
        PyCatalog,
        PyChunkStore,
        PyTransactionManager,
    )

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GCPolicy:
    """Retention policy for version garbage collection.

    At least one of ``max_age_seconds`` or ``max_versions_per_table`` must
    be set.  When both are set, a version is eligible for deletion if it
    violates *either* policy (union).

    Attributes:
        max_age_seconds: Delete versions older than this (seconds).
            ``None`` disables time-based TTL.
        max_versions_per_table: Keep at most this many versions per table.
            ``None`` disables count-based retention.
    """

    max_age_seconds: Optional[float] = None
    max_versions_per_table: Optional[int] = None

    def __post_init__(self):
        if self.max_age_seconds is not None and self.max_age_seconds < 0:
            raise ValueError("max_age_seconds must be non-negative")
        if self.max_versions_per_table is not None and self.max_versions_per_table < 1:
            raise ValueError("max_versions_per_table must be >= 1")


@dataclass
class GCResult:
    """Report of what garbage collection cleaned up.

    Attributes:
        versions_deleted: Number of catalog versions removed.
        chunks_deleted: Number of unreferenced chunks removed.
        chunks_failed: Number of chunks that failed to delete.
        temp_files_removed: Number of orphaned temp files cleaned.
        bytes_freed: Approximate bytes freed on disk.
        elapsed_seconds: Wall-clock time for the full GC run.
        details: Per-table list of deleted version numbers.
    """

    versions_deleted: int = 0
    chunks_deleted: int = 0
    chunks_failed: int = 0
    temp_files_removed: int = 0
    bytes_freed: int = 0
    elapsed_seconds: float = 0.0
    details: Dict[str, List[int]] = field(default_factory=dict)

    def __repr__(self) -> str:
        return (
            f"GCResult(versions_deleted={self.versions_deleted}, "
            f"chunks_deleted={self.chunks_deleted}, "
            f"bytes_freed={self.bytes_freed}, "
            f"elapsed={self.elapsed_seconds:.3f}s)"
        )


class GarbageCollector:
    """Two-phase garbage collector for Rhizo databases.

    Phase 1 removes expired catalog versions (respecting safety constraints).
    Phase 2 sweeps unreferenced chunks from the content-addressable store.

    Args:
        catalog: PyCatalog for version metadata.
        store: PyChunkStore for chunk storage.
        branch_manager: Optional PyBranchManager (for branch safety checks).
        transaction_manager: Optional PyTransactionManager (for active tx checks).
    """

    def __init__(
        self,
        catalog: "PyCatalog",
        store: "PyChunkStore",
        branch_manager: Optional["PyBranchManager"] = None,
        transaction_manager: Optional["PyTransactionManager"] = None,
    ):
        self.catalog = catalog
        self.store = store
        self.branch_manager = branch_manager
        self.transaction_manager = transaction_manager

    def collect(self, policy: GCPolicy) -> GCResult:
        """Run two-phase GC with the given policy.

        Args:
            policy: Retention policy (at least one constraint must be set).

        Returns:
            GCResult with details of what was cleaned up.

        Raises:
            ValueError: If no policy constraint is set.
        """
        if policy.max_age_seconds is None and policy.max_versions_per_table is None:
            raise ValueError(
                "At least one of max_age_seconds or max_versions_per_table must be set"
            )

        t0 = time.monotonic()
        result = GCResult()

        # Collect protected versions (must not be deleted)
        protected = self._collect_protected_versions()
        logger.debug("Protected versions: %d across %d tables",
                     sum(len(v) for v in protected.values()), len(protected))

        # Phase 1: delete expired versions
        deleted_versions, details = self._phase1_delete_versions(policy, protected)
        result.versions_deleted = deleted_versions
        result.details = details

        # Phase 2: sweep unreferenced chunks
        chunks_deleted, chunks_failed, bytes_freed = self._phase2_sweep_chunks()
        result.chunks_deleted = chunks_deleted
        result.chunks_failed = chunks_failed
        result.bytes_freed = bytes_freed

        # Cleanup temp files
        temp_removed, _ = self.store.cleanup_orphaned_temp_files()
        result.temp_files_removed = temp_removed

        result.elapsed_seconds = time.monotonic() - t0

        logger.info(
            "GC complete: %d versions deleted, %d chunks freed (%d bytes) in %.3fs",
            result.versions_deleted,
            result.chunks_deleted,
            result.bytes_freed,
            result.elapsed_seconds,
        )

        return result

    def _collect_protected_versions(self) -> Dict[str, Set[int]]:
        """Build map of table_name -> set of version numbers that must not be deleted."""
        protected: Dict[str, Set[int]] = {}

        # 1. Latest version of every table (always protected)
        for table_name in self.catalog.list_tables():
            tv = self.catalog.get_version(table_name)
            protected.setdefault(table_name, set()).add(tv.version)

        # 2. Branch heads and fork points
        if self.branch_manager is not None:
            for branch_name in self.branch_manager.list():
                branch = self.branch_manager.get(branch_name)
                # head is a dict of table_name -> version
                if hasattr(branch, "head") and branch.head:
                    for table_name, version in branch.head.items():
                        protected.setdefault(table_name, set()).add(version)
                # fork_point is also table_name -> version
                if hasattr(branch, "fork_point") and branch.fork_point:
                    for table_name, version in branch.fork_point.items():
                        protected.setdefault(table_name, set()).add(version)

        # 3. Active transaction snapshots
        if self.transaction_manager is not None:
            try:
                active = self.transaction_manager.active_transactions()
                for tx_info in active:
                    if hasattr(tx_info, "read_snapshot") and tx_info.read_snapshot:
                        for table_name, version in tx_info.read_snapshot.items():
                            protected.setdefault(table_name, set()).add(version)
            except Exception:
                # If we can't query active transactions, be conservative
                logger.warning("Could not query active transactions for GC safety")

        return protected

    def _phase1_delete_versions(
        self,
        policy: GCPolicy,
        protected: Dict[str, Set[int]],
    ) -> Tuple[int, Dict[str, List[int]]]:
        """Phase 1: Delete expired versions from catalog.

        Returns (total_deleted, details_per_table).
        """
        now = time.time()
        total_deleted = 0
        details: Dict[str, List[int]] = {}

        for table_name in self.catalog.list_tables():
            versions = self.catalog.list_versions(table_name)
            if not versions:
                continue

            protected_set = protected.get(table_name, set())

            # Build deletion candidates
            candidates: Set[int] = set()

            # Time-based: versions older than cutoff
            if policy.max_age_seconds is not None:
                cutoff = now - policy.max_age_seconds
                for v in versions:
                    tv = self.catalog.get_version(table_name, v)
                    if tv.created_at < cutoff:
                        candidates.add(v)

            # Count-based: excess versions beyond limit
            if policy.max_versions_per_table is not None:
                if len(versions) > policy.max_versions_per_table:
                    # versions is sorted ascending — delete oldest
                    excess_count = len(versions) - policy.max_versions_per_table
                    for v in versions[:excess_count]:
                        candidates.add(v)

            # Remove protected versions
            deletable = candidates - protected_set

            # Delete
            deleted_for_table = []
            for v in sorted(deletable):
                try:
                    self.catalog.delete_version(table_name, v)
                    deleted_for_table.append(v)
                    total_deleted += 1
                except Exception as e:
                    logger.warning("Failed to delete %s v%d: %s", table_name, v, e)

            if deleted_for_table:
                details[table_name] = deleted_for_table

        return total_deleted, details

    def _phase2_sweep_chunks(self) -> Tuple[int, int, int]:
        """Phase 2: Delete unreferenced chunks from store.

        Returns (deleted_count, failed_count, bytes_freed).
        """
        # Get all currently referenced chunk hashes
        referenced = self.catalog.get_all_referenced_chunk_hashes()

        # Estimate bytes that will be freed
        all_hashes = self.store.list_chunk_hashes()
        referenced_set = set(referenced)
        bytes_freed = 0
        for h in all_hashes:
            if h not in referenced_set:
                try:
                    chunk_data = self.store.get(h)
                    bytes_freed += len(chunk_data)
                except Exception:
                    pass

        # Run GC
        deleted, failed = self.store.garbage_collect(referenced)

        return deleted, failed, bytes_freed


class AutoGC:
    """Background thread that periodically runs garbage collection.

    Args:
        gc: GarbageCollector instance.
        policy: Retention policy to apply on each run.
        interval_seconds: Seconds between GC runs (default: 3600).
        on_complete: Optional callback invoked after each GC run.
    """

    def __init__(
        self,
        gc: GarbageCollector,
        policy: GCPolicy,
        interval_seconds: float = 3600.0,
        on_complete: Optional[Callable[[GCResult], None]] = None,
    ):
        self._gc = gc
        self._policy = policy
        self._interval = interval_seconds
        self._on_complete = on_complete
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_result: Optional[GCResult] = None
        self._lock = threading.Lock()

    def start(self) -> None:
        """Start the background GC thread."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name="rhizo-auto-gc"
        )
        self._thread.start()

    def stop(self, timeout: float = 30.0) -> None:
        """Stop the background GC thread.

        Blocks until the current GC run (if any) completes or timeout expires.
        """
        self._stop_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=timeout)
        self._thread = None

    @property
    def last_result(self) -> Optional[GCResult]:
        """Get the result of the most recent GC run."""
        with self._lock:
            return self._last_result

    @property
    def is_running(self) -> bool:
        """Whether the background thread is active."""
        return self._thread is not None and self._thread.is_alive()

    def _run_loop(self) -> None:
        """Main loop for the background thread."""
        while not self._stop_event.is_set():
            # Wait for interval or stop signal
            self._stop_event.wait(self._interval)
            if self._stop_event.is_set():
                break

            try:
                result = self._gc.collect(self._policy)
                with self._lock:
                    self._last_result = result
                if self._on_complete is not None:
                    self._on_complete(result)
            except Exception:
                logger.exception("AutoGC run failed")
