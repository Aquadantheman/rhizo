//! Conflict detection for concurrent transactions.
//!
//! This module provides pluggable conflict detection strategies:
//! - `TableLevelConflictDetector` - Two transactions conflict if they write the same table
//! - `PartitionLevelConflictDetector` - (Future) Conflict on same partition
//! - `RowLevelConflictDetector` - (Future) Conflict on same row keys
//!
//! The conflict detection strategy determines the concurrency/isolation trade-off.

use std::collections::HashSet;
use super::types::{TransactionRecord, WriteGranularity};

/// Represents a detected conflict between two transactions
#[derive(Debug, Clone)]
pub struct Conflict {
    /// Tables (or resources) that conflict
    pub tables: Vec<String>,

    /// First transaction ID
    pub tx1_id: u64,

    /// Second transaction ID
    pub tx2_id: u64,

    /// Description of the conflict
    pub description: String,
}

impl Conflict {
    /// Create a new conflict
    pub fn new(tables: Vec<String>, tx1_id: u64, tx2_id: u64) -> Self {
        let description = format!(
            "Write-write conflict on tables {:?} between tx {} and tx {}",
            tables, tx1_id, tx2_id
        );
        Self {
            tables,
            tx1_id,
            tx2_id,
            description,
        }
    }

    /// Check if conflict involves a specific table
    pub fn involves_table(&self, table: &str) -> bool {
        self.tables.iter().any(|t| t == table)
    }
}

/// Trait for conflict detection strategies
pub trait ConflictDetector: Send + Sync {
    /// Detect conflicts between two transactions
    ///
    /// Returns `Some(Conflict)` if the transactions conflict, `None` otherwise.
    fn detect(&self, tx1: &TransactionRecord, tx2: &TransactionRecord) -> Option<Conflict>;

    /// Get the name of this detector for debugging
    fn name(&self) -> &'static str;
}

/// Table-level conflict detection (Phase 5.0)
///
/// Two transactions conflict if they write to the same table.
/// This is the simplest and most conservative strategy.
#[derive(Debug, Default)]
pub struct TableLevelConflictDetector;

impl TableLevelConflictDetector {
    pub fn new() -> Self {
        Self
    }
}

impl ConflictDetector for TableLevelConflictDetector {
    fn detect(&self, tx1: &TransactionRecord, tx2: &TransactionRecord) -> Option<Conflict> {
        // Collect tables written by each transaction
        let tables1: HashSet<_> = tx1.writes.iter()
            .map(|w| w.table_name.as_str())
            .collect();
        let tables2: HashSet<_> = tx2.writes.iter()
            .map(|w| w.table_name.as_str())
            .collect();

        // Find intersection (tables written by both)
        let conflicts: Vec<String> = tables1
            .intersection(&tables2)
            .map(|s| s.to_string())
            .collect();

        if conflicts.is_empty() {
            None
        } else {
            Some(Conflict::new(conflicts, tx1.tx_id, tx2.tx_id))
        }
    }

    fn name(&self) -> &'static str {
        "TableLevelConflictDetector"
    }
}

/// Partition-level conflict detection (Phase 5.5 - future)
///
/// Two transactions conflict only if they write to the same partition
/// of the same table.
#[allow(dead_code)] // Planned for Phase 5.5
#[derive(Debug, Default)]
pub struct PartitionLevelConflictDetector;

#[allow(dead_code)] // Planned for Phase 5.5
impl PartitionLevelConflictDetector {
    pub fn new() -> Self {
        Self
    }
}

