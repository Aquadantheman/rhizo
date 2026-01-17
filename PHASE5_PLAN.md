# Phase 5: Cross-Table ACID Transactions - Implementation Plan

## Overview

Phase 5 implements cross-table ACID transactions with snapshot isolation, building on our content-addressable storage, versioned catalog, and branching infrastructure. The design prioritizes:

1. **Robustness** - Correct recovery from any failure state
2. **Scalability** - Same code path from single-node to distributed
3. **Adoption** - Simple API, backward compatible
4. **Future-proof** - Data formats designed for 10+ year stability

## Architecture Summary

```
┌─────────────────────────────────────────────────────────────────┐
│                     QueryEngine (Python)                         │
│         engine.transaction() context manager                     │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                   TransactionManager (Rust)                      │
│   • Assigns transaction IDs                                      │
│   • Manages epochs                                               │
│   • Coordinates commits                                          │
│   • Detects conflicts                                           │
└─────────────────────────────────────────────────────────────────┘
                              │
              ┌───────────────┼───────────────┐
              ▼               ▼               ▼
┌──────────────────┐ ┌──────────────┐ ┌──────────────────┐
│  TransactionLog  │ │  FileCatalog │ │  BranchManager   │
│  (epochs, WAL)   │ │  (versions)  │ │  (branch heads)  │
└──────────────────┘ └──────────────┘ └──────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                        ChunkStore                                │
│              (content-addressable storage)                       │
└─────────────────────────────────────────────────────────────────┘
```

---

## Part 1: Data Structures

### 1.1 Core Transaction Types

**File:** `udr_core/src/transaction/types.rs`

```rust
use serde::{Deserialize, Serialize};
use std::collections::HashMap;

/// Unique transaction identifier
pub type TxId = u64;

/// Unique epoch identifier
pub type EpochId = u64;

/// Transaction status - designed for both single-node and distributed
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub enum TransactionStatus {
    /// Transaction is active and accepting operations
    Active,

    /// Transaction is preparing to commit (writes buffered)
    Preparing,

    /// Transaction has been committed successfully
    Committed,

    /// Transaction was aborted (conflict or explicit rollback)
    Aborted { reason: String },

    // === Future: Distributed transaction states ===
    // PreparingDistributed { participants: Vec<String> },
    // AwaitingVotes { received: Vec<String>, pending: Vec<String> },
}

/// How granular is our conflict detection for a write
#[derive(Debug, Clone, Serialize, Deserialize)]
pub enum WriteGranularity {
    /// Entire table (Phase 5.0)
    WholeTable,

    /// Specific partitions (Phase 5.5)
    Partitions(Vec<String>),

    /// Specific row keys (Phase 5.x)
    Keys {
        key_columns: Vec<String>,
        affected_keys: Vec<serde_json::Value>,
    },
}

impl Default for WriteGranularity {
    fn default() -> Self {
        WriteGranularity::WholeTable
    }
}

/// A single table write within a transaction
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TableWrite {
    /// Name of the table being written
    pub table_name: String,

    /// New version number for this table
    pub new_version: u64,

    /// Chunk hashes for the new version
    pub chunk_hashes: Vec<String>,

    /// Optional schema hash
    pub schema_hash: Option<String>,

    /// Granularity of this write (for conflict detection)
    pub granularity: WriteGranularity,

    /// Branch this write targets (None = current branch)
    pub branch: Option<String>,
}

/// Complete transaction record - the source of truth
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TransactionRecord {
    // === Identity ===
    /// Unique transaction ID (monotonically increasing)
    pub tx_id: TxId,

    /// Epoch this transaction belongs to
    pub epoch_id: EpochId,

    // === Timing ===
    /// Unix timestamp when transaction started
    pub started_at: i64,

    /// Unix timestamp when committed (None if not yet committed)
    pub committed_at: Option<i64>,

    // === Read Set (Snapshot) ===
    /// Tables read and their versions at transaction start
    /// Used for conflict detection and debugging
    pub read_snapshot: HashMap<String, u64>,

    // === Write Set ===
    /// All writes this transaction will perform
    pub writes: Vec<TableWrite>,

    // === Status ===
    /// Current transaction status
    pub status: TransactionStatus,

    // === Branch Context ===
    /// Branch this transaction operates on
    pub branch: String,

    // === Metadata ===
    /// User-provided metadata
    pub metadata: HashMap<String, String>,

    // === Extensibility ===
    /// Schema version for forward compatibility
    pub format_version: u32,

    /// Reserved for future extensions
    pub extensions: Option<serde_json::Value>,
}

impl TransactionRecord {
    /// Current format version
    pub const CURRENT_FORMAT_VERSION: u32 = 1;

    /// Create a new transaction record
    pub fn new(tx_id: TxId, epoch_id: EpochId, branch: String) -> Self {
        let now = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_secs() as i64;

        Self {
            tx_id,
            epoch_id,
            started_at: now,
            committed_at: None,
            read_snapshot: HashMap::new(),
            writes: Vec::new(),
            status: TransactionStatus::Active,
            branch,
            metadata: HashMap::new(),
            format_version: Self::CURRENT_FORMAT_VERSION,
            extensions: None,
        }
    }

    /// Check if transaction is still active
    pub fn is_active(&self) -> bool {
        matches!(self.status, TransactionStatus::Active)
    }

    /// Check if transaction is committed
    pub fn is_committed(&self) -> bool {
        matches!(self.status, TransactionStatus::Committed)
    }

    /// Get list of tables being written
    pub fn written_tables(&self) -> Vec<&str> {
        self.writes.iter().map(|w| w.table_name.as_str()).collect()
    }
}
```

### 1.2 Epoch Types

**File:** `udr_core/src/transaction/epoch.rs`

