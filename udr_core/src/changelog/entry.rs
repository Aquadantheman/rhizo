//! Changelog entry types representing committed changes.

use serde::{Deserialize, Serialize};
use std::collections::HashMap;

use crate::transaction::TransactionRecord;

/// A single table change within a commit.
///
/// Represents what happened to one table in a transaction:
/// - The table name
/// - The previous version (None if this is a new table)
/// - The new version after this commit
/// - The chunk hashes for the new version
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct TableChange {
    /// Table that was modified
    pub table_name: String,

    /// Previous version (None if new table)
    pub old_version: Option<u64>,

    /// New version after this commit
    pub new_version: u64,

    /// Chunk hashes for the new version
    pub chunk_hashes: Vec<String>,
}

impl TableChange {
    /// Create a new table change
    pub fn new(
        table_name: impl Into<String>,
        old_version: Option<u64>,
        new_version: u64,
        chunk_hashes: Vec<String>,
    ) -> Self {
        Self {
            table_name: table_name.into(),
            old_version,
            new_version,
            chunk_hashes,
        }
    }

    /// Check if this is a new table (no previous version)
    pub fn is_new_table(&self) -> bool {
        self.old_version.is_none()
    }
}

/// Entry in the changelog representing a committed transaction.
///
/// This is a view of a committed TransactionRecord optimized for
/// changelog consumption. It includes:
/// - Transaction identity (tx_id, epoch_id)
/// - Timing (when committed)
/// - Branch context
/// - List of table changes with before/after versions
///
/// # Example
///
/// ```ignore
/// let entry = ChangelogEntry::from_transaction(&tx_record, &prev_versions);
/// for change in &entry.changes {
///     println!("{}: v{:?} -> v{}",
///         change.table_name,
///         change.old_version,
///         change.new_version
///     );
/// }
/// ```
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
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
    /// Create a new changelog entry
    pub fn new(
        tx_id: u64,
        epoch_id: u64,
        committed_at: i64,
        branch: impl Into<String>,
    ) -> Self {
        Self {
            tx_id,
            epoch_id,
            committed_at,
            branch: branch.into(),
            changes: Vec::new(),
            metadata: HashMap::new(),
        }
    }

    /// Create from a committed TransactionRecord.
    ///
    /// # Arguments
    /// * `tx` - The committed transaction record
    /// * `previous_versions` - Map of table names to their versions before this tx
    ///
    /// # Panics
    /// Panics if the transaction is not committed (has no committed_at timestamp)
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

    /// Add a table change to this entry
    pub fn add_change(&mut self, change: TableChange) {
        self.changes.push(change);
    }

    /// Get list of changed table names
    pub fn changed_tables(&self) -> Vec<&str> {
        self.changes.iter().map(|c| c.table_name.as_str()).collect()
    }

    /// Check if a specific table was changed in this entry
    pub fn contains_table(&self, table_name: &str) -> bool {
        self.changes.iter().any(|c| c.table_name == table_name)
    }

    /// Get the change for a specific table, if present
    pub fn get_change(&self, table_name: &str) -> Option<&TableChange> {
        self.changes.iter().find(|c| c.table_name == table_name)
    }

    /// Number of tables changed in this entry
    pub fn change_count(&self) -> usize {
        self.changes.len()
    }
}
