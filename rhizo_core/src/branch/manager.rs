use std::collections::HashMap;
use std::fs;
use std::path::{Path, PathBuf};

use super::branch::{Branch, BranchDiff};
use super::error::BranchError;

const DEFAULT_BRANCH: &str = "main";
const BRANCHES_DIR: &str = "_branches";
const DEFAULT_FILE: &str = "_default.txt";

/// Manages branches for UDR tables.
///
/// Branches are stored as JSON files in a `_branches` subdirectory.
/// The `_default.txt` file contains the name of the default branch.
///
/// Branch names with slashes (e.g., "feature/test") are stored with
/// slashes converted to double underscores (e.g., "feature__test.json").
pub struct BranchManager {
    base_path: PathBuf,
}

impl BranchManager {
    /// Create a new BranchManager.
    ///
    /// Creates the branches directory if it doesn't exist.
    /// Creates a default "main" branch if no branches exist.
    pub fn new(base_path: impl AsRef<Path>) -> Result<Self, BranchError> {
        let base_path = base_path.as_ref().to_path_buf();
        let branches_dir = base_path.join(BRANCHES_DIR);
        fs::create_dir_all(&branches_dir)?;

        let manager = Self { base_path };

        // Create main branch if it doesn't exist
        if manager.list()?.is_empty() {
            let main = Branch::new(DEFAULT_BRANCH, HashMap::new());
            manager.save_branch(&main)?;
            manager.set_default(DEFAULT_BRANCH)?;
        }

        Ok(manager)
    }

    /// Create a new branch from an existing branch.
    ///
    /// If `from_branch` is None, creates from the default branch.
    /// This is a zero-copy operation - only pointers are copied.
    pub fn create(
        &self,
        name: &str,
        from_branch: Option<&str>,
        description: Option<&str>,
    ) -> Result<Branch, BranchError> {
        // Validate branch name
        self.validate_branch_name(name)?;

        // Check if branch already exists
        if self.branch_exists(name) {
            return Err(BranchError::BranchAlreadyExists(name.to_string()));
        }

        // Get source branch
        let from_name = match from_branch {
            Some(name) => name.to_string(),
            None => self.get_default()?.unwrap_or_else(|| DEFAULT_BRANCH.to_string()),
        };
        let source = self.get(&from_name)?;

        // Create new branch (zero-copy - only copies the HashMap)
        let mut branch = Branch::from_branch(name, &source);
        if let Some(desc) = description {
            branch = branch.with_description(desc);
        }

        // Save the branch
        self.save_branch(&branch)?;

        Ok(branch)
    }

    /// Get a branch by name.
    pub fn get(&self, name: &str) -> Result<Branch, BranchError> {
        let path = self.branch_path(name);
        if !path.exists() {
            return Err(BranchError::BranchNotFound(name.to_string()));
        }

        let json = fs::read_to_string(&path)?;
        let branch: Branch = serde_json::from_str(&json)?;
        Ok(branch)
    }

    /// List all branch names.
    pub fn list(&self) -> Result<Vec<String>, BranchError> {
        let branches_dir = self.base_path.join(BRANCHES_DIR);
        if !branches_dir.exists() {
            return Ok(Vec::new());
        }

        let mut branches = Vec::new();
        for entry in fs::read_dir(&branches_dir)? {
            let entry = entry?;
            let path = entry.path();

            if let Some(ext) = path.extension() {
                if ext == "json" {
                    if let Some(stem) = path.file_stem() {
                        let filename = stem.to_string_lossy();
                        // Convert __ back to /
                        let branch_name = filename.replace("__", "/");
                        branches.push(branch_name);
                    }
                }
            }
        }

        branches.sort();
        Ok(branches)
    }

    /// Delete a branch.
    ///
    /// Cannot delete the default branch.
    pub fn delete(&self, name: &str) -> Result<(), BranchError> {
        // Check if it's the default branch
        if let Some(default) = self.get_default()? {
            if default == name {
                return Err(BranchError::CannotDeleteDefault(name.to_string()));
            }
        }

        let path = self.branch_path(name);
        if !path.exists() {
            return Err(BranchError::BranchNotFound(name.to_string()));
        }

        fs::remove_file(&path)?;
        Ok(())
    }

    /// Update the head pointer for a table on a branch.
    pub fn update_head(
        &self,
        branch_name: &str,
        table_name: &str,
        version: u64,
    ) -> Result<(), BranchError> {
        let mut branch = self.get(branch_name)?;
        branch.set_table_version(table_name, version);
        self.save_branch(&branch)?;
        Ok(())
    }