impl ConflictDetector for PartitionLevelConflictDetector {
    fn detect(&self, tx1: &TransactionRecord, tx2: &TransactionRecord) -> Option<Conflict> {
        // For each table, check if writes overlap on partitions
        for write1 in &tx1.writes {
            for write2 in &tx2.writes {
                if write1.table_name != write2.table_name {
                    continue;
                }

                // Check partition overlap
                match (&write1.granularity, &write2.granularity) {
                    // Both whole-table writes -> conflict
                    (WriteGranularity::WholeTable, WriteGranularity::WholeTable) => {
                        return Some(Conflict::new(
                            vec![write1.table_name.clone()],
                            tx1.tx_id,
                            tx2.tx_id,
                        ));
                    }
                    // One is whole-table, other is partition -> conflict
                    (WriteGranularity::WholeTable, WriteGranularity::Partitions(_)) |
                    (WriteGranularity::Partitions(_), WriteGranularity::WholeTable) => {
                        return Some(Conflict::new(
                            vec![write1.table_name.clone()],
                            tx1.tx_id,
                            tx2.tx_id,
                        ));
                    }
                    // Both partition writes -> check overlap
                    (WriteGranularity::Partitions(p1), WriteGranularity::Partitions(p2)) => {
                        let parts1: HashSet<_> = p1.iter().collect();
                        let parts2: HashSet<_> = p2.iter().collect();
                        if parts1.intersection(&parts2).next().is_some() {
                            return Some(Conflict::new(
                                vec![write1.table_name.clone()],
                                tx1.tx_id,
                                tx2.tx_id,
                            ));
                        }
                    }
                    // Keys granularity - treat as whole table for now
                    _ => {
                        return Some(Conflict::new(
                            vec![write1.table_name.clone()],
                            tx1.tx_id,
                            tx2.tx_id,
                        ));
                    }
                }
            }
        }

        None
    }

    fn name(&self) -> &'static str {
        "PartitionLevelConflictDetector"
    }
}

/// Row-level conflict detection (Phase 5.x - future)
///
/// Two transactions conflict only if they write to the same row keys.
/// This provides the highest concurrency but requires key tracking.
#[allow(dead_code)] // Planned for Phase 5.x
#[derive(Debug, Default)]
pub struct RowLevelConflictDetector;

#[allow(dead_code)] // Planned for Phase 5.x
impl RowLevelConflictDetector {
    pub fn new() -> Self {
        Self
    }
}

impl ConflictDetector for RowLevelConflictDetector {
    fn detect(&self, tx1: &TransactionRecord, tx2: &TransactionRecord) -> Option<Conflict> {
        // TODO: Implement row-level conflict detection
        // For now, fall back to table-level
        TableLevelConflictDetector.detect(tx1, tx2)
    }

    fn name(&self) -> &'static str {
        "RowLevelConflictDetector"
    }
}

/// Multi-strategy conflict detector
///
/// Uses different detection strategies based on write granularity.
#[allow(dead_code)] // Planned for future adaptive conflict detection
#[derive(Debug, Default)]
pub struct AdaptiveConflictDetector;

#[allow(dead_code)]
impl AdaptiveConflictDetector {
    pub fn new() -> Self {
        Self
    }
}

impl ConflictDetector for AdaptiveConflictDetector {
    fn detect(&self, tx1: &TransactionRecord, tx2: &TransactionRecord) -> Option<Conflict> {
        // Check if both transactions use partition granularity
        let has_partition_writes = tx1.writes.iter().chain(tx2.writes.iter())
            .any(|w| matches!(w.granularity, WriteGranularity::Partitions(_)));

        if has_partition_writes {
            PartitionLevelConflictDetector.detect(tx1, tx2)
        } else {
            TableLevelConflictDetector.detect(tx1, tx2)
        }
    }

    fn name(&self) -> &'static str {
        "AdaptiveConflictDetector"
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::transaction::types::TableWrite;

    fn create_tx_with_writes(tx_id: u64, tables: &[&str]) -> TransactionRecord {
        let mut tx = TransactionRecord::new(tx_id, 1, "main".to_string());
        for table in tables {
            tx.add_write(TableWrite::new(*table, 1, vec!["chunk".to_string()]));
        }
        tx
    }

    #[test]
    fn test_no_conflict_different_tables() {
        let detector = TableLevelConflictDetector::new();

        let tx1 = create_tx_with_writes(1, &["users"]);
        let tx2 = create_tx_with_writes(2, &["orders"]);

        let conflict = detector.detect(&tx1, &tx2);
        assert!(conflict.is_none());
    }

    #[test]
    fn test_conflict_same_table() {
        let detector = TableLevelConflictDetector::new();

        let tx1 = create_tx_with_writes(1, &["users"]);
        let tx2 = create_tx_with_writes(2, &["users"]);

        let conflict = detector.detect(&tx1, &tx2);
        assert!(conflict.is_some());

        let conflict = conflict.unwrap();
        assert_eq!(conflict.tables, vec!["users"]);
        assert_eq!(conflict.tx1_id, 1);
        assert_eq!(conflict.tx2_id, 2);
    }

