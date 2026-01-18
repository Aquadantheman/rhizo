use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::time::{SystemTime, UNIX_EPOCH};

/// A branch represents a named pointer to table versions.
///
/// Branches enable Git-like workflows for data:
/// - Zero-copy creation (only pointers are copied, not data)
/// - Isolated experimentation
/// - Merge when ready
///
/// The `head` HashMap maps table names to their version numbers on this branch.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Branch {
    /// Unique name for the branch (e.g., "main", "feature/scoring-v2")
    pub name: String,

    /// Head pointers: table_name -> version number
    /// This is the core of zero-copy branching - we only store pointers
    pub head: HashMap<String, u64>,

    /// Unix timestamp when branch was created
    pub created_at: i64,

    /// Parent branch name (None for root/"main")
    pub parent_branch: Option<String>,

    /// Optional description or commit message for the branch
    pub description: Option<String>,
}

impl Branch {
    /// Create a new branch with the given name and head pointers.
    pub fn new(name: impl Into<String>, head: HashMap<String, u64>) -> Self {
        let timestamp = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_secs() as i64;

        Self {
            name: name.into(),
            head,
            created_at: timestamp,
            parent_branch: None,
            description: None,
        }
    }

    /// Create a branch from an existing branch (zero-copy).
    /// Only the head pointers are copied, not the underlying data.
    pub fn from_branch(name: impl Into<String>, parent: &Branch) -> Self {
        let timestamp = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_secs() as i64;

        Self {
            name: name.into(),
            head: parent.head.clone(), // Only copies the HashMap, not the data!
            created_at: timestamp,
            parent_branch: Some(parent.name.clone()),
            description: None,
        }
    }

    /// Set the description for this branch.
    pub fn with_description(mut self, description: impl Into<String>) -> Self {
        self.description = Some(description.into());
        self
    }

    /// Get the version of a table on this branch.
    /// Returns None if the table doesn't exist on this branch.
    pub fn get_table_version(&self, table_name: &str) -> Option<u64> {
        self.head.get(table_name).copied()
    }

    /// Update the head pointer for a table.
    pub fn set_table_version(&mut self, table_name: impl Into<String>, version: u64) {
        self.head.insert(table_name.into(), version);
    }
}

/// Result of comparing two branches.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BranchDiff {
    /// Name of the source branch
    pub source_branch: String,

    /// Name of the target branch
    pub target_branch: String,

    /// Tables that exist in both branches with the same version
    pub unchanged: Vec<String>,

    /// Tables with different versions: (table_name, source_version, target_version)
    pub modified: Vec<(String, u64, u64)>,

    /// Tables only in source branch: (table_name, version)
    pub added_in_source: Vec<(String, u64)>,

    /// Tables only in target branch: (table_name, version)
    pub added_in_target: Vec<(String, u64)>,

    /// Whether branches have diverged (both modified same table differently)
    pub has_conflicts: bool,
}

impl BranchDiff {
    /// Compute the diff between two branches.
    pub fn compute(source: &Branch, target: &Branch) -> Self {
        let mut unchanged = Vec::new();
        let mut modified = Vec::new();
        let mut added_in_source = Vec::new();
        let mut added_in_target = Vec::new();

        // Check tables in source
        for (table, &src_version) in &source.head {
            match target.head.get(table) {
                Some(&tgt_version) if src_version == tgt_version => {
                    unchanged.push(table.clone());
                }
                Some(&tgt_version) => {
                    modified.push((table.clone(), src_version, tgt_version));
                }
                None => {
                    added_in_source.push((table.clone(), src_version));
                }
            }
        }

        // Check tables only in target
        for (table, &tgt_version) in &target.head {
            if !source.head.contains_key(table) {
                added_in_target.push((table.clone(), tgt_version));
            }
        }

        // Sort for deterministic output
        unchanged.sort();
        modified.sort_by(|a, b| a.0.cmp(&b.0));
        added_in_source.sort_by(|a, b| a.0.cmp(&b.0));
        added_in_target.sort_by(|a, b| a.0.cmp(&b.0));

        // Conflicts exist if both branches modified the same table
        let has_conflicts = !modified.is_empty();

        Self {
            source_branch: source.name.clone(),
            target_branch: target.name.clone(),
            unchanged,
            modified,
            added_in_source,
            added_in_target,
            has_conflicts,
        }
    }

    /// Get the list of conflicting tables (tables modified on both branches).
    pub fn conflicting_tables(&self) -> Vec<String> {
        self.modified.iter().map(|(name, _, _)| name.clone()).collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_branch_new() {
        let mut head = HashMap::new();
        head.insert("users".to_string(), 1);
        head.insert("orders".to_string(), 2);

        let branch = Branch::new("main", head);

        assert_eq!(branch.name, "main");
        assert_eq!(branch.get_table_version("users"), Some(1));
        assert_eq!(branch.get_table_version("orders"), Some(2));
        assert_eq!(branch.get_table_version("nonexistent"), None);
        assert!(branch.parent_branch.is_none());
    }

    #[test]
    fn test_branch_from_branch() {
        let mut head = HashMap::new();
        head.insert("users".to_string(), 5);
        let main = Branch::new("main", head);

        let feature = Branch::from_branch("feature/test", &main);

        assert_eq!(feature.name, "feature/test");
        assert_eq!(feature.get_table_version("users"), Some(5));
        assert_eq!(feature.parent_branch, Some("main".to_string()));
    }

    #[test]
    fn test_branch_diff_identical() {
        let mut head = HashMap::new();
        head.insert("users".to_string(), 1);

        let branch_a = Branch::new("a", head.clone());
        let branch_b = Branch::new("b", head);

        let diff = BranchDiff::compute(&branch_a, &branch_b);

        assert_eq!(diff.unchanged, vec!["users"]);
        assert!(diff.modified.is_empty());
        assert!(diff.added_in_source.is_empty());
        assert!(diff.added_in_target.is_empty());
        assert!(!diff.has_conflicts);
    }

    #[test]
    fn test_branch_diff_modified() {
        let mut head_a = HashMap::new();
        head_a.insert("users".to_string(), 2);

        let mut head_b = HashMap::new();
        head_b.insert("users".to_string(), 1);

        let branch_a = Branch::new("a", head_a);
        let branch_b = Branch::new("b", head_b);

        let diff = BranchDiff::compute(&branch_a, &branch_b);

        assert!(diff.unchanged.is_empty());
        assert_eq!(diff.modified, vec![("users".to_string(), 2, 1)]);
        assert!(diff.has_conflicts);
    }

    #[test]
    fn test_branch_diff_added() {
        let mut head_a = HashMap::new();
        head_a.insert("users".to_string(), 1);
        head_a.insert("orders".to_string(), 1);

        let mut head_b = HashMap::new();
        head_b.insert("users".to_string(), 1);
        head_b.insert("products".to_string(), 1);

        let branch_a = Branch::new("a", head_a);
        let branch_b = Branch::new("b", head_b);

        let diff = BranchDiff::compute(&branch_a, &branch_b);

        assert_eq!(diff.unchanged, vec!["users"]);
        assert_eq!(diff.added_in_source, vec![("orders".to_string(), 1)]);
        assert_eq!(diff.added_in_target, vec![("products".to_string(), 1)]);
        assert!(!diff.has_conflicts);
    }
}
