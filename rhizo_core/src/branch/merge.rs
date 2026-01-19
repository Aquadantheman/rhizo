//! Algebraic merge support for branch operations.
//!
//! This module extends the branch manager with algebraic merge capabilities,
//! enabling automatic conflict resolution for tables with algebraic schemas.

use super::branch::BranchDiff;
#[cfg(test)]
use crate::algebraic::TableAlgebraicSchema;
use crate::algebraic::AlgebraicSchemaRegistry;
#[cfg(test)]
use crate::algebraic::OpType;
use serde::{Deserialize, Serialize};
use std::collections::HashMap;

/// Result of analyzing merge compatibility.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct MergeAnalysis {
    /// Tables that can be auto-merged (all columns conflict-free)
    pub auto_mergeable: Vec<String>,

    /// Tables that cannot be auto-merged (have conflicting columns)
    pub conflicting: Vec<String>,

    /// Tables only modified in source (no conflict)
    pub source_only: Vec<String>,

    /// Tables only modified in target (no conflict)
    pub target_only: Vec<String>,

    /// Tables with same version in both (no merge needed)
    pub unchanged: Vec<String>,
}

impl MergeAnalysis {
    /// Check if merge can proceed without conflicts.
    pub fn can_merge(&self) -> bool {
        self.conflicting.is_empty()
    }

    /// Get all tables that need merging.
    pub fn tables_to_merge(&self) -> Vec<&str> {
        self.auto_mergeable
            .iter()
            .chain(self.source_only.iter())
            .chain(self.target_only.iter())
            .map(|s| s.as_str())
            .collect()
    }
}

/// Outcome of an algebraic merge operation.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct MergeOutcome {
    /// Source branch name
    pub source_branch: String,

    /// Target branch name
    pub target_branch: String,

    /// Tables that were fast-forwarded (no conflict)
    pub fast_forwarded: Vec<String>,

    /// Tables that were algebraically merged
    pub algebraically_merged: Vec<String>,

    /// Tables that conflicted (if any)
    pub conflicts: Vec<String>,

    /// Whether the merge was successful
    pub success: bool,

    /// Optional description of what happened
    pub description: Option<String>,
}

impl MergeOutcome {
    /// Create a successful merge outcome.
    pub fn success(
        source: impl Into<String>,
        target: impl Into<String>,
        fast_forwarded: Vec<String>,
        algebraically_merged: Vec<String>,
    ) -> Self {
        Self {
            source_branch: source.into(),
            target_branch: target.into(),
            fast_forwarded,
            algebraically_merged,
            conflicts: Vec::new(),
            success: true,
            description: None,
        }
    }

    /// Create a failed merge outcome.
    pub fn conflict(
        source: impl Into<String>,
        target: impl Into<String>,
        conflicts: Vec<String>,
    ) -> Self {
        Self {
            source_branch: source.into(),
            target_branch: target.into(),
            fast_forwarded: Vec::new(),
            algebraically_merged: Vec::new(),
            conflicts,
            success: false,
            description: Some("Merge failed due to conflicts".to_string()),
        }
    }

    /// Add a description to the outcome.
    pub fn with_description(mut self, desc: impl Into<String>) -> Self {
        self.description = Some(desc.into());
        self
    }
}

/// Analyzer for algebraic merge compatibility.
///
/// This determines which tables can be auto-merged based on their
/// algebraic schemas and the nature of the changes.
pub struct MergeAnalyzer<'a> {
    registry: &'a AlgebraicSchemaRegistry,
}

impl<'a> MergeAnalyzer<'a> {
    /// Create a new merge analyzer with the given schema registry.
    pub fn new(registry: &'a AlgebraicSchemaRegistry) -> Self {
        Self { registry }
    }

    /// Analyze a branch diff to determine merge compatibility.
    ///
    /// # Arguments
    /// * `diff` - The diff between source and target branches
    ///
    /// # Returns
    /// Analysis indicating which tables can be auto-merged
    pub fn analyze(&self, diff: &BranchDiff) -> MergeAnalysis {
        let mut auto_mergeable = Vec::new();
        let mut conflicting = Vec::new();

        // Analyze modified tables (potential conflicts)
        for (table, _src_ver, _tgt_ver) in &diff.modified {
            if self.can_auto_merge_table(table) {
                auto_mergeable.push(table.clone());
            } else {
                conflicting.push(table.clone());
            }
        }

        MergeAnalysis {
            auto_mergeable,
            conflicting,
            source_only: diff.added_in_source.iter().map(|(t, _)| t.clone()).collect(),
            target_only: diff.added_in_target.iter().map(|(t, _)| t.clone()).collect(),
            unchanged: diff.unchanged.clone(),
        }
    }

    /// Check if a table can be auto-merged based on its schema.
    ///
    /// A table can be auto-merged if:
    /// 1. It has a registered algebraic schema, AND
    /// 2. The schema's default operation is conflict-free, OR
    /// 3. All modified columns have conflict-free operations
    fn can_auto_merge_table(&self, table: &str) -> bool {
        match self.registry.get(table) {
            Some(schema) => schema.is_fully_conflict_free(),
            None => false, // No schema = cannot auto-merge
        }
    }

    /// Analyze merge with detailed column information.
    ///
    /// Returns a map of table -> (auto_merge_columns, conflict_columns)
    pub fn analyze_columns(
        &self,
        diff: &BranchDiff,
    ) -> HashMap<String, (Vec<String>, Vec<String>)> {
        let mut result = HashMap::new();

        for (table, _, _) in &diff.modified {
            let (auto_cols, conflict_cols) = self.analyze_table_columns(table);
            result.insert(table.clone(), (auto_cols, conflict_cols));
        }

        result
    }