    #[test]
    fn test_conflict_multiple_tables() {
        let detector = TableLevelConflictDetector::new();

        let tx1 = create_tx_with_writes(1, &["users", "orders"]);
        let tx2 = create_tx_with_writes(2, &["orders", "products"]);

        let conflict = detector.detect(&tx1, &tx2);
        assert!(conflict.is_some());

        let conflict = conflict.unwrap();
        assert!(conflict.involves_table("orders"));
    }

    #[test]
    fn test_no_conflict_empty_writes() {
        let detector = TableLevelConflictDetector::new();

        let tx1 = TransactionRecord::new(1, 1, "main".to_string());
        let tx2 = TransactionRecord::new(2, 1, "main".to_string());

        let conflict = detector.detect(&tx1, &tx2);
        assert!(conflict.is_none());
    }

    #[test]
    fn test_no_conflict_one_empty() {
        let detector = TableLevelConflictDetector::new();

        let tx1 = create_tx_with_writes(1, &["users"]);
        let tx2 = TransactionRecord::new(2, 1, "main".to_string());

        let conflict = detector.detect(&tx1, &tx2);
        assert!(conflict.is_none());
    }

    #[test]
    fn test_partition_conflict_whole_table() {
        let detector = PartitionLevelConflictDetector::new();

        let tx1 = create_tx_with_writes(1, &["users"]);
        let tx2 = create_tx_with_writes(2, &["users"]);

        // Both WholeTable -> should conflict
        let conflict = detector.detect(&tx1, &tx2);
        assert!(conflict.is_some());
    }

    #[test]
    fn test_partition_no_conflict_different_partitions() {
        let detector = PartitionLevelConflictDetector::new();

        let mut tx1 = TransactionRecord::new(1, 1, "main".to_string());
        tx1.add_write(TableWrite::new("users", 1, vec!["chunk".to_string()])
            .with_granularity(WriteGranularity::Partitions(vec!["p1".to_string()])));

        let mut tx2 = TransactionRecord::new(2, 1, "main".to_string());
        tx2.add_write(TableWrite::new("users", 1, vec!["chunk".to_string()])
            .with_granularity(WriteGranularity::Partitions(vec!["p2".to_string()])));

        let conflict = detector.detect(&tx1, &tx2);
        assert!(conflict.is_none());
    }

    #[test]
    fn test_partition_conflict_same_partition() {
        let detector = PartitionLevelConflictDetector::new();

        let mut tx1 = TransactionRecord::new(1, 1, "main".to_string());
        tx1.add_write(TableWrite::new("users", 1, vec!["chunk".to_string()])
            .with_granularity(WriteGranularity::Partitions(vec!["p1".to_string()])));

        let mut tx2 = TransactionRecord::new(2, 1, "main".to_string());
        tx2.add_write(TableWrite::new("users", 1, vec!["chunk".to_string()])
            .with_granularity(WriteGranularity::Partitions(vec!["p1".to_string(), "p2".to_string()])));

        let conflict = detector.detect(&tx1, &tx2);
        assert!(conflict.is_some());
    }

    #[test]
    fn test_adaptive_detector() {
        let detector = AdaptiveConflictDetector::new();

        // Table-level writes
        let tx1 = create_tx_with_writes(1, &["users"]);
        let tx2 = create_tx_with_writes(2, &["users"]);

        let conflict = detector.detect(&tx1, &tx2);
        assert!(conflict.is_some());

        // Different tables
        let tx3 = create_tx_with_writes(3, &["orders"]);
        let conflict = detector.detect(&tx1, &tx3);
        assert!(conflict.is_none());
    }

    #[test]
    fn test_conflict_description() {
        let conflict = Conflict::new(
            vec!["users".to_string(), "orders".to_string()],
            1,
            2,
        );

        assert!(conflict.description.contains("users"));
        assert!(conflict.description.contains("orders"));
        assert!(conflict.description.contains("tx 1"));
        assert!(conflict.description.contains("tx 2"));
    }

    #[test]
    fn test_detector_names() {
        assert_eq!(TableLevelConflictDetector::new().name(), "TableLevelConflictDetector");
        assert_eq!(PartitionLevelConflictDetector::new().name(), "PartitionLevelConflictDetector");
        assert_eq!(RowLevelConflictDetector::new().name(), "RowLevelConflictDetector");
        assert_eq!(AdaptiveConflictDetector::new().name(), "AdaptiveConflictDetector");
    }
}
