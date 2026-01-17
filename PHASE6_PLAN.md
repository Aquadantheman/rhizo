# Phase 6: Changelog & Subscriptions - Implementation Plan

## Overview

Phase 6 completes the unified batch/stream vision by exposing the transaction log as a queryable changelog with subscription support. The key insight: **the transaction log IS the changelog** - we don't need Kafka or a separate streaming system.

**Design Principles:**
1. **Lightweight** - Build entirely on existing transaction log infrastructure
2. **Robust** - Leverage battle-tested persistence from Phase 5
3. **Adoptable** - Simple polling-based API that works without complex infrastructure
4. **Future-proof** - Can add push notifications later without API changes

## Vision Alignment

From the whitepaper:
> "Batch query: What is the state at version V? Stream query: What changed since version V? Same data model, same guarantees."

From VISION.md:
```python
# Batch: What's the current state?
current = engine.query("SELECT * FROM events")

# Stream: What's changed?
for change in engine.subscribe("events", since_version=100):
    process(change)
```

## What We Already Have

| Component | Location | Status |
|-----------|----------|--------|
| Transaction Log | `udr_core/src/transaction/log.rs` | Persists all committed transactions |
| TransactionRecord | `udr_core/src/transaction/types.rs` | Contains writes with table, version, chunks |
| diff_versions() | `python/udr_query/engine.py` | Row-level comparison |
| Epoch organization | `udr_core/src/transaction/epoch.rs` | Time-ordered commit structure |

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        Application                               │
│     engine.subscribe("users", since_version=5)                   │
│     engine.get_changes(since_tx=100)                            │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                       Subscriber (Python)                        │
│   • Polling loop with configurable interval                      │
│   • Tracks last seen tx_id/version                              │
│   • Callback or iterator interface                               │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                    Changelog (Rust/Python)                       │
│   • Query committed transactions since tx_id/timestamp           │
│   • Filter by table, branch                                      │
│   • Transform TransactionRecord → ChangelogEntry                 │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                     TransactionLog (Rust)                        │
│   • Already persists all committed transactions                  │
│   • Epoch-organized for efficient scanning                       │
│   • Add: list_committed_since(tx_id) method                     │
└─────────────────────────────────────────────────────────────────┘
```

---

## Part 1: Data Structures

### 1.1 ChangelogEntry (Rust)

**File:** `udr_core/src/changelog/entry.rs`

```rust
use serde::{Deserialize, Serialize};
use std::collections::HashMap;

/// A single table change within a commit
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TableChange {
    /// Table that was modified
    pub table_name: String,

    /// Previous version (None if new table)
    pub old_version: Option<u64>,

    /// New version after this commit
    pub new_version: u64,

    /// Chunk hashes for the new version
    pub chunk_hashes: Vec<String>,

    /// Schema changed in this version
    pub schema_changed: bool,
}

/// Entry in the changelog representing a committed transaction
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ChangelogEntry {
    /// Transaction ID (monotonically increasing, unique)
    pub tx_id: u64,

    /// Epoch this transaction was committed in
    pub epoch_id: u64,

    /// Unix timestamp when committed
    pub committed_at: i64,

    /// Branch this commit was on
    pub branch: String,

    /// Tables changed in this commit
    pub changes: Vec<TableChange>,

    /// User-provided metadata (if any)
    pub metadata: HashMap<String, String>,
}

impl ChangelogEntry {
    /// Create from a committed TransactionRecord
    pub fn from_transaction(
        tx: &TransactionRecord,
        previous_versions: &HashMap<String, u64>,
    ) -> Self {
        let changes = tx.writes.iter().map(|w| {
            TableChange {
                table_name: w.table_name.clone(),
                old_version: previous_versions.get(&w.table_name).copied(),
                new_version: w.new_version,
                chunk_hashes: w.chunk_hashes.clone(),
                schema_changed: false, // TODO: detect schema changes
            }
        }).collect();

        Self {
            tx_id: tx.tx_id,
            epoch_id: tx.epoch_id,
            committed_at: tx.committed_at.unwrap_or(0),
            branch: tx.branch.clone(),
            changes,
            metadata: tx.metadata.clone(),
        }
    }