    /// Analyze columns within a single table.
    fn analyze_table_columns(&self, table: &str) -> (Vec<String>, Vec<String>) {
        match self.registry.get(table) {
            Some(schema) => {
                let auto_cols = schema.conflict_free_columns()
                    .into_iter()
                    .map(|s| s.to_string())
                    .collect();
                let conflict_cols = schema.conflicting_columns()
                    .into_iter()
                    .map(|s| s.to_string())
                    .collect();
                (auto_cols, conflict_cols)
            }
            None => (Vec::new(), Vec::new()),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::branch::Branch;
    use std::collections::HashMap;

    fn create_test_registry() -> AlgebraicSchemaRegistry {
        let mut registry = AlgebraicSchemaRegistry::new();

        // Table with all conflict-free columns
        let mut counters = TableAlgebraicSchema::new("counters");
        counters.set_default(OpType::AbelianAdd);
        registry.register(counters);

        // Table with mixed columns
        let mut users = TableAlgebraicSchema::new("users");
        users.add_column("login_count", OpType::AbelianAdd);
        users.add_column("last_login", OpType::SemilatticeMax);
        users.add_column("name", OpType::GenericOverwrite);
        registry.register(users);

        // Table with all conflict columns
        let mut settings = TableAlgebraicSchema::new("settings");
        settings.set_default(OpType::GenericOverwrite);
        registry.register(settings);

        registry
    }

    fn create_test_diff() -> BranchDiff {
        let mut source_head = HashMap::new();
        source_head.insert("counters".to_string(), 2);
        source_head.insert("users".to_string(), 2);
        source_head.insert("settings".to_string(), 2);
        source_head.insert("new_table".to_string(), 1);
        let source = Branch::new("feature", source_head);

        let mut target_head = HashMap::new();
        target_head.insert("counters".to_string(), 1);
        target_head.insert("users".to_string(), 1);
        target_head.insert("settings".to_string(), 1);
        target_head.insert("other_table".to_string(), 1);
        let target = Branch::new("main", target_head);

        BranchDiff::compute(&source, &target)
    }

    #[test]
    fn test_merge_analysis_counters_auto_mergeable() {
        let registry = create_test_registry();
        let analyzer = MergeAnalyzer::new(&registry);
        let diff = create_test_diff();

        let analysis = analyzer.analyze(&diff);

        assert!(analysis.auto_mergeable.contains(&"counters".to_string()));
        assert!(!analysis.conflicting.contains(&"counters".to_string()));
    }

    #[test]
    fn test_merge_analysis_users_has_conflicts() {
        let registry = create_test_registry();
        let analyzer = MergeAnalyzer::new(&registry);
        let diff = create_test_diff();

        let analysis = analyzer.analyze(&diff);

        // users has mixed columns, so not fully conflict-free
        assert!(!analysis.auto_mergeable.contains(&"users".to_string()));
        assert!(analysis.conflicting.contains(&"users".to_string()));
    }

    #[test]
    fn test_merge_analysis_settings_conflicts() {
        let registry = create_test_registry();
        let analyzer = MergeAnalyzer::new(&registry);
        let diff = create_test_diff();

        let analysis = analyzer.analyze(&diff);

        assert!(!analysis.auto_mergeable.contains(&"settings".to_string()));
        assert!(analysis.conflicting.contains(&"settings".to_string()));
    }

    #[test]
    fn test_merge_analysis_source_only() {
        let registry = create_test_registry();
        let analyzer = MergeAnalyzer::new(&registry);
        let diff = create_test_diff();

        let analysis = analyzer.analyze(&diff);

        assert!(analysis.source_only.contains(&"new_table".to_string()));
    }

    #[test]
    fn test_merge_analysis_target_only() {
        let registry = create_test_registry();
        let analyzer = MergeAnalyzer::new(&registry);
        let diff = create_test_diff();

        let analysis = analyzer.analyze(&diff);

        assert!(analysis.target_only.contains(&"other_table".to_string()));
    }

    #[test]
    fn test_merge_analysis_can_merge() {
        let registry = create_test_registry();
        let analyzer = MergeAnalyzer::new(&registry);
        let diff = create_test_diff();

        let analysis = analyzer.analyze(&diff);

        // Cannot merge because users and settings conflict
        assert!(!analysis.can_merge());
    }

    #[test]
    fn test_merge_analysis_columns() {
        let registry = create_test_registry();
        let analyzer = MergeAnalyzer::new(&registry);
        let diff = create_test_diff();

        let columns = analyzer.analyze_columns(&diff);

        // Check users columns
        let (auto_cols, conflict_cols) = columns.get("users").unwrap();
        assert!(auto_cols.contains(&"login_count".to_string()));
        assert!(auto_cols.contains(&"last_login".to_string()));
        assert!(conflict_cols.contains(&"name".to_string()));
    }

    #[test]
    fn test_merge_outcome_success() {
        let outcome = MergeOutcome::success(
            "feature",
            "main",
            vec!["table1".to_string()],
            vec!["counters".to_string()],
        );

        assert!(outcome.success);
        assert!(outcome.conflicts.is_empty());
        assert_eq!(outcome.fast_forwarded, vec!["table1"]);
        assert_eq!(outcome.algebraically_merged, vec!["counters"]);
    }

    #[test]
    fn test_merge_outcome_conflict() {
        let outcome = MergeOutcome::conflict(
            "feature",
            "main",
            vec!["users".to_string()],
        );

        assert!(!outcome.success);
        assert_eq!(outcome.conflicts, vec!["users"]);
    }

    #[test]
    fn test_unregistered_table_cannot_merge() {
        let registry = AlgebraicSchemaRegistry::new(); // Empty registry
        let analyzer = MergeAnalyzer::new(&registry);

        assert!(!analyzer.can_auto_merge_table("any_table"));
    }
}