    /// Get the version of a table on a branch.
    ///
    /// Returns None if the table doesn't exist on this branch.
    pub fn get_table_version(
        &self,
        branch_name: &str,
        table_name: &str,
    ) -> Result<Option<u64>, BranchError> {
        let branch = self.get(branch_name)?;
        Ok(branch.get_table_version(table_name))
    }

    /// Compare two branches using three-way merge when fork point is available.
    pub fn diff(&self, source: &str, target: &str) -> Result<BranchDiff, BranchError> {
        let source_branch = self.get(source)?;
        let target_branch = self.get(target)?;
        Ok(BranchDiff::compute(&source_branch, &target_branch))
    }

    /// Check if a merge is possible (no true conflicts after three-way analysis).
    pub fn can_fast_forward(&self, source: &str, target: &str) -> Result<bool, BranchError> {
        let diff = self.diff(source, target)?;
        Ok(!diff.has_conflicts)
    }

    /// Merge source branch into target branch.
    ///
    /// Uses three-way merge when a fork point is available:
    /// - Source-only changes are applied to target automatically
    /// - Target-only changes are preserved automatically
    /// - Added tables from source are included
    /// - True conflicts (both changed same table) cause an error
    ///
    /// Without a fork point, uses safe forward-only merge: source tables
    /// are applied only when they are new to the target or have a higher
    /// version. Target-only tables and higher target versions are preserved.
    pub fn merge_fast_forward(&self, source: &str, into: &str) -> Result<(), BranchError> {
        let source_branch = self.get(source)?;
        let mut target_branch = self.get(into)?;

        if source_branch.fork_point.is_some() {
            // Three-way merge: use diff to classify changes
            let diff = self.diff(source, into)?;

            if diff.has_conflicts {
                let conflicting = diff.conflicting_tables();
                return Err(BranchError::MergeConflict(conflicting));
            }

            // Apply source-only changes (source diverged from base, target didn't)
            for (table, src_version, _) in &diff.source_only_changes {
                target_branch.set_table_version(table, *src_version);
            }

            // Target-only changes: target already has the right version, nothing to do

            // Apply tables added only in source
            for (table, version) in &diff.added_in_source {
                target_branch.set_table_version(table, *version);
            }
        } else {
            // No fork point: safe forward-only merge.
            // Apply source tables only when new or higher version.
            // Never regress a target table version.
            for (table, &src_version) in &source_branch.head {
                let tgt_version = target_branch.get_table_version(table).unwrap_or(0);
                if src_version > tgt_version {
                    target_branch.set_table_version(table, src_version);
                }
            }
        }

        self.save_branch(&target_branch)?;

        Ok(())
    }

    /// Get the default branch name.
    pub fn get_default(&self) -> Result<Option<String>, BranchError> {
        let path = self.base_path.join(BRANCHES_DIR).join(DEFAULT_FILE);
        if !path.exists() {
            return Ok(None);
        }
        let name = fs::read_to_string(&path)?.trim().to_string();
        Ok(Some(name))
    }

    /// Set the default branch.
    pub fn set_default(&self, name: &str) -> Result<(), BranchError> {
        // Verify branch exists
        if !self.branch_exists(name) {
            return Err(BranchError::BranchNotFound(name.to_string()));
        }

        let path = self.base_path.join(BRANCHES_DIR).join(DEFAULT_FILE);
        let temp_path = path.with_extension("tmp");

        fs::write(&temp_path, name)?;
        fs::rename(&temp_path, &path)?;

        Ok(())
    }

    // --- Private helpers ---

    fn branch_path(&self, name: &str) -> PathBuf {
        // Convert slashes to double underscores for filesystem safety
        let safe_name = name.replace("/", "__");
        self.base_path
            .join(BRANCHES_DIR)
            .join(format!("{}.json", safe_name))
    }

    fn branch_exists(&self, name: &str) -> bool {
        self.branch_path(name).exists()
    }

    fn save_branch(&self, branch: &Branch) -> Result<(), BranchError> {
        let path = self.branch_path(&branch.name);
        let temp_path = path.with_extension("json.tmp");

        let json = serde_json::to_string_pretty(branch)?;
        fs::write(&temp_path, &json)?;
        fs::rename(&temp_path, &path)?;

        Ok(())
    }

