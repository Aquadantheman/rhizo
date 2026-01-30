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

    /// Fork-point snapshot: the head pointers at the time this branch was
    /// created. Used as the common ancestor for three-way merge to distinguish
    /// "source changed" from "target changed" from "both changed".
    /// None for root branches or branches created before this field existed.
    #[serde(default)]
    pub fork_point: Option<HashMap<String, u64>>,
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
            fork_point: None, // Root branches have no fork point
        }
    }

    /// Create a branch from an existing branch (zero-copy).
    /// Only the head pointers are copied, not the underlying data.
    /// The parent's current head is saved as the fork-point for three-way merge.
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
            fork_point: Some(parent.head.clone()), // Snapshot for three-way merge
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

    /// Tables where both branches changed the same table to different versions
    /// (true conflicts requiring manual resolution):
    /// (table_name, source_version, target_version)
    pub modified: Vec<(String, u64, u64)>,

    /// Tables only in source branch: (table_name, version)
    pub added_in_source: Vec<(String, u64)>,

    /// Tables only in target branch: (table_name, version)
    pub added_in_target: Vec<(String, u64)>,

    /// Whether branches have diverged (both modified same table differently)
    pub has_conflicts: bool,

    /// Tables changed only on the source branch (auto-resolvable to source version):
    /// (table_name, source_version, target_version)
    #[serde(default)]
    pub source_only_changes: Vec<(String, u64, u64)>,

    /// Tables changed only on the target branch (auto-resolvable to target version):
    /// (table_name, source_version, target_version)
    #[serde(default)]
    pub target_only_changes: Vec<(String, u64, u64)>,
}

impl BranchDiff {
    /// Compute the diff between two branches using three-way merge when a
    /// fork point (common ancestor) is available.
    ///
    /// With a fork point, changes are classified as:
    /// - **source_only_changes**: source changed from base, target did not → auto-resolve to source
    /// - **target_only_changes**: target changed from base, source did not → auto-resolve to target
    /// - **modified** (true conflicts): both changed from base to different values
    ///
    /// Without a fork point (legacy branches), falls back to two-way comparison
    /// where any version difference is a conflict.
    pub fn compute(source: &Branch, target: &Branch) -> Self {
        // Use source's fork_point if available for three-way merge
        Self::compute_with_base(source, target, source.fork_point.as_ref())
    }

