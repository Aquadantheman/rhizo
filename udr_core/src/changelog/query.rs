//! Query builder for changelog filtering.

/// Query parameters for filtering changelog entries.
///
/// Uses a builder pattern for ergonomic query construction:
///
/// ```ignore
/// let query = ChangelogQuery::new()
///     .since_tx(100)
///     .for_tables(vec!["users".to_string(), "orders".to_string()])
///     .on_branch("main")
///     .with_limit(50);
/// ```
#[derive(Debug, Clone, Default)]
pub struct ChangelogQuery {
    /// Start from this transaction ID (exclusive)
    pub since_tx_id: Option<u64>,

    /// Start from this timestamp (inclusive, Unix seconds)
    pub since_timestamp: Option<i64>,

    /// Filter to specific tables (None = all tables)
    pub tables: Option<Vec<String>>,

    /// Filter to specific branch (None = all branches)
    pub branch: Option<String>,

    /// Maximum entries to return (None = unlimited)
    pub limit: Option<usize>,
}

impl ChangelogQuery {
    /// Create a new empty query (matches all entries)
    pub fn new() -> Self {
        Self::default()
    }

    /// Filter to entries after this transaction ID (exclusive).
    ///
    /// Only entries with tx_id > since_tx_id will be returned.
    pub fn since_tx(mut self, tx_id: u64) -> Self {
        self.since_tx_id = Some(tx_id);
        self
    }

    /// Filter to entries at or after this timestamp.
    ///
    /// Only entries with committed_at >= timestamp will be returned.
    pub fn since_time(mut self, timestamp: i64) -> Self {
        self.since_timestamp = Some(timestamp);
        self
    }

    /// Filter to entries that changed any of these tables.
    ///
    /// An entry is included if it changed at least one of the specified tables.
    pub fn for_tables(mut self, tables: Vec<String>) -> Self {
        self.tables = Some(tables);
        self
    }

    /// Filter to entries on a specific branch.
    pub fn on_branch(mut self, branch: impl Into<String>) -> Self {
        self.branch = Some(branch.into());
        self
    }

    /// Limit the number of entries returned.
    pub fn with_limit(mut self, limit: usize) -> Self {
        self.limit = Some(limit);
        self
    }

    /// Check if an entry matches this query's filters.
    ///
    /// Note: This does NOT check since_tx_id or since_timestamp,
    /// which are typically handled by the query executor for efficiency.
    pub fn matches_entry(&self, entry: &super::entry::ChangelogEntry) -> bool {
        // Check branch filter
        if let Some(ref branch) = self.branch {
            if &entry.branch != branch {
                return false;
            }
        }

        // Check table filter
        if let Some(ref tables) = self.tables {
            let has_matching_table = entry.changes.iter()
                .any(|c| tables.contains(&c.table_name));
            if !has_matching_table {
                return false;
            }
        }

        true
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::changelog::entry::{ChangelogEntry, TableChange};

    fn make_entry(tx_id: u64, branch: &str, tables: Vec<&str>) -> ChangelogEntry {
        let mut entry = ChangelogEntry::new(tx_id, 1, 1000, branch);
        for table in tables {
            entry.add_change(TableChange::new(table, Some(1), 2, vec![]));
        }
        entry
    }

    #[test]
    fn test_query_builder() {
        let query = ChangelogQuery::new()
            .since_tx(100)
            .for_tables(vec!["users".to_string()])
            .on_branch("main")
            .with_limit(10);

        assert_eq!(query.since_tx_id, Some(100));
        assert_eq!(query.tables, Some(vec!["users".to_string()]));
        assert_eq!(query.branch, Some("main".to_string()));
        assert_eq!(query.limit, Some(10));
    }

    #[test]
    fn test_matches_no_filter() {
        let query = ChangelogQuery::new();
        let entry = make_entry(1, "main", vec!["users"]);
        assert!(query.matches_entry(&entry));
    }

    #[test]
    fn test_matches_branch_filter() {
        let query = ChangelogQuery::new().on_branch("main");

        let main_entry = make_entry(1, "main", vec!["users"]);
        let feature_entry = make_entry(2, "feature", vec!["users"]);

        assert!(query.matches_entry(&main_entry));
        assert!(!query.matches_entry(&feature_entry));
    }

    #[test]
    fn test_matches_table_filter() {
        let query = ChangelogQuery::new()
            .for_tables(vec!["users".to_string(), "orders".to_string()]);

        let users_entry = make_entry(1, "main", vec!["users"]);
        let orders_entry = make_entry(2, "main", vec!["orders"]);
        let audit_entry = make_entry(3, "main", vec!["audit"]);
        let multi_entry = make_entry(4, "main", vec!["users", "audit"]);

        assert!(query.matches_entry(&users_entry));
        assert!(query.matches_entry(&orders_entry));
        assert!(!query.matches_entry(&audit_entry));
        assert!(query.matches_entry(&multi_entry)); // Has users
    }

    #[test]
    fn test_matches_combined_filters() {
        let query = ChangelogQuery::new()
            .on_branch("main")
            .for_tables(vec!["users".to_string()]);

        let match_entry = make_entry(1, "main", vec!["users"]);
        let wrong_branch = make_entry(2, "feature", vec!["users"]);
        let wrong_table = make_entry(3, "main", vec!["orders"]);

        assert!(query.matches_entry(&match_entry));
        assert!(!query.matches_entry(&wrong_branch));
        assert!(!query.matches_entry(&wrong_table));
    }
}
