//! Tests for the changelog module.

use super::*;
use crate::transaction::{TransactionRecord, TransactionStatus, TableWrite};
use std::collections::HashMap;

mod entry_tests {
    use super::*;

    #[test]
    fn test_table_change_new() {
        let change = TableChange::new(
            "users",
            Some(1),
            2,
            vec!["hash1".to_string(), "hash2".to_string()],
        );

        assert_eq!(change.table_name, "users");
        assert_eq!(change.old_version, Some(1));
        assert_eq!(change.new_version, 2);
        assert_eq!(change.chunk_hashes.len(), 2);
        assert!(!change.is_new_table());
    }

    #[test]
    fn test_table_change_new_table() {
        let change = TableChange::new("users", None, 1, vec![]);
        assert!(change.is_new_table());
    }

    #[test]
    fn test_changelog_entry_new() {
        let entry = ChangelogEntry::new(1, 1, 1000, "main");

        assert_eq!(entry.tx_id, 1);
        assert_eq!(entry.epoch_id, 1);
        assert_eq!(entry.committed_at, 1000);
        assert_eq!(entry.branch, "main");
        assert!(entry.changes.is_empty());
    }

    #[test]
    fn test_changelog_entry_add_change() {
        let mut entry = ChangelogEntry::new(1, 1, 1000, "main");
        entry.add_change(TableChange::new("users", None, 1, vec![]));
        entry.add_change(TableChange::new("orders", Some(5), 6, vec![]));

        assert_eq!(entry.changes.len(), 2);
        assert_eq!(entry.change_count(), 2);
    }

    #[test]
    fn test_changelog_entry_changed_tables() {
        let mut entry = ChangelogEntry::new(1, 1, 1000, "main");
        entry.add_change(TableChange::new("users", None, 1, vec![]));
        entry.add_change(TableChange::new("orders", Some(5), 6, vec![]));

        let tables = entry.changed_tables();
        assert_eq!(tables.len(), 2);
        assert!(tables.contains(&"users"));
        assert!(tables.contains(&"orders"));
    }

    #[test]
    fn test_changelog_entry_contains_table() {
        let mut entry = ChangelogEntry::new(1, 1, 1000, "main");
        entry.add_change(TableChange::new("users", None, 1, vec![]));

        assert!(entry.contains_table("users"));
        assert!(!entry.contains_table("orders"));
    }

    #[test]
    fn test_changelog_entry_get_change() {
        let mut entry = ChangelogEntry::new(1, 1, 1000, "main");
        entry.add_change(TableChange::new("users", None, 1, vec![]));
        entry.add_change(TableChange::new("orders", Some(5), 6, vec![]));

        let users_change = entry.get_change("users");
        assert!(users_change.is_some());
        assert_eq!(users_change.unwrap().new_version, 1);

        let audit_change = entry.get_change("audit");
        assert!(audit_change.is_none());
    }

    #[test]
    fn test_changelog_entry_from_transaction() {
        // Create a mock transaction record
        let mut tx = TransactionRecord::new(1, 1, "main".to_string());
        tx.committed_at = Some(1000);
        tx.status = TransactionStatus::Committed;
        tx.writes.push(TableWrite::new("users", 2, vec!["hash1".to_string()]));
        tx.writes.push(TableWrite::new("orders", 6, vec!["hash2".to_string()]));

        // Previous versions
        let mut prev_versions = HashMap::new();
        prev_versions.insert("users".to_string(), 1);
        prev_versions.insert("orders".to_string(), 5);

        let entry = ChangelogEntry::from_transaction(&tx, &prev_versions);

        assert_eq!(entry.tx_id, 1);
        assert_eq!(entry.epoch_id, 1);
        assert_eq!(entry.committed_at, 1000);
        assert_eq!(entry.branch, "main");
        assert_eq!(entry.changes.len(), 2);

        let users_change = entry.get_change("users").unwrap();
        assert_eq!(users_change.old_version, Some(1));
        assert_eq!(users_change.new_version, 2);

        let orders_change = entry.get_change("orders").unwrap();
        assert_eq!(orders_change.old_version, Some(5));
        assert_eq!(orders_change.new_version, 6);
    }

    #[test]
    fn test_changelog_entry_from_transaction_new_table() {
        let mut tx = TransactionRecord::new(1, 1, "main".to_string());
        tx.committed_at = Some(1000);
        tx.status = TransactionStatus::Committed;
        tx.writes.push(TableWrite::new("new_table", 1, vec![]));

        // Empty previous versions (new table)
        let prev_versions = HashMap::new();

        let entry = ChangelogEntry::from_transaction(&tx, &prev_versions);

        let change = entry.get_change("new_table").unwrap();
        assert!(change.is_new_table());
        assert_eq!(change.old_version, None);
        assert_eq!(change.new_version, 1);
    }
}

mod query_tests {
    use super::*;

    // Query builder tests are in query.rs
    // These are integration tests with ChangelogEntry

    fn make_entry(tx_id: u64, branch: &str, tables: Vec<(&str, Option<u64>, u64)>) -> ChangelogEntry {
        let mut entry = ChangelogEntry::new(tx_id, 1, tx_id as i64 * 1000, branch);
        for (name, old_v, new_v) in tables {
            entry.add_change(TableChange::new(name, old_v, new_v, vec![]));
        }
        entry
    }

    #[test]
    fn test_query_filter_workflow() {
        let entries = [
            make_entry(1, "main", vec![("users", None, 1)]),
            make_entry(2, "main", vec![("orders", None, 1)]),
            make_entry(3, "feature", vec![("users", Some(1), 2)]),
            make_entry(4, "main", vec![("users", Some(1), 2), ("orders", Some(1), 2)]),
        ];

        // Filter by branch
        let query = ChangelogQuery::new().on_branch("main");
        let main_entries: Vec<_> = entries.iter()
            .filter(|e| query.matches_entry(e))
            .collect();
        assert_eq!(main_entries.len(), 3);

        // Filter by table
        let query = ChangelogQuery::new().for_tables(vec!["users".to_string()]);
        let user_entries: Vec<_> = entries.iter()
            .filter(|e| query.matches_entry(e))
            .collect();
        assert_eq!(user_entries.len(), 3); // 1, 3, 4

        // Combined filter
        let query = ChangelogQuery::new()
            .on_branch("main")
            .for_tables(vec!["users".to_string()]);
        let combined: Vec<_> = entries.iter()
            .filter(|e| query.matches_entry(e))
            .collect();
        assert_eq!(combined.len(), 2); // 1, 4
    }
}

mod serialization_tests {
    use super::*;

    #[test]
    fn test_table_change_serialization() {
        let change = TableChange::new("users", Some(1), 2, vec!["hash".to_string()]);

        let json = serde_json::to_string(&change).unwrap();
        let deserialized: TableChange = serde_json::from_str(&json).unwrap();

        assert_eq!(change, deserialized);
    }

    #[test]
    fn test_changelog_entry_serialization() {
        let mut entry = ChangelogEntry::new(1, 1, 1000, "main");
        entry.add_change(TableChange::new("users", Some(1), 2, vec![]));

        let json = serde_json::to_string(&entry).unwrap();
        let deserialized: ChangelogEntry = serde_json::from_str(&json).unwrap();

        assert_eq!(entry, deserialized);
    }
}