    /// Compute the diff with an explicit base (fork point) for three-way merge.
    pub fn compute_with_base(
        source: &Branch,
        target: &Branch,
        base: Option<&HashMap<String, u64>>,
    ) -> Self {
        let mut unchanged = Vec::new();
        let mut modified = Vec::new();
        let mut added_in_source = Vec::new();
        let mut added_in_target = Vec::new();
        let mut source_only_changes = Vec::new();
        let mut target_only_changes = Vec::new();

        // Check tables in source
        for (table, &src_version) in &source.head {
            match target.head.get(table) {
                Some(&tgt_version) if src_version == tgt_version => {
                    unchanged.push(table.clone());
                }
                Some(&tgt_version) => {
                    // Versions differ — use base to classify
                    if let Some(base_head) = base {
                        let base_version = base_head.get(table).copied();
                        match base_version {
                            Some(bv) if src_version != bv && tgt_version == bv => {
                                // Only source changed
                                source_only_changes.push((table.clone(), src_version, tgt_version));
                            }
                            Some(bv) if tgt_version != bv && src_version == bv => {
                                // Only target changed
                                target_only_changes.push((table.clone(), src_version, tgt_version));
                            }
                            _ => {
                                // Both changed (or table not in base) — true conflict
                                modified.push((table.clone(), src_version, tgt_version));
                            }
                        }
                    } else {
                        // No base available — fall back to two-way (all differences are conflicts)
                        modified.push((table.clone(), src_version, tgt_version));
                    }
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
        source_only_changes.sort_by(|a, b| a.0.cmp(&b.0));
        target_only_changes.sort_by(|a, b| a.0.cmp(&b.0));

        // True conflicts only when both branches modified the same table
        let has_conflicts = !modified.is_empty();

        Self {
            source_branch: source.name.clone(),
            target_branch: target.name.clone(),
            unchanged,
            modified,
            added_in_source,
            added_in_target,
            has_conflicts,
            source_only_changes,
            target_only_changes,
        }
    }

    /// Get the list of conflicting tables (tables modified on both branches).
    pub fn conflicting_tables(&self) -> Vec<String> {
        self.modified.iter().map(|(name, _, _)| name.clone()).collect()
    }

    /// Check if this diff can be auto-merged (no true conflicts).
    pub fn can_auto_merge(&self) -> bool {
        !self.has_conflicts
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
        // Fork point should snapshot parent's head
        assert!(feature.fork_point.is_some());
        assert_eq!(feature.fork_point.as_ref().unwrap().get("users"), Some(&5));
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
    fn test_branch_diff_modified_no_base() {
        // Without fork_point, falls back to two-way (all diffs are conflicts)
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
        assert!(diff.source_only_changes.is_empty());
        assert!(diff.target_only_changes.is_empty());
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

    #[test]
    fn test_three_way_source_only_change() {
        // Base: users=1, orders=1
        // Source (feature): users=2, orders=1  (changed users)
        // Target (main): users=1, orders=1     (unchanged)
        // Expected: source_only_changes for users, no conflicts
        let mut base = HashMap::new();
        base.insert("users".to_string(), 1);
        base.insert("orders".to_string(), 1);

        let main = Branch::new("main", base.clone());
        let mut feature = Branch::from_branch("feature", &main);
        feature.set_table_version("users", 2);

        let diff = BranchDiff::compute(&feature, &main);

        assert!(!diff.has_conflicts);
        assert!(diff.modified.is_empty());
        assert_eq!(diff.source_only_changes, vec![("users".to_string(), 2, 1)]);
        assert!(diff.target_only_changes.is_empty());
        assert_eq!(diff.unchanged, vec!["orders"]);
    }

    #[test]
    fn test_three_way_target_only_change() {
        // Base: users=1, orders=1
        // Source (feature): users=1, orders=1  (unchanged)
        // Target (main): users=3, orders=1     (changed users)
        // Expected: target_only_changes for users, no conflicts
        let mut base = HashMap::new();
        base.insert("users".to_string(), 1);
        base.insert("orders".to_string(), 1);

        let main = Branch::new("main", base.clone());
        let feature = Branch::from_branch("feature", &main);

        // Simulate main advancing after fork
        let mut main_advanced = main;
        main_advanced.set_table_version("users", 3);

        let diff = BranchDiff::compute(&feature, &main_advanced);

        assert!(!diff.has_conflicts);
        assert!(diff.modified.is_empty());
        assert!(diff.source_only_changes.is_empty());
        assert_eq!(diff.target_only_changes, vec![("users".to_string(), 1, 3)]);
        assert_eq!(diff.unchanged, vec!["orders"]);
    }

    #[test]
    fn test_three_way_true_conflict() {
        // Base: users=1
        // Source: users=2 (feature changed it)
        // Target: users=3 (main also changed it)
        // Expected: true conflict
        let mut base = HashMap::new();
        base.insert("users".to_string(), 1);

        let main = Branch::new("main", base.clone());
        let mut feature = Branch::from_branch("feature", &main);
        feature.set_table_version("users", 2);

        let mut main_advanced = main;
        main_advanced.set_table_version("users", 3);

        let diff = BranchDiff::compute(&feature, &main_advanced);

        assert!(diff.has_conflicts);
        assert_eq!(diff.modified, vec![("users".to_string(), 2, 3)]);
        assert!(diff.source_only_changes.is_empty());
        assert!(diff.target_only_changes.is_empty());
    }

    #[test]
    fn test_three_way_mixed_changes() {
        // Base: users=1, orders=1, products=1
        // Source: users=2, orders=1, products=3 (changed users and products)
        // Target: users=1, orders=2, products=4 (changed orders and products)
        // Expected: users=source_only, orders=target_only, products=conflict
        let mut base = HashMap::new();
        base.insert("users".to_string(), 1);
        base.insert("orders".to_string(), 1);
        base.insert("products".to_string(), 1);

        let main = Branch::new("main", base.clone());
        let mut feature = Branch::from_branch("feature", &main);
        feature.set_table_version("users", 2);
        feature.set_table_version("products", 3);

        let mut main_advanced = main;
        main_advanced.set_table_version("orders", 2);
        main_advanced.set_table_version("products", 4);

        let diff = BranchDiff::compute(&feature, &main_advanced);

        assert!(diff.has_conflicts);
        assert_eq!(diff.modified, vec![("products".to_string(), 3, 4)]);
        assert_eq!(diff.source_only_changes, vec![("users".to_string(), 2, 1)]);
        assert_eq!(diff.target_only_changes, vec![("orders".to_string(), 1, 2)]);
    }

    #[test]
    fn test_three_way_can_auto_merge() {
        // No true conflicts — only source-only and target-only changes
        let mut base = HashMap::new();
        base.insert("users".to_string(), 1);
        base.insert("orders".to_string(), 1);

        let main = Branch::new("main", base.clone());
        let mut feature = Branch::from_branch("feature", &main);
        feature.set_table_version("users", 2);

        let mut main_advanced = main;
        main_advanced.set_table_version("orders", 2);

        let diff = BranchDiff::compute(&feature, &main_advanced);

        assert!(diff.can_auto_merge());
        assert!(!diff.has_conflicts);
        assert_eq!(diff.source_only_changes.len(), 1);
        assert_eq!(diff.target_only_changes.len(), 1);
    }
}
