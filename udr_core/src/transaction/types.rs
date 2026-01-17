//! Core transaction types for cross-table ACID transactions.

use serde::{Deserialize, Serialize};
use std::collections::HashMap;

/// Unique transaction identifier (monotonically increasing)
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

impl std::fmt::Display for TransactionStatus {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            TransactionStatus::Active => write!(f, "Active"),
            TransactionStatus::Preparing => write!(f, "Preparing"),
            TransactionStatus::Committed => write!(f, "Committed"),
            TransactionStatus::Aborted { reason } => write!(f, "Aborted({})", reason),
        }
    }
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

    /// Branch this write targets (None = current transaction branch)
    pub branch: Option<String>,
}

impl TableWrite {
    /// Create a new table write with whole-table granularity
    pub fn new(table_name: impl Into<String>, new_version: u64, chunk_hashes: Vec<String>) -> Self {
        Self {
            table_name: table_name.into(),
            new_version,
            chunk_hashes,
            schema_hash: None,
            granularity: WriteGranularity::WholeTable,
            branch: None,
        }
    }

    /// Set the schema hash
    pub fn with_schema_hash(mut self, hash: impl Into<String>) -> Self {
        self.schema_hash = Some(hash.into());
        self
    }

    /// Set the target branch
    pub fn with_branch(mut self, branch: impl Into<String>) -> Self {
        self.branch = Some(branch.into());
        self
    }

    /// Set the write granularity
    pub fn with_granularity(mut self, granularity: WriteGranularity) -> Self {
        self.granularity = granularity;
        self
    }
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

    /// Check if transaction is preparing
    pub fn is_preparing(&self) -> bool {
        matches!(self.status, TransactionStatus::Preparing)
    }

    /// Check if transaction is committed
    pub fn is_committed(&self) -> bool {
        matches!(self.status, TransactionStatus::Committed)
    }

    /// Check if transaction is aborted
    pub fn is_aborted(&self) -> bool {
        matches!(self.status, TransactionStatus::Aborted { .. })
    }

    /// Get list of tables being written
    pub fn written_tables(&self) -> Vec<&str> {
        self.writes.iter().map(|w| w.table_name.as_str()).collect()
    }

    /// Get list of tables being read (from snapshot)
    pub fn read_tables(&self) -> Vec<&str> {
        self.read_snapshot.keys().map(|s| s.as_str()).collect()
    }

    /// Add a write to this transaction
    pub fn add_write(&mut self, write: TableWrite) {
        self.writes.push(write);
    }

    /// Record a read from a table
    pub fn record_read(&mut self, table_name: impl Into<String>, version: u64) {
        self.read_snapshot.insert(table_name.into(), version);
    }

    /// Mark transaction as preparing
    pub fn mark_preparing(&mut self) {
        self.status = TransactionStatus::Preparing;
    }

    /// Mark transaction as committed
    pub fn mark_committed(&mut self) {
        let now = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_secs() as i64;

        self.status = TransactionStatus::Committed;
        self.committed_at = Some(now);
    }

    /// Mark transaction as aborted
    pub fn mark_aborted(&mut self, reason: impl Into<String>) {
        self.status = TransactionStatus::Aborted {
            reason: reason.into(),
        };
    }

    /// Set metadata key-value pair
    pub fn set_metadata(&mut self, key: impl Into<String>, value: impl Into<String>) {
        self.metadata.insert(key.into(), value.into());
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_transaction_record_new() {
        let tx = TransactionRecord::new(1, 1, "main".to_string());
        assert_eq!(tx.tx_id, 1);
        assert_eq!(tx.epoch_id, 1);
        assert_eq!(tx.branch, "main");
        assert!(tx.is_active());
        assert!(!tx.is_committed());
        assert!(tx.writes.is_empty());
        assert!(tx.read_snapshot.is_empty());
    }

    #[test]
    fn test_transaction_status_transitions() {
        let mut tx = TransactionRecord::new(1, 1, "main".to_string());

        assert!(tx.is_active());

        tx.mark_preparing();
        assert!(tx.is_preparing());

        tx.mark_committed();
        assert!(tx.is_committed());
        assert!(tx.committed_at.is_some());
    }

    #[test]
    fn test_transaction_aborted() {
        let mut tx = TransactionRecord::new(1, 1, "main".to_string());

        tx.mark_aborted("Conflict detected");
        assert!(tx.is_aborted());

        if let TransactionStatus::Aborted { reason } = &tx.status {
            assert_eq!(reason, "Conflict detected");
        } else {
            panic!("Expected Aborted status");
        }
    }

    #[test]
    fn test_table_write() {
        let write = TableWrite::new("users", 5, vec!["abc123".to_string()])
            .with_schema_hash("schema_xyz")
            .with_branch("feature/test");

        assert_eq!(write.table_name, "users");
        assert_eq!(write.new_version, 5);
        assert_eq!(write.chunk_hashes, vec!["abc123"]);
        assert_eq!(write.schema_hash, Some("schema_xyz".to_string()));
        assert_eq!(write.branch, Some("feature/test".to_string()));
    }

    #[test]
    fn test_transaction_writes_and_reads() {
        let mut tx = TransactionRecord::new(1, 1, "main".to_string());

        tx.record_read("users", 5);
        tx.record_read("orders", 3);

        let write = TableWrite::new("users", 6, vec!["abc".to_string()]);
        tx.add_write(write);

        assert_eq!(tx.read_snapshot.get("users"), Some(&5));
        assert_eq!(tx.read_snapshot.get("orders"), Some(&3));
        assert_eq!(tx.writes.len(), 1);
        assert_eq!(tx.written_tables(), vec!["users"]);
    }

    #[test]
    fn test_write_granularity_default() {
        let granularity = WriteGranularity::default();
        assert!(matches!(granularity, WriteGranularity::WholeTable));
    }

    #[test]
    fn test_transaction_metadata() {
        let mut tx = TransactionRecord::new(1, 1, "main".to_string());

        tx.set_metadata("user", "alice");
        tx.set_metadata("operation", "batch_update");

        assert_eq!(tx.metadata.get("user"), Some(&"alice".to_string()));
        assert_eq!(tx.metadata.get("operation"), Some(&"batch_update".to_string()));
    }
}