```rust
use serde::{Deserialize, Serialize};
use super::types::{EpochId, TxId, TransactionRecord};

/// Configuration for epoch behavior
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct EpochConfig {
    /// Duration of each epoch in milliseconds
    /// Set to 0 for immediate mode (each tx is own epoch)
    pub duration_ms: u64,

    /// Maximum transactions per epoch before forcing new epoch
    pub max_transactions: u64,

    /// Whether to batch transactions within epochs
    pub batching_enabled: bool,
}

impl Default for EpochConfig {
    fn default() -> Self {
        Self {
            duration_ms: 100,       // 100ms epochs
            max_transactions: 1000, // Cap for memory
            batching_enabled: true,
        }
    }
}

impl EpochConfig {
    /// Configuration for single-node/POC mode
    pub fn single_node() -> Self {
        Self {
            duration_ms: 0,          // Immediate commit
            max_transactions: 1,     // One tx per epoch
            batching_enabled: false,
        }
    }

    /// Configuration for high-throughput mode
    pub fn high_throughput() -> Self {
        Self {
            duration_ms: 50,         // 50ms epochs
            max_transactions: 10000, // High batch size
            batching_enabled: true,
        }
    }
}

/// Status of an epoch
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub enum EpochStatus {
    /// Epoch is currently accepting transactions
    Active,

    /// Epoch is closed, transactions are being committed
    Committing,

    /// All transactions in epoch have been committed
    Committed,

    /// Epoch was rolled back (recovery scenario)
    RolledBack,
}

/// Metadata about an epoch
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct EpochMetadata {
    /// Unique epoch identifier
    pub epoch_id: EpochId,

    /// Unix timestamp when epoch started
    pub started_at: i64,

    /// Unix timestamp when epoch ended (None if still active)
    pub ended_at: Option<i64>,

    /// Status of the epoch
    pub status: EpochStatus,

    /// Transaction IDs in this epoch (ordered)
    pub transactions: Vec<TxId>,

    /// First transaction ID in this epoch
    pub first_tx_id: Option<TxId>,

    /// Last transaction ID in this epoch
    pub last_tx_id: Option<TxId>,
}

impl EpochMetadata {
    pub fn new(epoch_id: EpochId) -> Self {
        let now = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_secs() as i64;

        Self {
            epoch_id,
            started_at: now,
            ended_at: None,
            status: EpochStatus::Active,
            transactions: Vec::new(),
            first_tx_id: None,
            last_tx_id: None,
        }
    }

    pub fn add_transaction(&mut self, tx_id: TxId) {
        if self.first_tx_id.is_none() {
            self.first_tx_id = Some(tx_id);
        }
        self.last_tx_id = Some(tx_id);
        self.transactions.push(tx_id);
    }
}
```

### 1.3 Error Types

**File:** `udr_core/src/transaction/error.rs`

```rust
use thiserror::Error;
use super::types::TxId;

#[derive(Error, Debug)]
pub enum TransactionError {
    #[error("IO error: {0}")]
    Io(#[from] std::io::Error),

    #[error("JSON error: {0}")]
    Json(#[from] serde_json::Error),

    #[error("Transaction not found: {0}")]
    TransactionNotFound(TxId),

    #[error("Transaction {0} is not active")]
    TransactionNotActive(TxId),

    #[error("Transaction {0} already committed")]
    AlreadyCommitted(TxId),

    #[error("Transaction {0} already aborted")]
    AlreadyAborted(TxId),

    #[error("Write-write conflict on tables: {0:?}")]
    WriteConflict(Vec<String>),

    #[error("Snapshot conflict: table {table} was modified (read v{read_version}, now v{current_version})")]
    SnapshotConflict {
        table: String,
        read_version: u64,
        current_version: u64,
    },

    #[error("Epoch {0} is not active")]
    EpochNotActive(u64),

    #[error("Epoch {0} not found")]
    EpochNotFound(u64),

    #[error("Invalid transaction state: expected {expected}, got {actual}")]
    InvalidState {
        expected: String,
        actual: String,
    },

    #[error("Catalog error: {0}")]
    CatalogError(String),

    #[error("Branch error: {0}")]
    BranchError(String),

    #[error("Recovery error: {0}")]
    RecoveryError(String),
}
```

---

## Part 2: Storage Layout

### 2.1 Directory Structure

```
{base_path}/
├── chunks/                         # Existing: content-addressable storage
│   └── ab/cd/abcdef...
│
├── catalog/                        # Existing: table versions
│   └── {table}/
│       ├── 1.json
│       ├── 2.json
│       └── latest
│
├── branches/                       # Existing: branch heads
│   └── _branches/
│       ├── main.json
│       └── feature__test.json
│
└── transactions/                   # NEW: transaction system
    ├── _config.json                # Epoch configuration
    ├── _sequence                   # Current tx_id counter (atomic)
    ├── _epoch_sequence             # Current epoch_id counter (atomic)
    │
    ├── epochs/                     # Epoch-organized transaction logs
    │   ├── 000001/                 # Epoch directory
    │   │   ├── _meta.json          # EpochMetadata
    │   │   ├── _committed          # Marker file (empty = committed)
    │   │   ├── tx_000001.json      # TransactionRecord
    │   │   └── tx_000002.json
    │   │
    │   ├── 000002/
    │   │   └── ...
    │   │
    │   └── _current -> 000047      # Symlink to active epoch
    │
    └── archive/                    # Compacted old epochs (future)
        └── snapshot_000001_000100.json
```

### 2.2 File Formats

**_config.json:**
```json
{
    "format_version": 1,
    "epoch_config": {
        "duration_ms": 100,
        "max_transactions": 1000,
        "batching_enabled": true
    },
    "created_at": 1705420800
}
```

**_sequence (plain text):**
```
42
```