    /// Get list of changed table names
    pub fn changed_tables(&self) -> Vec<&str> {
        self.changes.iter().map(|c| c.table_name.as_str()).collect()
    }
}
```

### 1.2 ChangelogQuery (Rust)

**File:** `udr_core/src/changelog/query.rs`

```rust
/// Query parameters for changelog
#[derive(Debug, Clone, Default)]
pub struct ChangelogQuery {
    /// Start from this transaction ID (exclusive)
    pub since_tx_id: Option<u64>,

    /// Start from this timestamp (inclusive)
    pub since_timestamp: Option<i64>,

    /// Filter to specific tables
    pub tables: Option<Vec<String>>,

    /// Filter to specific branch
    pub branch: Option<String>,

    /// Maximum entries to return
    pub limit: Option<usize>,
}

impl ChangelogQuery {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn since_tx(mut self, tx_id: u64) -> Self {
        self.since_tx_id = Some(tx_id);
        self
    }

    pub fn since_time(mut self, timestamp: i64) -> Self {
        self.since_timestamp = Some(timestamp);
        self
    }

    pub fn for_tables(mut self, tables: Vec<String>) -> Self {
        self.tables = Some(tables);
        self
    }

    pub fn on_branch(mut self, branch: &str) -> Self {
        self.branch = Some(branch.to_string());
        self
    }

    pub fn with_limit(mut self, limit: usize) -> Self {
        self.limit = Some(limit);
        self
    }
}
```

---

## Part 2: Rust Implementation

### 2.1 Changelog Manager

**File:** `udr_core/src/changelog/manager.rs`

```rust
use super::entry::{ChangelogEntry, TableChange};
use super::query::ChangelogQuery;
use crate::transaction::log::TransactionLog;
use crate::transaction::types::{TransactionRecord, TransactionStatus};
use crate::transaction::error::TransactionError;
use std::collections::HashMap;

/// Manages changelog queries
pub struct ChangelogManager<'a> {
    log: &'a TransactionLog,
}

impl<'a> ChangelogManager<'a> {
    pub fn new(log: &'a TransactionLog) -> Self {
        Self { log }
    }

    /// Query changelog entries matching the given criteria
    pub fn query(&self, q: ChangelogQuery) -> Result<Vec<ChangelogEntry>, TransactionError> {
        let committed = self.log.list_committed_transactions()?;

        let mut entries = Vec::new();
        let mut previous_versions: HashMap<String, u64> = HashMap::new();

        for tx in committed {
            // Filter by tx_id
            if let Some(since_tx) = q.since_tx_id {
                if tx.tx_id <= since_tx {
                    // Still need to track versions for later entries
                    for w in &tx.writes {
                        previous_versions.insert(w.table_name.clone(), w.new_version);
                    }
                    continue;
                }
            }

            // Filter by timestamp
            if let Some(since_ts) = q.since_timestamp {
                if let Some(committed_at) = tx.committed_at {
                    if committed_at < since_ts {
                        for w in &tx.writes {
                            previous_versions.insert(w.table_name.clone(), w.new_version);
                        }
                        continue;
                    }
                }
            }

            // Filter by branch
            if let Some(ref branch) = q.branch {
                if &tx.branch != branch {
                    continue;
                }
            }

            // Filter by tables
            let include = if let Some(ref tables) = q.tables {
                tx.writes.iter().any(|w| tables.contains(&w.table_name))
            } else {
                true
            };

            if include {
                let entry = ChangelogEntry::from_transaction(&tx, &previous_versions);
                entries.push(entry);

                // Check limit
                if let Some(limit) = q.limit {
                    if entries.len() >= limit {
                        break;
                    }
                }
            }

            // Track versions for future entries
            for w in &tx.writes {
                previous_versions.insert(w.table_name.clone(), w.new_version);
            }
        }

        Ok(entries)
    }

