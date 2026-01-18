//! Epoch types for organizing transactions into time-based groups.
//!
//! Epochs provide:
//! - Temporal ordering of transactions
//! - Batching for throughput optimization
//! - Recovery boundaries
//! - Foundation for distributed coordination

use serde::{Deserialize, Serialize};
use super::types::{EpochId, TxId};

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
    /// Each transaction is its own epoch (simplest model)
    pub fn single_node() -> Self {
        Self {
            duration_ms: 0,          // Immediate commit
            max_transactions: 1,     // One tx per epoch
            batching_enabled: false,
        }
    }

    /// Configuration for high-throughput mode
    /// Batches many transactions per epoch
    pub fn high_throughput() -> Self {
        Self {
            duration_ms: 50,         // 50ms epochs
            max_transactions: 10000, // High batch size
            batching_enabled: true,
        }
    }

    /// Configuration for low-latency mode
    /// Smaller epochs for faster commits
    pub fn low_latency() -> Self {
        Self {
            duration_ms: 10,         // 10ms epochs
            max_transactions: 100,   // Smaller batches
            batching_enabled: true,
        }
    }

    /// Check if this config uses immediate mode (no batching)
    pub fn is_immediate(&self) -> bool {
        self.duration_ms == 0 || self.max_transactions <= 1
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

impl std::fmt::Display for EpochStatus {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            EpochStatus::Active => write!(f, "Active"),
            EpochStatus::Committing => write!(f, "Committing"),
            EpochStatus::Committed => write!(f, "Committed"),
            EpochStatus::RolledBack => write!(f, "RolledBack"),
        }
    }
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

    /// Number of committed transactions
    pub committed_count: u64,

    /// Number of aborted transactions
    pub aborted_count: u64,

    /// Schema version for forward compatibility
    pub format_version: u32,
}

impl EpochMetadata {
    /// Current format version
    pub const CURRENT_FORMAT_VERSION: u32 = 1;

    /// Create new epoch metadata
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
            committed_count: 0,
            aborted_count: 0,
            format_version: Self::CURRENT_FORMAT_VERSION,
        }
    }

    /// Add a transaction to this epoch
    pub fn add_transaction(&mut self, tx_id: TxId) {
        if self.first_tx_id.is_none() {
            self.first_tx_id = Some(tx_id);
        }
        self.last_tx_id = Some(tx_id);
        self.transactions.push(tx_id);
    }

    /// Record a committed transaction
    pub fn record_commit(&mut self) {
        self.committed_count += 1;
    }

    /// Record an aborted transaction
    pub fn record_abort(&mut self) {
        self.aborted_count += 1;
    }

    /// Get total transaction count
    pub fn transaction_count(&self) -> usize {
        self.transactions.len()
    }

    /// Check if epoch is active
    pub fn is_active(&self) -> bool {
        matches!(self.status, EpochStatus::Active)
    }

    /// Check if epoch is committed
    pub fn is_committed(&self) -> bool {
        matches!(self.status, EpochStatus::Committed)
    }

    /// Mark epoch as committing (no more transactions accepted)
    pub fn mark_committing(&mut self) {
        self.status = EpochStatus::Committing;
    }

    /// Mark epoch as fully committed
    pub fn mark_committed(&mut self) {
        let now = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_secs() as i64;

        self.status = EpochStatus::Committed;
        self.ended_at = Some(now);
    }

    /// Mark epoch as rolled back
    pub fn mark_rolled_back(&mut self) {
        let now = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_secs() as i64;

        self.status = EpochStatus::RolledBack;
        self.ended_at = Some(now);
    }

    /// Get duration in milliseconds (if ended)
    pub fn duration_ms(&self) -> Option<u64> {
        self.ended_at.map(|end| {
            ((end - self.started_at) * 1000) as u64
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_epoch_config_default() {
        let config = EpochConfig::default();
        assert_eq!(config.duration_ms, 100);
        assert_eq!(config.max_transactions, 1000);
        assert!(config.batching_enabled);
        assert!(!config.is_immediate());
    }

    #[test]
    fn test_epoch_config_single_node() {
        let config = EpochConfig::single_node();
        assert_eq!(config.duration_ms, 0);
        assert_eq!(config.max_transactions, 1);
        assert!(!config.batching_enabled);
        assert!(config.is_immediate());
    }

    #[test]
    fn test_epoch_config_high_throughput() {
        let config = EpochConfig::high_throughput();
        assert_eq!(config.duration_ms, 50);
        assert_eq!(config.max_transactions, 10000);
        assert!(config.batching_enabled);
    }

    #[test]
    fn test_epoch_metadata_new() {
        let meta = EpochMetadata::new(1);
        assert_eq!(meta.epoch_id, 1);
        assert!(meta.is_active());
        assert!(meta.transactions.is_empty());
        assert!(meta.first_tx_id.is_none());
        assert!(meta.last_tx_id.is_none());
    }

    #[test]
    fn test_epoch_add_transactions() {
        let mut meta = EpochMetadata::new(1);

        meta.add_transaction(1);
        assert_eq!(meta.first_tx_id, Some(1));
        assert_eq!(meta.last_tx_id, Some(1));
        assert_eq!(meta.transaction_count(), 1);

        meta.add_transaction(2);
        meta.add_transaction(3);

        assert_eq!(meta.first_tx_id, Some(1));
        assert_eq!(meta.last_tx_id, Some(3));
        assert_eq!(meta.transaction_count(), 3);
        assert_eq!(meta.transactions, vec![1, 2, 3]);
    }

    #[test]
    fn test_epoch_status_transitions() {
        let mut meta = EpochMetadata::new(1);

        assert!(meta.is_active());

        meta.mark_committing();
        assert_eq!(meta.status, EpochStatus::Committing);

        meta.mark_committed();
        assert!(meta.is_committed());
        assert!(meta.ended_at.is_some());
    }

    #[test]
    fn test_epoch_commit_abort_counts() {
        let mut meta = EpochMetadata::new(1);

        meta.record_commit();
        meta.record_commit();
        meta.record_abort();

        assert_eq!(meta.committed_count, 2);
        assert_eq!(meta.aborted_count, 1);
    }

    #[test]
    fn test_epoch_rollback() {
        let mut meta = EpochMetadata::new(1);

        meta.mark_rolled_back();
        assert_eq!(meta.status, EpochStatus::RolledBack);
        assert!(meta.ended_at.is_some());
    }
}