**epochs/000001/_meta.json:**
```json
{
    "epoch_id": 1,
    "started_at": 1705420800,
    "ended_at": 1705420801,
    "status": "Committed",
    "transactions": [1, 2, 3],
    "first_tx_id": 1,
    "last_tx_id": 3
}
```

**epochs/000001/tx_000001.json:**
```json
{
    "tx_id": 1,
    "epoch_id": 1,
    "started_at": 1705420800,
    "committed_at": 1705420801,
    "read_snapshot": {
        "users": 5,
        "orders": 3
    },
    "writes": [
        {
            "table_name": "users",
            "new_version": 6,
            "chunk_hashes": ["abc123", "def456"],
            "schema_hash": null,
            "granularity": "WholeTable",
            "branch": null
        },
        {
            "table_name": "audit",
            "new_version": 1,
            "chunk_hashes": ["xyz789"],
            "schema_hash": null,
            "granularity": "WholeTable",
            "branch": null
        }
    ],
    "status": "Committed",
    "branch": "main",
    "metadata": {},
    "format_version": 1,
    "extensions": null
}
```

---

## Part 3: Transaction Manager

### 3.1 Core Implementation

**File:** `udr_core/src/transaction/manager.rs`

```rust
use std::collections::HashMap;
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::{Arc, RwLock};

use super::types::*;
use super::epoch::*;
use super::error::TransactionError;
use super::log::TransactionLog;
use super::conflict::ConflictDetector;
use crate::catalog::FileCatalog;
use crate::branch::BranchManager;

/// Manages cross-table ACID transactions
pub struct TransactionManager {
    /// Base path for transaction storage
    base_path: PathBuf,

    /// Transaction log (persists transactions and epochs)
    log: TransactionLog,

    /// Current epoch configuration
    config: EpochConfig,

    /// Active transactions (in-memory for fast access)
    active_transactions: RwLock<HashMap<TxId, TransactionRecord>>,

    /// Conflict detector (pluggable strategy)
    conflict_detector: Box<dyn ConflictDetector + Send + Sync>,

    /// Reference to catalog (for version resolution)
    catalog: Arc<FileCatalog>,

    /// Reference to branch manager (optional)
    branch_manager: Option<Arc<BranchManager>>,
}

impl TransactionManager {
    /// Create a new TransactionManager
    pub fn new(
        base_path: impl AsRef<Path>,
        catalog: Arc<FileCatalog>,
        branch_manager: Option<Arc<BranchManager>>,
    ) -> Result<Self, TransactionError> {
        let base_path = base_path.as_ref().to_path_buf();
        let tx_path = base_path.join("transactions");
        fs::create_dir_all(&tx_path)?;

        let log = TransactionLog::new(&tx_path)?;
        let config = log.load_config()?.unwrap_or_default();

        Ok(Self {
            base_path: tx_path,
            log,
            config,
            active_transactions: RwLock::new(HashMap::new()),
            conflict_detector: Box::new(TableLevelConflictDetector),
            catalog,
            branch_manager,
        })
    }

    /// Begin a new transaction
    pub fn begin(&self, branch: Option<&str>) -> Result<TxId, TransactionError> {
        // Get next transaction ID
        let tx_id = self.log.next_tx_id()?;

        // Get current epoch (or create new one)
        let epoch_id = self.log.current_epoch_id()?;

        // Determine branch
        let branch_name = match branch {
            Some(b) => b.to_string(),
            None => self.default_branch()?,
        };

        // Create transaction record
        let mut tx = TransactionRecord::new(tx_id, epoch_id, branch_name.clone());

        // Capture read snapshot (current versions of all tables on branch)
        tx.read_snapshot = self.capture_snapshot(&branch_name)?;

        // Add to active transactions
        {
            let mut active = self.active_transactions.write().unwrap();
            active.insert(tx_id, tx.clone());
        }

        // Persist to log
        self.log.write_transaction(&tx)?;

        Ok(tx_id)
    }

    /// Add a read to the transaction (for conflict detection)
    pub fn record_read(
        &self,
        tx_id: TxId,
        table_name: &str,
        version: u64,
    ) -> Result<(), TransactionError> {
        let mut active = self.active_transactions.write().unwrap();
        let tx = active.get_mut(&tx_id)
            .ok_or(TransactionError::TransactionNotFound(tx_id))?;

        if !tx.is_active() {
            return Err(TransactionError::TransactionNotActive(tx_id));
        }

        tx.read_snapshot.insert(table_name.to_string(), version);
        Ok(())
    }

    /// Add a write to the transaction
    pub fn add_write(
        &self,
        tx_id: TxId,
        write: TableWrite,
    ) -> Result<(), TransactionError> {
        let mut active = self.active_transactions.write().unwrap();
        let tx = active.get_mut(&tx_id)
            .ok_or(TransactionError::TransactionNotFound(tx_id))?;

        if !tx.is_active() {
            return Err(TransactionError::TransactionNotActive(tx_id));
        }

        tx.writes.push(write);
        Ok(())
    }

    /// Commit a transaction
    pub fn commit(&self, tx_id: TxId) -> Result<(), TransactionError> {
        // Get transaction from active set
        let tx = {
            let active = self.active_transactions.read().unwrap();
            active.get(&tx_id)
                .ok_or(TransactionError::TransactionNotFound(tx_id))?
                .clone()
        };

        if !tx.is_active() {
            return Err(TransactionError::TransactionNotActive(tx_id));
        }

        // Check for conflicts with other committed transactions
        self.check_conflicts(&tx)?;

        // Validate snapshot (tables we read haven't changed)
        self.validate_snapshot(&tx)?;

        // Prepare commit (update status)
        let mut committed_tx = tx.clone();
        committed_tx.status = TransactionStatus::Committed;
        committed_tx.committed_at = Some(
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_secs() as i64
        );

        // Apply writes to catalog
        self.apply_writes(&committed_tx)?;

        // Update branch heads (if branch manager configured)
        self.update_branch_heads(&committed_tx)?;

        // Persist committed status
        self.log.write_transaction(&committed_tx)?;

        // Remove from active set
        {
            let mut active = self.active_transactions.write().unwrap();
            active.remove(&tx_id);
        }

        Ok(())
    }

    /// Abort a transaction
    pub fn abort(&self, tx_id: TxId, reason: &str) -> Result<(), TransactionError> {
        let mut active = self.active_transactions.write().unwrap();
        let tx = active.get_mut(&tx_id)
            .ok_or(TransactionError::TransactionNotFound(tx_id))?;

        if !tx.is_active() {
            return Err(TransactionError::TransactionNotActive(tx_id));
        }

        tx.status = TransactionStatus::Aborted {
            reason: reason.to_string()
        };

        // Persist aborted status
        self.log.write_transaction(tx)?;

        // Remove from active set
        active.remove(&tx_id);

        Ok(())
    }

    /// Get a transaction by ID
    pub fn get_transaction(&self, tx_id: TxId) -> Result<TransactionRecord, TransactionError> {
        // Check active first
        {
            let active = self.active_transactions.read().unwrap();
            if let Some(tx) = active.get(&tx_id) {
                return Ok(tx.clone());
            }
        }

        // Fall back to log
        self.log.read_transaction(tx_id)
    }

    // === Private helpers ===

    fn default_branch(&self) -> Result<String, TransactionError> {
        if let Some(ref bm) = self.branch_manager {
            bm.get_default()
                .map_err(|e| TransactionError::BranchError(e.to_string()))?
                .ok_or_else(|| TransactionError::BranchError("No default branch".to_string()))
        } else {
            Ok("main".to_string())
        }
    }

    fn capture_snapshot(&self, branch: &str) -> Result<HashMap<String, u64>, TransactionError> {
        let mut snapshot = HashMap::new();

        if let Some(ref bm) = self.branch_manager {
            // Use branch heads
            let branch_data = bm.get(branch)
                .map_err(|e| TransactionError::BranchError(e.to_string()))?;
            snapshot = branch_data.head;
        } else {
            // Use catalog latest versions
            let tables = self.catalog.list_tables()
                .map_err(|e| TransactionError::CatalogError(e.to_string()))?;
            for table in tables {
                let version = self.catalog.get_version(&table, None)
                    .map_err(|e| TransactionError::CatalogError(e.to_string()))?;
                snapshot.insert(table, version.version);
            }
        }

        Ok(snapshot)
    }

    fn check_conflicts(&self, tx: &TransactionRecord) -> Result<(), TransactionError> {
        let active = self.active_transactions.read().unwrap();

        for (_, other_tx) in active.iter() {
            if other_tx.tx_id == tx.tx_id {
                continue;
            }

            if let Some(conflict) = self.conflict_detector.detect(tx, other_tx) {
                return Err(TransactionError::WriteConflict(conflict.tables));
            }
        }

        Ok(())
    }

    fn validate_snapshot(&self, tx: &TransactionRecord) -> Result<(), TransactionError> {
        for (table, read_version) in &tx.read_snapshot {
            let current_version = if let Some(ref bm) = self.branch_manager {
                bm.get_table_version(&tx.branch, table)
                    .map_err(|e| TransactionError::BranchError(e.to_string()))?
            } else {
                self.catalog.get_version(table, None)
                    .map(|v| Some(v.version))
                    .unwrap_or(None)
            };

            if let Some(current) = current_version {
                if current != *read_version {
                    return Err(TransactionError::SnapshotConflict {
                        table: table.clone(),
                        read_version: *read_version,
                        current_version: current,
                    });
                }
            }
        }

        Ok(())
    }

    fn apply_writes(&self, tx: &TransactionRecord) -> Result<(), TransactionError> {
        use crate::catalog::TableVersion;

        for write in &tx.writes {
            let table_version = TableVersion::new(
                &write.table_name,
                write.new_version,
                write.chunk_hashes.clone(),
            );

            self.catalog.commit(table_version)
                .map_err(|e| TransactionError::CatalogError(e.to_string()))?;
        }

        Ok(())
    }

    fn update_branch_heads(&self, tx: &TransactionRecord) -> Result<(), TransactionError> {
        if let Some(ref bm) = self.branch_manager {
            for write in &tx.writes {
                let branch = write.branch.as_ref().unwrap_or(&tx.branch);
                bm.update_head(branch, &write.table_name, write.new_version)
                    .map_err(|e| TransactionError::BranchError(e.to_string()))?;
            }
        }

        Ok(())
    }
}
```

