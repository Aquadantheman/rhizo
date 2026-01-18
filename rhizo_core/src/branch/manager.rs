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

    /// Compare two branches.
    pub fn diff(&self, source: &str, target: &str) -> Result<BranchDiff, BranchError> {
        let source_branch = self.get(source)?;
        let target_branch = self.get(target)?;
        Ok(BranchDiff::compute(&source_branch, &target_branch))
    }

    /// Check if a fast-forward merge is possible.
    ///
    /// Fast-forward is possible when target branch has not diverged from source,
    /// meaning all tables in target either don't exist in source or have the
    /// same version.
    pub fn can_fast_forward(&self, source: &str, target: &str) -> Result<bool, BranchError> {
        let diff = self.diff(source, target)?;
        // Can fast-forward if no modifications (divergence)
        Ok(!diff.has_conflicts)
    }

    /// Merge source branch into target branch (fast-forward only).
    ///
    /// This updates target to have the same head pointers as source.
    /// Fails if branches have diverged (both modified same table).
    pub fn merge_fast_forward(&self, source: &str, into: &str) -> Result<(), BranchError> {
        let diff = self.diff(source, into)?;

        if diff.has_conflicts {
            let conflicting = diff.conflicting_tables();
            return Err(BranchError::MergeConflict(conflicting));
        }

        // Get both branches
        let source_branch = self.get(source)?;
        let mut target_branch = self.get(into)?;

        // Update target with all source head pointers
        for (table, version) in &source_branch.head {
            target_branch.set_table_version(table, *version);
        }

        // Save updated target
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

        assert!(diff.modified.contains(&("users".to_string(), 2, 1)));
        assert!(diff.added_in_source.contains(&("orders".to_string(), 1)));
        assert!(diff.has_conflicts); // users was modified

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

        // Should NOT be able to fast-forward
        assert!(!manager.can_fast_forward("feature", "main").unwrap());

        let result = manager.merge_fast_forward("feature", "main");
        assert!(matches!(result, Err(BranchError::MergeConflict(_))));

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

        // Valid names should work
        assert!(manager.create("feature/test-1", None, None).is_ok());
        assert!(manager.create("experiment_v2", None, None).is_ok());

        fs::remove_dir_all(&dir).ok();
    }
}