    fn validate_branch_name(&self, name: &str) -> Result<(), BranchError> {
        if name.is_empty() {
            return Err(BranchError::InvalidBranchName(
                "Branch name cannot be empty".to_string(),
            ));
        }

        if name.starts_with('_') {
            return Err(BranchError::InvalidBranchName(
                "Branch name cannot start with underscore".to_string(),
            ));
        }

        // Allow alphanumeric, hyphens, underscores, and slashes
        let valid = name.chars().all(|c| {
            c.is_alphanumeric() || c == '-' || c == '_' || c == '/'
        });

        if !valid {
            return Err(BranchError::InvalidBranchName(format!(
                "Branch name contains invalid characters: {}",
                name
            )));
        }

        // Don't allow double slashes
        if name.contains("//") {
            return Err(BranchError::InvalidBranchName(
                "Branch name cannot contain double slashes".to_string(),
            ));
        }

        // Don't allow double underscores — they collide with the filesystem
        // encoding of slashes (e.g., "feature/test" → "feature__test.json",
        // so "feature__test" would map to the same file).
        if name.contains("__") {
            return Err(BranchError::InvalidBranchName(
                "Branch name cannot contain double underscores (reserved for path encoding)".to_string(),
            ));
        }

        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::env;

    fn temp_dir() -> PathBuf {
        env::temp_dir().join(format!("udr_branch_test_{}", uuid::Uuid::new_v4()))
    }

    #[test]
    fn test_new_creates_main_branch() {
        let dir = temp_dir();
        let manager = BranchManager::new(&dir).unwrap();

        let branches = manager.list().unwrap();
        assert!(branches.contains(&"main".to_string()));

        let main = manager.get("main").unwrap();
        assert_eq!(main.name, "main");
        assert!(main.head.is_empty());

        fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn test_create_branch() {
        let dir = temp_dir();
        let manager = BranchManager::new(&dir).unwrap();

        // Add a table to main
        manager.update_head("main", "users", 1).unwrap();

        // Create feature branch
        let feature = manager
            .create("feature/test", Some("main"), Some("Test branch"))
            .unwrap();

        assert_eq!(feature.name, "feature/test");
        assert_eq!(feature.get_table_version("users"), Some(1));
        assert_eq!(feature.parent_branch, Some("main".to_string()));
        assert_eq!(feature.description, Some("Test branch".to_string()));

        fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn test_create_branch_already_exists() {
        let dir = temp_dir();
        let manager = BranchManager::new(&dir).unwrap();

        manager.create("feature", None, None).unwrap();
        let result = manager.create("feature", None, None);

        assert!(matches!(result, Err(BranchError::BranchAlreadyExists(_))));

        fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn test_branch_not_found() {
        let dir = temp_dir();
        let manager = BranchManager::new(&dir).unwrap();

        let result = manager.get("nonexistent");
        assert!(matches!(result, Err(BranchError::BranchNotFound(_))));

        fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn test_list_branches() {
        let dir = temp_dir();
        let manager = BranchManager::new(&dir).unwrap();

        manager.create("alpha", None, None).unwrap();
        manager.create("beta", None, None).unwrap();
        manager.create("feature/gamma", None, None).unwrap();

        let branches = manager.list().unwrap();
        assert!(branches.contains(&"main".to_string()));
        assert!(branches.contains(&"alpha".to_string()));
        assert!(branches.contains(&"beta".to_string()));
        assert!(branches.contains(&"feature/gamma".to_string()));

        fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn test_delete_branch() {
        let dir = temp_dir();
        let manager = BranchManager::new(&dir).unwrap();

        manager.create("feature", None, None).unwrap();
        assert!(manager.list().unwrap().contains(&"feature".to_string()));

        manager.delete("feature").unwrap();
        assert!(!manager.list().unwrap().contains(&"feature".to_string()));

        fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn test_cannot_delete_default_branch() {
        let dir = temp_dir();
        let manager = BranchManager::new(&dir).unwrap();

        let result = manager.delete("main");
        assert!(matches!(result, Err(BranchError::CannotDeleteDefault(_))));

        fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn test_update_head() {
        let dir = temp_dir();
        let manager = BranchManager::new(&dir).unwrap();

        manager.update_head("main", "users", 1).unwrap();
        manager.update_head("main", "users", 2).unwrap();
        manager.update_head("main", "orders", 1).unwrap();

        let main = manager.get("main").unwrap();
        assert_eq!(main.get_table_version("users"), Some(2));
        assert_eq!(main.get_table_version("orders"), Some(1));

        fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn test_diff_branches() {
        let dir = temp_dir();
        let manager = BranchManager::new(&dir).unwrap();

        // Setup main with users v1
        manager.update_head("main", "users", 1).unwrap();

        // Create feature and update users to v2
        manager.create("feature", Some("main"), None).unwrap();
        manager.update_head("feature", "users", 2).unwrap();
        manager.update_head("feature", "orders", 1).unwrap();

        let diff = manager.diff("feature", "main").unwrap();

        // With three-way merge: only feature changed users (source-only), not a conflict
        assert!(diff.source_only_changes.contains(&("users".to_string(), 2, 1)));
        assert!(diff.added_in_source.contains(&("orders".to_string(), 1)));
        assert!(!diff.has_conflicts); // source-only change, not a true conflict

        fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn test_fast_forward_merge() {
        let dir = temp_dir();
        let manager = BranchManager::new(&dir).unwrap();

        // Setup main with users v1
        manager.update_head("main", "users", 1).unwrap();

        // Create feature and add orders (no conflict with main)
        manager.create("feature", Some("main"), None).unwrap();
        manager.update_head("feature", "orders", 1).unwrap();

        // Should be able to fast-forward
        assert!(manager.can_fast_forward("feature", "main").unwrap());

        manager.merge_fast_forward("feature", "main").unwrap();

        // Main should now have orders
        let main = manager.get("main").unwrap();
        assert_eq!(main.get_table_version("users"), Some(1));
        assert_eq!(main.get_table_version("orders"), Some(1));

        fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn test_cannot_fast_forward_with_conflicts() {
        let dir = temp_dir();
        let manager = BranchManager::new(&dir).unwrap();

        // Setup main with users v1
        manager.update_head("main", "users", 1).unwrap();

        // Create feature and update users to v2
        manager.create("feature", Some("main"), None).unwrap();
        manager.update_head("feature", "users", 2).unwrap();

        // Also update users on main to v3 (divergence!)
        manager.update_head("main", "users", 3).unwrap();

        // Should NOT be able to fast-forward (true conflict — both changed users)
        assert!(!manager.can_fast_forward("feature", "main").unwrap());

        let result = manager.merge_fast_forward("feature", "main");
        assert!(matches!(result, Err(BranchError::MergeConflict(_))));

        fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn test_three_way_auto_merge() {
        // Three-way merge: source changes users, target changes orders
        // Both should be auto-resolved without conflict
        let dir = temp_dir();
        let manager = BranchManager::new(&dir).unwrap();

        // Setup main with users=1 and orders=1
        manager.update_head("main", "users", 1).unwrap();
        manager.update_head("main", "orders", 1).unwrap();

        // Create feature branch (fork point: users=1, orders=1)
        manager.create("feature", Some("main"), None).unwrap();

        // Feature changes users to v2
        manager.update_head("feature", "users", 2).unwrap();

        // Main changes orders to v2 (no conflict with feature's changes)
        manager.update_head("main", "orders", 2).unwrap();

        // Three-way merge should succeed
        assert!(manager.can_fast_forward("feature", "main").unwrap());
        manager.merge_fast_forward("feature", "main").unwrap();

        // After merge: main should have users=2 (from feature) and orders=2 (kept)
        let main = manager.get("main").unwrap();
        assert_eq!(main.get_table_version("users"), Some(2));
        assert_eq!(main.get_table_version("orders"), Some(2));

        fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn test_three_way_true_conflict_in_manager() {
        // Both branches change the same table — true conflict
        let dir = temp_dir();
        let manager = BranchManager::new(&dir).unwrap();

        manager.update_head("main", "users", 1).unwrap();

        // Create feature (fork point: users=1)
        manager.create("feature", Some("main"), None).unwrap();

        // Both change users
        manager.update_head("feature", "users", 2).unwrap();
        manager.update_head("main", "users", 3).unwrap();

        // Should fail — true conflict
        assert!(!manager.can_fast_forward("feature", "main").unwrap());
        let result = manager.merge_fast_forward("feature", "main");
        assert!(matches!(result, Err(BranchError::MergeConflict(_))));

        fs::remove_dir_all(&dir).ok();
    }

    // ============ No-fork-point merge safety tests (M7 fix) ============

    #[test]
    fn test_no_fork_point_merge_preserves_target_only_tables() {
        // Bug: no-fork-point fallback overwrote all source heads onto target,
        // destroying tables that only existed on target.
        let dir = temp_dir();
        let manager = BranchManager::new(&dir).unwrap();

        // Main has users=3 and orders=5
        manager.update_head("main", "users", 3).unwrap();
        manager.update_head("main", "orders", 5).unwrap();

        // Create a root branch (no fork point) with only users=1
        let mut legacy = Branch::new("legacy", HashMap::new());
        legacy.set_table_version("users", 1);
        assert!(legacy.fork_point.is_none());
        manager.save_branch(&legacy).unwrap();

        // Merge legacy into main — should NOT destroy orders
        manager.merge_fast_forward("legacy", "main").unwrap();

        let main = manager.get("main").unwrap();
        // orders must still be v5 (target-only, never touched by source)
        assert_eq!(main.get_table_version("orders"), Some(5));
        // users should stay v3 (target has higher version than source's v1)
        assert_eq!(main.get_table_version("users"), Some(3));

        fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn test_no_fork_point_merge_applies_higher_source_version() {
        // When source has a higher version, the merge should apply it.
        let dir = temp_dir();
        let manager = BranchManager::new(&dir).unwrap();

        manager.update_head("main", "users", 1).unwrap();

        // Legacy branch with users=5 (higher than main's v1)
        let mut legacy = Branch::new("legacy", HashMap::new());
        legacy.set_table_version("users", 5);
        manager.save_branch(&legacy).unwrap();

        manager.merge_fast_forward("legacy", "main").unwrap();

        let main = manager.get("main").unwrap();
        assert_eq!(main.get_table_version("users"), Some(5));

        fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn test_no_fork_point_merge_adds_new_tables() {
        // Source has a table that target doesn't — should be added.
        let dir = temp_dir();
        let manager = BranchManager::new(&dir).unwrap();

        manager.update_head("main", "users", 1).unwrap();

        let mut legacy = Branch::new("legacy", HashMap::new());
        legacy.set_table_version("metrics", 3);
        manager.save_branch(&legacy).unwrap();

        manager.merge_fast_forward("legacy", "main").unwrap();

        let main = manager.get("main").unwrap();
        assert_eq!(main.get_table_version("users"), Some(1)); // preserved
        assert_eq!(main.get_table_version("metrics"), Some(3)); // added

        fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn test_no_fork_point_never_regresses_version() {
        // Source has lower versions for multiple tables — none should regress.
        let dir = temp_dir();
        let manager = BranchManager::new(&dir).unwrap();

        manager.update_head("main", "users", 10).unwrap();
        manager.update_head("main", "orders", 8).unwrap();
        manager.update_head("main", "products", 5).unwrap();

        let mut legacy = Branch::new("legacy", HashMap::new());
        legacy.set_table_version("users", 3);    // lower
        legacy.set_table_version("orders", 12);   // higher
        legacy.set_table_version("products", 5);  // equal
        manager.save_branch(&legacy).unwrap();

        manager.merge_fast_forward("legacy", "main").unwrap();

        let main = manager.get("main").unwrap();
        assert_eq!(main.get_table_version("users"), Some(10));    // kept (higher)
        assert_eq!(main.get_table_version("orders"), Some(12));   // updated (source higher)
        assert_eq!(main.get_table_version("products"), Some(5));  // unchanged (equal)

        fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn test_fork_point_merge_unaffected_by_fix() {
        // Ensure normal three-way merge (with fork point) still works correctly.
        let dir = temp_dir();
        let manager = BranchManager::new(&dir).unwrap();

        // Main: users=1, orders=1
        manager.update_head("main", "users", 1).unwrap();
        manager.update_head("main", "orders", 1).unwrap();

        // Feature branch (has fork point from main)
        manager.create("feature", Some("main"), None).unwrap();

        // Feature changes users to v2
        manager.update_head("feature", "users", 2).unwrap();
        // Main changes orders to v2
        manager.update_head("main", "orders", 2).unwrap();

        // Three-way merge should auto-resolve
        manager.merge_fast_forward("feature", "main").unwrap();

        let main = manager.get("main").unwrap();
        assert_eq!(main.get_table_version("users"), Some(2));   // from feature
        assert_eq!(main.get_table_version("orders"), Some(2));  // preserved on main

        fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn test_invalid_branch_names() {
        let dir = temp_dir();
        let manager = BranchManager::new(&dir).unwrap();

        // Empty name
        assert!(matches!(
            manager.create("", None, None),
            Err(BranchError::InvalidBranchName(_))
        ));

        // Starts with underscore
        assert!(matches!(
            manager.create("_hidden", None, None),
            Err(BranchError::InvalidBranchName(_))
        ));

        // Double slashes
        assert!(matches!(
            manager.create("feature//test", None, None),
            Err(BranchError::InvalidBranchName(_))
        ));

        // Double underscores (would collide with slash encoding on filesystem)
        assert!(matches!(
            manager.create("feature__test", None, None),
            Err(BranchError::InvalidBranchName(_))
        ));

        // Valid names should work
        assert!(manager.create("feature/test-1", None, None).is_ok());
        assert!(manager.create("experiment_v2", None, None).is_ok());

        fs::remove_dir_all(&dir).ok();
    }
}