### 3.2 Conflict Detection

**File:** `udr_core/src/transaction/conflict.rs`

```rust
use super::types::TransactionRecord;

/// Represents a detected conflict
pub struct Conflict {
    pub tables: Vec<String>,
    pub tx1_id: u64,
    pub tx2_id: u64,
}

/// Trait for conflict detection strategies
pub trait ConflictDetector: Send + Sync {
    fn detect(&self, tx1: &TransactionRecord, tx2: &TransactionRecord) -> Option<Conflict>;
}

/// Table-level conflict detection (Phase 5.0)
/// Two transactions conflict if they write to the same table
pub struct TableLevelConflictDetector;

impl ConflictDetector for TableLevelConflictDetector {
    fn detect(&self, tx1: &TransactionRecord, tx2: &TransactionRecord) -> Option<Conflict> {
        use std::collections::HashSet;

        let tables1: HashSet<_> = tx1.writes.iter()
            .map(|w| w.table_name.as_str())
            .collect();
        let tables2: HashSet<_> = tx2.writes.iter()
            .map(|w| w.table_name.as_str())
            .collect();

        let conflicts: Vec<String> = tables1
            .intersection(&tables2)
            .map(|s| s.to_string())
            .collect();

        if conflicts.is_empty() {
            None
        } else {
            Some(Conflict {
                tables: conflicts,
                tx1_id: tx1.tx_id,
                tx2_id: tx2.tx_id,
            })
        }
    }
}

/// Row-level conflict detection (Phase 5.x - future)
pub struct RowLevelConflictDetector;

impl ConflictDetector for RowLevelConflictDetector {
    fn detect(&self, tx1: &TransactionRecord, tx2: &TransactionRecord) -> Option<Conflict> {
        // TODO: Implement row-level conflict detection
        // For now, fall back to table-level
        TableLevelConflictDetector.detect(tx1, tx2)
    }
}
```