    /// Get the latest transaction ID in the changelog
    pub fn latest_tx_id(&self) -> Result<Option<u64>, TransactionError> {
        self.log.latest_committed_tx_id()
    }
}
```

### 2.2 TransactionLog Extensions

**File:** `udr_core/src/transaction/log.rs` (additions)

```rust
impl TransactionLog {
    /// List all committed transactions in order
    pub fn list_committed_transactions(&self) -> Result<Vec<TransactionRecord>, TransactionError> {
        let epochs = self.list_epochs()?;
        let mut committed = Vec::new();

        for epoch_id in epochs {
            let epoch_dir = self.epoch_dir(epoch_id);

            // List all transaction files in this epoch
            for entry in std::fs::read_dir(&epoch_dir)? {
                let entry = entry?;
                let path = entry.path();

                if path.extension().and_then(|s| s.to_str()) == Some("json") {
                    if let Some(name) = path.file_stem().and_then(|s| s.to_str()) {
                        if name.starts_with("tx_") {
                            let json = std::fs::read_to_string(&path)?;
                            let tx: TransactionRecord = serde_json::from_str(&json)?;

                            if matches!(tx.status, TransactionStatus::Committed) {
                                committed.push(tx);
                            }
                        }
                    }
                }
            }
        }

        // Sort by tx_id for consistent ordering
        committed.sort_by_key(|tx| tx.tx_id);

        Ok(committed)
    }

    /// Get the latest committed transaction ID
    pub fn latest_committed_tx_id(&self) -> Result<Option<u64>, TransactionError> {
        let committed = self.list_committed_transactions()?;
        Ok(committed.last().map(|tx| tx.tx_id))
    }
}
```

---

## Part 3: Python Bindings

### 3.1 PyChangelogEntry

**File:** `udr_python/src/lib.rs` (additions)

```rust
#[pyclass]
#[derive(Clone)]
pub struct PyTableChange {
    #[pyo3(get)]
    pub table_name: String,
    #[pyo3(get)]
    pub old_version: Option<u64>,
    #[pyo3(get)]
    pub new_version: u64,
    #[pyo3(get)]
    pub chunk_hashes: Vec<String>,
}

#[pyclass]
#[derive(Clone)]
pub struct PyChangelogEntry {
    #[pyo3(get)]
    pub tx_id: u64,
    #[pyo3(get)]
    pub epoch_id: u64,
    #[pyo3(get)]
    pub committed_at: i64,
    #[pyo3(get)]
    pub branch: String,
    #[pyo3(get)]
    pub changes: Vec<PyTableChange>,
}

impl From<ChangelogEntry> for PyChangelogEntry {
    fn from(entry: ChangelogEntry) -> Self {
        Self {
            tx_id: entry.tx_id,
            epoch_id: entry.epoch_id,
            committed_at: entry.committed_at,
            branch: entry.branch,
            changes: entry.changes.into_iter().map(|c| PyTableChange {
                table_name: c.table_name,
                old_version: c.old_version,
                new_version: c.new_version,
                chunk_hashes: c.chunk_hashes,
            }).collect(),
        }
    }
}

#[pymethods]
impl PyChangelogEntry {
    /// Get list of changed table names
    fn changed_tables(&self) -> Vec<String> {
        self.changes.iter().map(|c| c.table_name.clone()).collect()
    }
}
```

### 3.2 PyTransactionManager Extensions

**File:** `udr_python/src/lib.rs` (additions to PyTransactionManager)

```rust
#[pymethods]
impl PyTransactionManager {
    /// Get changelog entries since a transaction ID
    #[pyo3(signature = (since_tx_id=None, since_timestamp=None, tables=None, branch=None, limit=None))]
    fn get_changelog(
        &self,
        since_tx_id: Option<u64>,
        since_timestamp: Option<i64>,
        tables: Option<Vec<String>>,
        branch: Option<String>,
        limit: Option<usize>,
    ) -> PyResult<Vec<PyChangelogEntry>> {
        let mut query = ChangelogQuery::new();

        if let Some(tx_id) = since_tx_id {
            query = query.since_tx(tx_id);
        }
        if let Some(ts) = since_timestamp {
            query = query.since_time(ts);
        }
        if let Some(t) = tables {
            query = query.for_tables(t);
        }
        if let Some(b) = branch {
            query = query.on_branch(&b);
        }
        if let Some(l) = limit {
            query = query.with_limit(l);
        }

        let manager = ChangelogManager::new(&self.inner.log);
        let entries = manager.query(query).map_err(tx_err_to_py)?;

        Ok(entries.into_iter().map(PyChangelogEntry::from).collect())
    }