### 3.3 Transaction Log

**File:** `udr_core/src/transaction/log.rs`

```rust
use std::fs;
use std::path::{Path, PathBuf};
use super::types::*;
use super::epoch::*;
use super::error::TransactionError;

const EPOCHS_DIR: &str = "epochs";
const CONFIG_FILE: &str = "_config.json";
const SEQUENCE_FILE: &str = "_sequence";
const EPOCH_SEQUENCE_FILE: &str = "_epoch_sequence";

/// Persistent transaction log
pub struct TransactionLog {
    base_path: PathBuf,
}

impl TransactionLog {
    pub fn new(base_path: impl AsRef<Path>) -> Result<Self, TransactionError> {
        let base_path = base_path.as_ref().to_path_buf();
        let epochs_dir = base_path.join(EPOCHS_DIR);
        fs::create_dir_all(&epochs_dir)?;

        Ok(Self { base_path })
    }

    /// Get next transaction ID (atomic increment)
    pub fn next_tx_id(&self) -> Result<TxId, TransactionError> {
        let path = self.base_path.join(SEQUENCE_FILE);

        let current = if path.exists() {
            fs::read_to_string(&path)?
                .trim()
                .parse::<u64>()
                .unwrap_or(0)
        } else {
            0
        };

        let next = current + 1;

        // Atomic write
        let temp_path = path.with_extension("tmp");
        fs::write(&temp_path, next.to_string())?;
        fs::rename(&temp_path, &path)?;

        Ok(next)
    }

    /// Get current epoch ID
    pub fn current_epoch_id(&self) -> Result<EpochId, TransactionError> {
        let path = self.base_path.join(EPOCH_SEQUENCE_FILE);

        if path.exists() {
            Ok(fs::read_to_string(&path)?
                .trim()
                .parse::<u64>()
                .unwrap_or(1))
        } else {
            // Create first epoch
            self.create_epoch(1)?;
            Ok(1)
        }
    }

    /// Create a new epoch
    pub fn create_epoch(&self, epoch_id: EpochId) -> Result<(), TransactionError> {
        let epoch_dir = self.epoch_dir(epoch_id);
        fs::create_dir_all(&epoch_dir)?;

        let meta = EpochMetadata::new(epoch_id);
        let meta_path = epoch_dir.join("_meta.json");
        let json = serde_json::to_string_pretty(&meta)?;
        fs::write(&meta_path, json)?;

        // Update sequence
        let seq_path = self.base_path.join(EPOCH_SEQUENCE_FILE);
        let temp_path = seq_path.with_extension("tmp");
        fs::write(&temp_path, epoch_id.to_string())?;
        fs::rename(&temp_path, &seq_path)?;

        Ok(())
    }

    /// Write transaction record to current epoch
    pub fn write_transaction(&self, tx: &TransactionRecord) -> Result<(), TransactionError> {
        let epoch_dir = self.epoch_dir(tx.epoch_id);
        fs::create_dir_all(&epoch_dir)?;

        let tx_path = epoch_dir.join(format!("tx_{:06}.json", tx.tx_id));
        let temp_path = tx_path.with_extension("json.tmp");

        let json = serde_json::to_string_pretty(tx)?;
        fs::write(&temp_path, &json)?;
        fs::rename(&temp_path, &tx_path)?;

        Ok(())
    }

    /// Read transaction record
    pub fn read_transaction(&self, tx_id: TxId) -> Result<TransactionRecord, TransactionError> {
        // Search through epochs (most recent first)
        let epochs = self.list_epochs()?;

        for epoch_id in epochs.into_iter().rev() {
            let tx_path = self.epoch_dir(epoch_id).join(format!("tx_{:06}.json", tx_id));
            if tx_path.exists() {
                let json = fs::read_to_string(&tx_path)?;
                let tx: TransactionRecord = serde_json::from_str(&json)?;
                return Ok(tx);
            }
        }

        Err(TransactionError::TransactionNotFound(tx_id))
    }

    /// Load configuration
    pub fn load_config(&self) -> Result<Option<EpochConfig>, TransactionError> {
        let path = self.base_path.join(CONFIG_FILE);
        if !path.exists() {
            return Ok(None);
        }

        let json = fs::read_to_string(&path)?;
        let config: EpochConfig = serde_json::from_str(&json)?;
        Ok(Some(config))
    }

    /// Save configuration
    pub fn save_config(&self, config: &EpochConfig) -> Result<(), TransactionError> {
        let path = self.base_path.join(CONFIG_FILE);
        let temp_path = path.with_extension("tmp");

        let json = serde_json::to_string_pretty(config)?;
        fs::write(&temp_path, &json)?;
        fs::rename(&temp_path, &path)?;

        Ok(())
    }

    /// List all epoch IDs
    pub fn list_epochs(&self) -> Result<Vec<EpochId>, TransactionError> {
        let epochs_dir = self.base_path.join(EPOCHS_DIR);
        if !epochs_dir.exists() {
            return Ok(Vec::new());
        }

        let mut epochs = Vec::new();
        for entry in fs::read_dir(&epochs_dir)? {
            let entry = entry?;
            if entry.file_type()?.is_dir() {
                if let Some(name) = entry.file_name().to_str() {
                    if let Ok(epoch_id) = name.parse::<u64>() {
                        epochs.push(epoch_id);
                    }
                }
            }
        }

        epochs.sort();
        Ok(epochs)
    }

    // === Private helpers ===

    fn epoch_dir(&self, epoch_id: EpochId) -> PathBuf {
        self.base_path.join(EPOCHS_DIR).join(format!("{:06}", epoch_id))
    }
}
```

---

## Part 4: Recovery

### 4.1 Recovery Manager

**File:** `udr_core/src/transaction/recovery.rs`

```rust
use super::types::*;
use super::epoch::*;
use super::log::TransactionLog;
use super::error::TransactionError;

/// Result of recovery process
#[derive(Debug)]
pub struct RecoveryReport {
    /// Last committed epoch found
    pub last_committed_epoch: Option<EpochId>,

    /// Transactions that were replayed (committed but effects not applied)
    pub replayed: Vec<TxId>,

    /// Transactions that were rolled back (pending at crash)
    pub rolled_back: Vec<TxId>,

    /// Transactions that were already aborted
    pub already_aborted: Vec<TxId>,

    /// Any errors encountered
    pub errors: Vec<String>,
}

impl RecoveryReport {
    pub fn new() -> Self {
        Self {
            last_committed_epoch: None,
            replayed: Vec::new(),
            rolled_back: Vec::new(),
            already_aborted: Vec::new(),
            errors: Vec::new(),
        }
    }

    pub fn is_clean(&self) -> bool {
        self.errors.is_empty()
    }
}

/// Handles crash recovery
pub struct RecoveryManager<'a> {
    log: &'a TransactionLog,
}

impl<'a> RecoveryManager<'a> {
    pub fn new(log: &'a TransactionLog) -> Self {
        Self { log }
    }

    /// Perform recovery after crash/restart
    pub fn recover(&self) -> Result<RecoveryReport, TransactionError> {
        let mut report = RecoveryReport::new();

        // 1. Find all epochs
        let epochs = self.log.list_epochs()?;

        if epochs.is_empty() {
            return Ok(report);
        }

        // 2. Find last committed epoch
        for epoch_id in epochs.iter().rev() {
            // Check if epoch has commit marker
            // TODO: Implement epoch commit marker check
            report.last_committed_epoch = Some(*epoch_id);
            break;
        }

        // 3. Scan current epoch for pending transactions
        if let Some(current_epoch) = epochs.last() {
            let epoch_dir = format!("epochs/{:06}", current_epoch);
            // TODO: Scan for pending transactions
        }

        // 4. For each pending transaction, decide: rollback or replay
        // - Pending -> rollback
        // - Prepared -> check coordinator (single-node: rollback)
        // - Committed (effects not applied) -> replay

        Ok(report)
    }
}
```

---

## Part 5: Python Bindings

### 5.1 Python Transaction Classes

**File:** `udr_python/src/transaction.rs` (additions to lib.rs)

```rust
use pyo3::prelude::*;
use pyo3::exceptions::{PyIOError, PyValueError, PyRuntimeError};
use std::sync::Arc;
use udr_core::transaction::{
    TransactionManager, TransactionRecord, TableWrite, WriteGranularity,
    TransactionError,
};

/// Convert TransactionError to Python exception
fn tx_err_to_py(e: TransactionError) -> PyErr {
    match e {
        TransactionError::TransactionNotFound(id) => {
            PyValueError::new_err(format!("Transaction not found: {}", id))
        }
        TransactionError::WriteConflict(tables) => {
            PyValueError::new_err(format!("Write conflict on tables: {:?}", tables))
        }
        TransactionError::SnapshotConflict { table, read_version, current_version } => {
            PyValueError::new_err(format!(
                "Snapshot conflict: {} was v{}, now v{}",
                table, read_version, current_version
            ))
        }
        TransactionError::Io(e) => PyIOError::new_err(e.to_string()),
        _ => PyRuntimeError::new_err(e.to_string()),
    }
}

#[pyclass]
pub struct PyTransactionManager {
    inner: Arc<TransactionManager>,
}

#[pymethods]
impl PyTransactionManager {
    #[new]
    fn new(
        base_path: &str,
        catalog: &PyCatalog,
        branch_manager: Option<&PyBranchManager>,
    ) -> PyResult<Self> {
        let catalog_arc = Arc::new(catalog.inner.clone());
        let branch_arc = branch_manager.map(|bm| Arc::new(bm.inner.clone()));

        let inner = TransactionManager::new(base_path, catalog_arc, branch_arc)
            .map_err(tx_err_to_py)?;

        Ok(Self { inner: Arc::new(inner) })
    }

    /// Begin a new transaction
    fn begin(&self, branch: Option<&str>) -> PyResult<u64> {
        self.inner.begin(branch).map_err(tx_err_to_py)
    }

    /// Commit a transaction
    fn commit(&self, tx_id: u64) -> PyResult<()> {
        self.inner.commit(tx_id).map_err(tx_err_to_py)
    }

    /// Abort a transaction
    fn abort(&self, tx_id: u64, reason: &str) -> PyResult<()> {
        self.inner.abort(tx_id, reason).map_err(tx_err_to_py)
    }

    /// Get transaction info
    fn get_transaction(&self, tx_id: u64) -> PyResult<PyTransactionInfo> {
        let tx = self.inner.get_transaction(tx_id).map_err(tx_err_to_py)?;
        Ok(PyTransactionInfo::from(tx))
    }
}

#[pyclass]
pub struct PyTransactionInfo {
    #[pyo3(get)]
    pub tx_id: u64,
    #[pyo3(get)]
    pub epoch_id: u64,
    #[pyo3(get)]
    pub status: String,
    #[pyo3(get)]
    pub branch: String,
    #[pyo3(get)]
    pub started_at: i64,
    #[pyo3(get)]
    pub committed_at: Option<i64>,
}

impl From<TransactionRecord> for PyTransactionInfo {
    fn from(tx: TransactionRecord) -> Self {
        Self {
            tx_id: tx.tx_id,
            epoch_id: tx.epoch_id,
            status: format!("{:?}", tx.status),
            branch: tx.branch,
            started_at: tx.started_at,
            committed_at: tx.committed_at,
        }
    }
}

#[pyclass]
pub struct PyTransaction {
    manager: Arc<TransactionManager>,
    tx_id: u64,
    committed: bool,
    aborted: bool,
}

#[pymethods]
impl PyTransaction {
    /// Check if transaction is still active
    fn is_active(&self) -> bool {
        !self.committed && !self.aborted
    }

    /// Commit the transaction
    fn commit(&mut self) -> PyResult<()> {
        if self.committed {
            return Err(PyValueError::new_err("Transaction already committed"));
        }
        if self.aborted {
            return Err(PyValueError::new_err("Transaction already aborted"));
        }

        self.manager.commit(self.tx_id).map_err(tx_err_to_py)?;
        self.committed = true;
        Ok(())
    }

    /// Abort the transaction
    fn abort(&mut self, reason: Option<&str>) -> PyResult<()> {
        if self.committed {
            return Err(PyValueError::new_err("Transaction already committed"));
        }
        if self.aborted {
            return Ok(()); // Idempotent abort
        }

        self.manager.abort(self.tx_id, reason.unwrap_or("User requested"))
            .map_err(tx_err_to_py)?;
        self.aborted = true;
        Ok(())
    }

    /// Get transaction ID
    fn tx_id(&self) -> u64 {
        self.tx_id
    }
}
```