    /// Get the latest transaction ID in the changelog
    fn latest_tx_id(&self) -> PyResult<Option<u64>> {
        let manager = ChangelogManager::new(&self.inner.log);
        manager.latest_tx_id().map_err(tx_err_to_py)
    }
}
```

---

## Part 4: Python Subscriber

### 4.1 Subscriber Class

**File:** `python/udr_query/subscriber.py`

```python
"""
Subscriber for UDR changelog events.

Provides both polling-based iteration and callback interfaces
for processing changelog entries as they become available.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional, Dict, List, Callable, Iterator, Any
import time
import threading

if TYPE_CHECKING:
    import udr

@dataclass
class ChangeEvent:
    """A processed changelog entry for subscriber consumption."""
    tx_id: int
    committed_at: int
    branch: str
    table_name: str
    old_version: Optional[int]
    new_version: int

    @classmethod
    def from_changelog_entry(cls, entry: "udr.PyChangelogEntry") -> List["ChangeEvent"]:
        """Convert a changelog entry to individual change events."""
        events = []
        for change in entry.changes:
            events.append(cls(
                tx_id=entry.tx_id,
                committed_at=entry.committed_at,
                branch=entry.branch,
                table_name=change.table_name,
                old_version=change.old_version,
                new_version=change.new_version,
            ))
        return events


class Subscriber:
    """
    Subscribes to changelog events from UDR.

    Supports both polling-based iteration and callback interfaces.

    Example (iterator):
        >>> subscriber = Subscriber(tx_manager, since_tx_id=100)
        >>> for event in subscriber:
        ...     print(f"{event.table_name}: v{event.old_version} -> v{event.new_version}")

    Example (callback):
        >>> def on_change(event):
        ...     print(f"Change: {event.table_name}")
        >>> subscriber = Subscriber(tx_manager)
        >>> subscriber.subscribe(on_change)  # Blocks and calls on_change for each event

    Example (background):
        >>> subscriber = Subscriber(tx_manager)
        >>> subscriber.start_background(on_change)  # Non-blocking
        >>> # ... do other work ...
        >>> subscriber.stop()
    """

    def __init__(
        self,
        transaction_manager: "udr.PyTransactionManager",
        since_tx_id: Optional[int] = None,
        tables: Optional[List[str]] = None,
        branch: Optional[str] = None,
        poll_interval: float = 1.0,
    ):
        """
        Create a new subscriber.

        Args:
            transaction_manager: The transaction manager to subscribe to
            since_tx_id: Start from this transaction (exclusive). None = start from latest
            tables: Only receive events for these tables. None = all tables
            branch: Only receive events for this branch. None = all branches
            poll_interval: Seconds between polls (default: 1.0)
        """
        self._tx_manager = transaction_manager
        self._tables = tables
        self._branch = branch
        self._poll_interval = poll_interval

        # Initialize cursor
        if since_tx_id is not None:
            self._last_tx_id = since_tx_id
        else:
            # Start from current latest
            latest = self._tx_manager.latest_tx_id()
            self._last_tx_id = latest if latest is not None else 0

        # Background thread state
        self._running = False
        self._thread: Optional[threading.Thread] = None

    def __iter__(self) -> Iterator[ChangeEvent]:
        """Iterate over changelog events (polling)."""
        while True:
            entries = self._poll()

            if entries:
                for entry in entries:
                    for event in ChangeEvent.from_changelog_entry(entry):
                        yield event
            else:
                # No new events, wait before next poll
                time.sleep(self._poll_interval)

    def poll(self) -> List[ChangeEvent]:
        """
        Poll for new events without blocking.

        Returns events since last poll. Returns empty list if no new events.
        """
        entries = self._poll()
        events = []
        for entry in entries:
            events.extend(ChangeEvent.from_changelog_entry(entry))
        return events

    def subscribe(self, callback: Callable[[ChangeEvent], None]) -> None:
        """
        Subscribe with a callback (blocking).

        Calls the callback for each new event. Blocks forever
        (or until interrupted).

        Args:
            callback: Function to call for each event
        """
        for event in self:
            callback(event)

    def start_background(self, callback: Callable[[ChangeEvent], None]) -> None:
        """
        Start a background thread to process events.

        Args:
            callback: Function to call for each event
        """
        if self._running:
            raise RuntimeError("Subscriber already running")

        self._running = True
        self._thread = threading.Thread(
            target=self._background_loop,
            args=(callback,),
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        """Stop the background subscriber."""
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=self._poll_interval * 2)
            self._thread = None

    @property
    def last_tx_id(self) -> int:
        """Get the last processed transaction ID."""
        return self._last_tx_id

    def _poll(self) -> List["udr.PyChangelogEntry"]:
        """Poll for new changelog entries."""
        entries = self._tx_manager.get_changelog(
            since_tx_id=self._last_tx_id,
            tables=self._tables,
            branch=self._branch,
        )

        if entries:
            self._last_tx_id = entries[-1].tx_id

        return entries

    def _background_loop(self, callback: Callable[[ChangeEvent], None]) -> None:
        """Background thread main loop."""
        while self._running:
            try:
                entries = self._poll()
                for entry in entries:
                    for event in ChangeEvent.from_changelog_entry(entry):
                        if not self._running:
                            return
                        callback(event)

                if not entries:
                    time.sleep(self._poll_interval)
            except Exception as e:
                # Log error but continue (could add error callback)
                print(f"Subscriber error: {e}")
                time.sleep(self._poll_interval)
```

### 4.2 QueryEngine Integration

**File:** `python/udr_query/engine.py` (additions)

```python
from .subscriber import Subscriber, ChangeEvent

class QueryEngine:
    # ... existing code ...

    def get_changes(
        self,
        since_tx_id: Optional[int] = None,
        since_timestamp: Optional[int] = None,
        tables: Optional[List[str]] = None,
        branch: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """
        Get changelog entries since a specific point.

        This is the batch interface for the unified batch/stream model:
        - Batch query: "What is the state at version V?" -> engine.query()
        - Stream query: "What changed since tx X?" -> engine.get_changes()

        Args:
            since_tx_id: Start from this transaction (exclusive)
            since_timestamp: Start from this Unix timestamp
            tables: Filter to specific tables
            branch: Filter to specific branch
            limit: Maximum entries to return

        Returns:
            List of changelog entries as dicts

        Raises:
            RuntimeError: If transaction_manager is not configured
        """
        if self.transaction_manager is None:
            raise RuntimeError("Cannot get changes: transaction_manager not configured")

        entries = self.transaction_manager.get_changelog(
            since_tx_id=since_tx_id,
            since_timestamp=since_timestamp,
            tables=tables,
            branch=branch,
            limit=limit,
        )

        return [
            {
                "tx_id": e.tx_id,
                "epoch_id": e.epoch_id,
                "committed_at": e.committed_at,
                "branch": e.branch,
                "changes": [
                    {
                        "table_name": c.table_name,
                        "old_version": c.old_version,
                        "new_version": c.new_version,
                    }
                    for c in e.changes
                ],
            }
            for e in entries
        ]

    def subscribe(
        self,
        tables: Optional[List[str]] = None,
        since_tx_id: Optional[int] = None,
        branch: Optional[str] = None,
        poll_interval: float = 1.0,
    ) -> Subscriber:
        """
        Create a subscriber for changelog events.

        This is the streaming interface for the unified batch/stream model.
        Returns a Subscriber that can be iterated or used with callbacks.

        Args:
            tables: Only receive events for these tables
            since_tx_id: Start from this transaction (exclusive)
            branch: Filter to specific branch
            poll_interval: Seconds between polls

        Returns:
            Subscriber instance

        Raises:
            RuntimeError: If transaction_manager is not configured

        Example:
            >>> # Iterator style
            >>> for event in engine.subscribe(tables=["users"]):
            ...     print(f"{event.table_name}: {event.old_version} -> {event.new_version}")

            >>> # Callback style
            >>> def on_change(event):
            ...     process(event)
            >>> engine.subscribe().subscribe(on_change)
        """
        if self.transaction_manager is None:
            raise RuntimeError("Cannot subscribe: transaction_manager not configured")

        return Subscriber(
            transaction_manager=self.transaction_manager,
            since_tx_id=since_tx_id,
            tables=tables,
            branch=branch,
            poll_interval=poll_interval,
        )
```

---

## Part 5: Type Stubs

**File:** `python/udr.pyi` (additions)

```python
class PyTableChange:
    """A single table change within a commit."""
    table_name: str
    old_version: Optional[int]
    new_version: int
    chunk_hashes: List[str]

class PyChangelogEntry:
    """Entry in the changelog representing a committed transaction."""
    tx_id: int
    epoch_id: int
    committed_at: int
    branch: str
    changes: List[PyTableChange]

    def changed_tables(self) -> List[str]: ...

# Add to PyTransactionManager:
class PyTransactionManager:
    # ... existing methods ...

    def get_changelog(
        self,
        since_tx_id: Optional[int] = None,
        since_timestamp: Optional[int] = None,
        tables: Optional[List[str]] = None,
        branch: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> List[PyChangelogEntry]: ...

    def latest_tx_id(self) -> Optional[int]: ...
```

---

## Part 6: Implementation Phases

### Phase 6.0: Core Changelog API (Rust) ✅ COMPLETE

**Goal:** Expose transaction log as queryable changelog

**Files created:**
1. ✅ `udr_core/src/changelog/mod.rs` - Module exports
2. ✅ `udr_core/src/changelog/entry.rs` - ChangelogEntry, TableChange
3. ✅ `udr_core/src/changelog/query.rs` - ChangelogQuery builder
4. ✅ `udr_core/src/changelog/tests.rs` - Comprehensive tests

**Files modified:**
1. ✅ `udr_core/src/transaction/log.rs` - Added list_committed_transactions(), latest_committed_tx_id(), list_committed_since()
2. ✅ `udr_core/src/transaction/manager.rs` - Added get_changelog(), latest_tx_id()
3. ✅ `udr_core/src/lib.rs` - Export changelog module

**Tests:** 17 new Rust tests (127 total)
- List committed transactions
- Query filtering (tx_id, timestamp, tables, branch)
- ChangelogEntry creation from TransactionRecord
- Latest tx_id tracking

### Phase 6.1: Python Bindings ✅ COMPLETE

**Goal:** Python access to changelog

**Files modified:**
1. ✅ `udr_python/src/lib.rs` - Added PyChangelogEntry, PyTableChange, get_changelog(), latest_tx_id()
2. ✅ `python/udr.pyi` - Added type stubs

**Tests:** Python bindings verified (131 tests passing)
- get_changelog() with all filter options
- latest_tx_id() method
- PyChangelogEntry helper methods (changed_tables, contains_table, get_change, change_count)
- PyTableChange.is_new_table() method

### Phase 6.2: Subscriber API (Python)

**Goal:** Streaming interface for changelog

**Files to create:**
1. `python/udr_query/subscriber.py` - Subscriber class, ChangeEvent

**Files to modify:**
1. `python/udr_query/__init__.py` - Export Subscriber, ChangeEvent
2. `python/udr_query/engine.py` - Add get_changes(), subscribe()

**Estimated tests:** 15-20 Python tests
- Iterator interface
- Callback interface
- Background thread
- Filtering
- Graceful shutdown

### Phase 6.3: Integration & Demo

**Goal:** End-to-end validation

**Files to create:**
1. `tests/test_changelog.py` - Comprehensive tests
2. `examples/changelog_demo.py` - Interactive demo

**Estimated tests:** 10-15 integration tests

---

## Part 7: Test Strategy

### Rust Tests

```rust
#[cfg(test)]
mod tests {
    // ChangelogEntry
    #[test] fn test_changelog_entry_from_transaction()
    #[test] fn test_changed_tables()

    // ChangelogQuery
    #[test] fn test_query_since_tx_id()
    #[test] fn test_query_since_timestamp()
    #[test] fn test_query_filter_tables()
    #[test] fn test_query_filter_branch()
    #[test] fn test_query_with_limit()

    // ChangelogManager
    #[test] fn test_empty_changelog()
    #[test] fn test_query_all()
    #[test] fn test_query_partial()
    #[test] fn test_latest_tx_id()

    // TransactionLog extensions
    #[test] fn test_list_committed_transactions()
    #[test] fn test_list_excludes_aborted()
    #[test] fn test_list_ordered_by_tx_id()
}
```

### Python Tests

```python
class TestChangelogBasics:
    def test_get_changelog_empty(self)
    def test_get_changelog_after_commits(self)
    def test_changelog_entry_structure(self)
    def test_filter_by_table(self)
    def test_filter_by_branch(self)
    def test_latest_tx_id(self)

class TestSubscriber:
    def test_poll_returns_events(self)
    def test_iterator_interface(self)
    def test_callback_interface(self)
    def test_background_thread(self)
    def test_filter_tables(self)
    def test_stop_background(self)

class TestQueryEngineIntegration:
    def test_get_changes(self)
    def test_subscribe_returns_subscriber(self)
    def test_unified_batch_stream(self)  # Key demo test
```

---

## Part 8: Success Criteria

### Must Have
- [x] ChangelogEntry struct with table changes
- [x] Query changelog since tx_id/timestamp
- [x] Filter by tables and branch
- [x] Python bindings for changelog
- [ ] Subscriber with polling
- [ ] `engine.get_changes()` method
- [ ] `engine.subscribe()` method

### Should Have
- [ ] Background subscriber thread
- [ ] Callback interface
- [ ] ChangeEvent helper class
- [x] Comprehensive tests (17 Rust tests, Python bindings verified)

### Nice to Have (Future)
- [ ] Push notifications (WebSocket/gRPC)
- [ ] Persistent cursor (resume after restart)
- [ ] Backpressure handling
- [ ] Dead letter queue for failed callbacks

---

## Part 9: Example Usage

### Batch/Stream Unified Demo

```python
import udr
from udr_query import QueryEngine
import pandas as pd
import time

# Initialize
store = udr.PyChunkStore("./data/chunks")
catalog = udr.PyCatalog("./data/catalog")
branches = udr.PyBranchManager("./data/branches")
tx_manager = udr.PyTransactionManager(
    "./data/transactions", "./data/catalog", "./data/branches",
    auto_recover=True
)
engine = QueryEngine(store, catalog, branch_manager=branches, transaction_manager=tx_manager)

# === BATCH: What is the current state? ===
result = engine.query("SELECT * FROM users")
print(f"Current users: {result.row_count}")

# === STREAM: What changed since tx 100? ===

# Option 1: One-time batch query of changes
changes = engine.get_changes(since_tx_id=100)
for entry in changes:
    for change in entry["changes"]:
        print(f"Table {change['table_name']}: v{change['old_version']} -> v{change['new_version']}")

# Option 2: Continuous streaming
def process_change(event):
    print(f"[{event.committed_at}] {event.table_name}: v{event.old_version} -> v{event.new_version}")

subscriber = engine.subscribe(tables=["users"], since_tx_id=100)

# Background processing
subscriber.start_background(process_change)

# Simulate writes in another thread/process
with engine.transaction() as tx:
    tx.write_table("users", pd.DataFrame({"id": [4], "name": ["David"]}))

time.sleep(2)  # Let subscriber process

subscriber.stop()
```

---

## Appendix: Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Polling vs Push | Polling first | Simpler, no dependencies, push can be added later |
| Cursor tracking | In-memory | Persistent cursor is future enhancement |
| Thread model | Optional background | Flexibility - user chooses sync/async |
| Entry granularity | Per-transaction | Matches commit semantics; per-row is future |
| Filter approach | Server-side | Reduces data transfer; client can filter more |

---

*Plan version: 1.0*
*Created: January 2026*