### 5.2 QueryEngine Integration

**File:** `python/udr_query/engine.py` (additions)

```python
from contextlib import contextmanager
from typing import Optional, Dict, Any, Generator
import pandas as pd
import pyarrow as pa

class TransactionalQueryEngine(QueryEngine):
    """QueryEngine with transaction support."""

    def __init__(
        self,
        store,
        catalog,
        branch_manager=None,
        transaction_manager=None,
        verify_integrity: bool = False,
    ):
        super().__init__(store, catalog, verify_integrity, branch_manager)
        self.transaction_manager = transaction_manager
        self._active_tx: Optional[int] = None
        self._tx_writes: Dict[str, Any] = {}  # Buffered writes

    @contextmanager
    def transaction(
        self,
        branch: Optional[str] = None,
    ) -> Generator["TransactionContext", None, None]:
        """
        Begin a transaction context.

        Usage:
            with engine.transaction() as tx:
                result = tx.query("SELECT * FROM users")
                tx.write_table("users", modified_df)
                tx.write_table("audit", audit_log)
            # Commits on successful exit, rolls back on exception

        Args:
            branch: Branch to operate on (default: current branch)

        Yields:
            TransactionContext for performing operations
        """
        if self.transaction_manager is None:
            raise RuntimeError("Transaction manager not configured")

        if self._active_tx is not None:
            raise RuntimeError("Nested transactions not supported")

        effective_branch = branch or self._current_branch
        tx_id = self.transaction_manager.begin(effective_branch)
        self._active_tx = tx_id
        self._tx_writes = {}

        ctx = TransactionContext(self, tx_id, effective_branch)

        try:
            yield ctx
            # Commit on successful exit
            ctx.commit()
        except Exception as e:
            # Rollback on exception
            ctx.abort(str(e))
            raise
        finally:
            self._active_tx = None
            self._tx_writes = {}


class TransactionContext:
    """Context for operations within a transaction."""

    def __init__(
        self,
        engine: TransactionalQueryEngine,
        tx_id: int,
        branch: str,
    ):
        self._engine = engine
        self._tx_id = tx_id
        self._branch = branch
        self._committed = False
        self._aborted = False
        self._buffered_writes: Dict[str, pd.DataFrame] = {}

    @property
    def tx_id(self) -> int:
        """Get the transaction ID."""
        return self._tx_id

    def query(
        self,
        sql: str,
        versions: Optional[Dict[str, int]] = None,
    ) -> "QueryResult":
        """
        Execute a SQL query within this transaction.

        Reads see the snapshot at transaction start, plus any
        writes made within this transaction.
        """
        if self._committed or self._aborted:
            raise RuntimeError("Transaction is no longer active")

        # For now, delegate to engine (future: handle read-your-writes)
        return self._engine.query(sql, versions, branch=self._branch)

    def write_table(
        self,
        table_name: str,
        data: pd.DataFrame,
        metadata: Optional[Dict[str, str]] = None,
    ) -> None:
        """
        Buffer a table write within this transaction.

        Writes are not visible to other transactions until commit.
        """
        if self._committed or self._aborted:
            raise RuntimeError("Transaction is no longer active")

        self._buffered_writes[table_name] = {
            "data": data,
            "metadata": metadata,
        }

    def commit(self) -> None:
        """Commit the transaction."""
        if self._committed:
            return  # Idempotent
        if self._aborted:
            raise RuntimeError("Cannot commit aborted transaction")

        # Apply buffered writes
        for table_name, write_info in self._buffered_writes.items():
            result = self._engine.writer.write(
                table_name,
                write_info["data"],
                write_info["metadata"],
            )

            # Record write with transaction manager
            self._engine.transaction_manager.add_write(
                self._tx_id,
                table_name,
                result.version,
                result.chunk_hashes,
            )

        # Commit through transaction manager
        self._engine.transaction_manager.commit(self._tx_id)
        self._committed = True

    def abort(self, reason: Optional[str] = None) -> None:
        """Abort the transaction."""
        if self._committed:
            raise RuntimeError("Cannot abort committed transaction")
        if self._aborted:
            return  # Idempotent

        self._engine.transaction_manager.abort(
            self._tx_id,
            reason or "User requested",
        )
        self._aborted = True
```

---

## Part 6: Implementation Phases

### Phase 5.0: Core Transactions ✅ COMPLETE

**Goal:** Basic cross-table ACID with table-level conflicts

**Files created:**
1. `udr_core/src/transaction/mod.rs` - Module exports ✅
2. `udr_core/src/transaction/types.rs` - Core types ✅
3. `udr_core/src/transaction/epoch.rs` - Epoch types ✅
4. `udr_core/src/transaction/error.rs` - Error types ✅
5. `udr_core/src/transaction/log.rs` - Transaction log ✅
6. `udr_core/src/transaction/manager.rs` - Transaction manager ✅
7. `udr_core/src/transaction/conflict.rs` - Conflict detection ✅
8. `udr_core/src/transaction/recovery.rs` - Recovery manager ✅

**Files modified:**
1. `udr_core/src/lib.rs` - Export transaction module ✅
2. `udr_python/src/lib.rs` - Add Python bindings ✅
3. `python/udr.pyi` - Type stubs for transactions ✅

**Tests:** 71 new Rust tests passing
- Transaction begin/commit/abort ✅
- Cross-table atomic commit ✅
- Conflict detection (table-level) ✅
- Crash recovery (basic) ✅
- Epoch management ✅
- Snapshot isolation ✅

### Phase 5.1: QueryEngine Integration (Week 2-3)

**Goal:** Seamless Python API

**Files to create:**
1. `python/udr_query/transaction.py` - Transaction context
2. `tests/test_transactions.py` - Transaction tests

**Files to modify:**
1. `python/udr_query/engine.py` - Add transaction support
2. `python/udr.pyi` - Type stubs

**Tests:**
- Context manager API
- Read-your-writes within transaction
- Rollback on exception

### Phase 5.2: Recovery & Robustness (Week 3)

**Goal:** Production-grade recovery

**Files to create:**
1. `udr_core/src/transaction/recovery.rs` - Recovery manager

**Tests:**
- Crash during write phase
- Crash during commit phase
- Epoch recovery
- Consistency verification

### Phase 5.5: Row-Level Conflicts (Future)

**Goal:** Higher concurrency for write-heavy workloads

**Not in initial scope - design supports future addition**

---

## Part 7: Test Strategy

### Unit Tests (Rust)

```rust
#[cfg(test)]
mod tests {
    // Transaction lifecycle
    #[test] fn test_begin_commit()
    #[test] fn test_begin_abort()
    #[test] fn test_multiple_writes()

    // Conflict detection
    #[test] fn test_no_conflict_different_tables()
    #[test] fn test_conflict_same_table()
    #[test] fn test_conflict_after_commit()

    // Snapshot isolation
    #[test] fn test_snapshot_read()
    #[test] fn test_snapshot_violation()

    // Epoch management
    #[test] fn test_epoch_creation()
    #[test] fn test_epoch_commit()

    // Recovery
    #[test] fn test_recovery_pending_tx()
    #[test] fn test_recovery_committed_tx()
}
```

### Integration Tests (Python)

```python
class TestTransactionBasics:
    def test_transaction_commit(self)
    def test_transaction_abort(self)
    def test_transaction_rollback_on_exception(self)

class TestCrossTableTransactions:
    def test_atomic_multi_table_commit(self)
    def test_partial_failure_rolls_back(self)

class TestConflictDetection:
    def test_concurrent_different_tables(self)
    def test_concurrent_same_table_conflicts(self)

class TestSnapshotIsolation:
    def test_reads_see_snapshot(self)
    def test_writes_not_visible_until_commit(self)

class TestBranchIntegration:
    def test_transaction_updates_branch_head(self)
    def test_transaction_on_feature_branch(self)
```

---

## Part 8: Success Criteria

### Must Have (Phase 5.0) ✅ COMPLETE
- [x] Multi-table atomic commits (TransactionManager)
- [x] Table-level conflict detection (TableLevelConflictDetector)
- [x] Branch head updates on commit (TransactionManager)
- [x] Basic crash recovery (RecoveryManager)
- [x] Transaction log persistence (TransactionLog)
- [x] Epoch-based organization (EpochConfig, EpochMetadata)
- [x] Read snapshot capture (record_read)
- [x] Snapshot violation detection (ConflictDetector trait)
- [ ] `engine.transaction()` context manager (Phase 5.1)
- [ ] Rollback on exception (Phase 5.1)

### Should Have (Phase 5.1)
- [ ] Python context manager API
- [ ] Read-your-writes within transaction
- [ ] QueryEngine integration

### Nice to Have (Phase 5.2+)
- [ ] Row-level conflict detection
- [ ] Distributed transaction support
- [ ] Advanced recovery scenarios

---

## Appendix: Design Decisions Log

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Epoch model | Time-based, configurable | Scales from POC to distributed |
| Conflict detection | Table-level (Phase 5.0) | Simple, sufficient for most cases |
| WAL approach | Intent log + epoch snapshots | Leverages content addressing |
| API style | Context manager + layered | Pythonic, progressive complexity |
| Log format | JSON with version field | Human-readable, evolvable |
| Recovery | State machine based | Deterministic, testable |

---

*Plan version: 1.0*
*Created: January 2026*
