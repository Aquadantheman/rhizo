//! Integration tests for algebraic merge classification.
//!
//! These tests verify the complete workflow from schema definition
//! through branch merge analysis.

use rhizo_core::{
    AlgebraicMerger, AlgebraicSchemaRegistry, AlgebraicValue, Branch, BranchDiff, MergeAnalyzer,
    MergeOutcome, MergeResult, OpType, TableAlgebraicSchema,
};
use std::collections::HashMap;

// ============================================================================
// Full Workflow Tests
// ============================================================================

/// Test complete inventory scenario with multiple algebraic types.
#[test]
fn test_inventory_workflow() {
    // 1. Define schema with multiple operation types
    let mut schema = TableAlgebraicSchema::new("inventory");
    schema.add_column("quantity", OpType::AbelianAdd);
    schema.add_column("last_updated", OpType::SemilatticeMax);
    schema.add_column("tags", OpType::SemilatticeUnion);
    schema.add_column("min_price", OpType::SemilatticeMin);
    schema.add_column("name", OpType::GenericOverwrite);

    // 2. Register schema
    let mut registry = AlgebraicSchemaRegistry::new();
    registry.register(schema.clone());

    // 3. Verify schema registration
    assert!(registry.get("inventory").is_some());
    assert_eq!(
        registry.get_op_type("inventory", "quantity"),
        OpType::AbelianAdd
    );

    // 4. Simulate concurrent updates from two branches
    // Branch A: quantity += 10, last_updated = 1000, tags += ["featured"]
    // Branch B: quantity += 5, last_updated = 1500, tags += ["sale"]

    let merged_quantity = AlgebraicMerger::merge(
        OpType::AbelianAdd,
        &AlgebraicValue::integer(10),
        &AlgebraicValue::integer(5),
    );
    assert_eq!(merged_quantity.unwrap(), AlgebraicValue::integer(15));

    let merged_timestamp = AlgebraicMerger::merge(
        OpType::SemilatticeMax,
        &AlgebraicValue::integer(1000),
        &AlgebraicValue::integer(1500),
    );
    assert_eq!(merged_timestamp.unwrap(), AlgebraicValue::integer(1500));

    let merged_tags = AlgebraicMerger::merge(
        OpType::SemilatticeUnion,
        &AlgebraicValue::string_set(["featured"]),
        &AlgebraicValue::string_set(["sale"]),
    );
    if let AlgebraicValue::StringSet(tags) = merged_tags.unwrap() {
        assert_eq!(tags.len(), 2);
        assert!(tags.contains("featured"));
        assert!(tags.contains("sale"));
    } else {
        panic!("Expected StringSet");
    }

    // 5. Verify conflict-free and conflicting columns
    assert!(schema.can_auto_merge(&["quantity", "last_updated", "tags"]));
    assert!(!schema.can_auto_merge(&["name"]));
}

/// Test user metrics scenario with counters and timestamps.
#[test]
fn test_user_metrics_workflow() {
    let mut schema = TableAlgebraicSchema::new("user_metrics");
    schema.set_default(OpType::AbelianAdd); // Default to conflict-free
    schema.add_column("login_count", OpType::AbelianAdd);
    schema.add_column("error_count", OpType::AbelianAdd);
    schema.add_column("last_login", OpType::SemilatticeMax);
    schema.add_column("first_seen", OpType::SemilatticeMin);
    schema.add_column("roles", OpType::SemilatticeUnion);

    let mut registry = AlgebraicSchemaRegistry::new();
    registry.register(schema.clone());

    // All columns should be conflict-free (default + explicit all conflict-free)
    assert!(schema.is_fully_conflict_free());

    // Concurrent logins from different locations:
    // Server A: login_count += 3
    // Server B: login_count += 2
    let merged = AlgebraicMerger::merge(
        OpType::AbelianAdd,
        &AlgebraicValue::integer(3),
        &AlgebraicValue::integer(2),
    );
    assert_eq!(merged.unwrap(), AlgebraicValue::integer(5));

    // Role accumulation: {"admin"} ∪ {"viewer"} = {"admin", "viewer"}
    let merged_roles = AlgebraicMerger::merge(
        OpType::SemilatticeUnion,
        &AlgebraicValue::string_set(["admin"]),
        &AlgebraicValue::string_set(["viewer"]),
    );
    if let AlgebraicValue::StringSet(roles) = merged_roles.unwrap() {
        assert!(roles.contains("admin"));
        assert!(roles.contains("viewer"));
    }
}

// ============================================================================
// Branch Merge Analysis Tests
// ============================================================================

fn create_branch_with_tables(name: &str, tables: &[(&str, u64)]) -> Branch {
    let mut head = HashMap::new();
    for (table, version) in tables {
        head.insert(table.to_string(), *version);
    }
    Branch::new(name, head)
}

#[test]
fn test_merge_analyzer_auto_mergeable() {
    // Setup: counters table with all conflict-free operations
    let mut counters = TableAlgebraicSchema::new("counters");
    counters.set_default(OpType::AbelianAdd);

    let mut registry = AlgebraicSchemaRegistry::new();
    registry.register(counters);

    // Create branches with diverged counters
    let source = create_branch_with_tables("feature", &[("counters", 2)]);
    let target = create_branch_with_tables("main", &[("counters", 1)]);

    let diff = BranchDiff::compute(&source, &target);
    let analyzer = MergeAnalyzer::new(&registry);
    let analysis = analyzer.analyze(&diff);

    assert!(analysis.auto_mergeable.contains(&"counters".to_string()));
    assert!(analysis.can_merge());
}

#[test]
fn test_merge_analyzer_conflicting() {
    // Setup: settings table with overwrite semantics
    let mut settings = TableAlgebraicSchema::new("settings");
    settings.set_default(OpType::GenericOverwrite);

    let mut registry = AlgebraicSchemaRegistry::new();
    registry.register(settings);

    let source = create_branch_with_tables("feature", &[("settings", 2)]);
    let target = create_branch_with_tables("main", &[("settings", 1)]);

    let diff = BranchDiff::compute(&source, &target);
    let analyzer = MergeAnalyzer::new(&registry);
    let analysis = analyzer.analyze(&diff);

    assert!(analysis.conflicting.contains(&"settings".to_string()));
    assert!(!analysis.can_merge());
}

#[test]
fn test_merge_analyzer_mixed_tables() {
    let mut registry = AlgebraicSchemaRegistry::new();

    // Counters: conflict-free
    let mut counters = TableAlgebraicSchema::new("counters");
    counters.set_default(OpType::AbelianAdd);
    registry.register(counters);

    // Settings: not conflict-free
    let mut settings = TableAlgebraicSchema::new("settings");
    settings.set_default(OpType::GenericOverwrite);
    registry.register(settings);

    // Branches that modify both tables
    let source = create_branch_with_tables("feature", &[("counters", 2), ("settings", 2)]);
    let target = create_branch_with_tables("main", &[("counters", 1), ("settings", 1)]);

    let diff = BranchDiff::compute(&source, &target);
    let analyzer = MergeAnalyzer::new(&registry);
    let analysis = analyzer.analyze(&diff);

    assert!(analysis.auto_mergeable.contains(&"counters".to_string()));
    assert!(analysis.conflicting.contains(&"settings".to_string()));
    assert!(!analysis.can_merge()); // Settings blocks merge
}

#[test]
fn test_merge_analyzer_source_only() {
    let registry = AlgebraicSchemaRegistry::new();

    // Source has new table that target doesn't have
    let source = create_branch_with_tables("feature", &[("new_feature", 1)]);
    let target = create_branch_with_tables("main", &[]);

    let diff = BranchDiff::compute(&source, &target);
    let analyzer = MergeAnalyzer::new(&registry);
    let analysis = analyzer.analyze(&diff);

    assert!(analysis.source_only.contains(&"new_feature".to_string()));
    assert!(analysis.can_merge()); // No conflicts
}

#[test]
fn test_merge_analyzer_column_analysis() {
    let mut users = TableAlgebraicSchema::new("users");
    users.add_column("login_count", OpType::AbelianAdd);
    users.add_column("last_login", OpType::SemilatticeMax);
    users.add_column("name", OpType::GenericOverwrite);

    let mut registry = AlgebraicSchemaRegistry::new();
    registry.register(users);

    let source = create_branch_with_tables("feature", &[("users", 2)]);
    let target = create_branch_with_tables("main", &[("users", 1)]);

    let diff = BranchDiff::compute(&source, &target);
    let analyzer = MergeAnalyzer::new(&registry);
    let columns = analyzer.analyze_columns(&diff);

    let (auto_cols, conflict_cols) = columns.get("users").unwrap();
    assert!(auto_cols.contains(&"login_count".to_string()));
    assert!(auto_cols.contains(&"last_login".to_string()));
    assert!(conflict_cols.contains(&"name".to_string()));
}

// ============================================================================
// Merge Outcome Tests
// ============================================================================

#[test]
fn test_merge_outcome_success() {
    let outcome = MergeOutcome::success(
        "feature",
        "main",
        vec!["table_a".to_string()],
        vec!["counters".to_string()],
    );

    assert!(outcome.success);
    assert!(outcome.conflicts.is_empty());
    assert_eq!(outcome.source_branch, "feature");
    assert_eq!(outcome.target_branch, "main");
    assert_eq!(outcome.fast_forwarded, vec!["table_a"]);
    assert_eq!(outcome.algebraically_merged, vec!["counters"]);
}

#[test]
fn test_merge_outcome_conflict() {
    let outcome = MergeOutcome::conflict(
        "feature",
        "main",
        vec!["settings".to_string()],
    );

    assert!(!outcome.success);
    assert_eq!(outcome.conflicts, vec!["settings"]);
    assert!(outcome.description.is_some());
}

#[test]
fn test_merge_outcome_with_description() {
    let outcome = MergeOutcome::success("a", "b", vec![], vec![])
        .with_description("Custom merge description");

    assert_eq!(
        outcome.description,
        Some("Custom merge description".to_string())
    );
}

// ============================================================================
// Mathematical Property Tests
// ============================================================================

/// Verify associativity: merge(merge(a,b), c) = merge(a, merge(b,c))
#[test]
fn test_associativity() {
    let a = AlgebraicValue::integer(10);
    let b = AlgebraicValue::integer(20);
    let c = AlgebraicValue::integer(30);

    for op in [
        OpType::AbelianAdd,
        OpType::AbelianMultiply,
        OpType::SemilatticeMax,
        OpType::SemilatticeMin,
    ] {
        let ab = AlgebraicMerger::merge(op, &a, &b).unwrap();
        let ab_c = AlgebraicMerger::merge(op, &ab, &c);

        let bc = AlgebraicMerger::merge(op, &b, &c).unwrap();
        let a_bc = AlgebraicMerger::merge(op, &a, &bc);

        assert_eq!(ab_c, a_bc, "{:?} should be associative", op);
    }
}

/// Verify set associativity
#[test]
fn test_set_associativity() {
    let a = AlgebraicValue::string_set(["1", "2"]);
    let b = AlgebraicValue::string_set(["2", "3"]);
    let c = AlgebraicValue::string_set(["3", "4"]);

    for op in [OpType::SemilatticeUnion, OpType::SemilatticeIntersect] {
        let ab = AlgebraicMerger::merge(op, &a, &b).unwrap();
        let ab_c = AlgebraicMerger::merge(op, &ab, &c);

        let bc = AlgebraicMerger::merge(op, &b, &c).unwrap();
        let a_bc = AlgebraicMerger::merge(op, &a, &bc);

        assert_eq!(ab_c, a_bc, "{:?} should be associative for sets", op);
    }
}

/// Verify identity elements
#[test]
fn test_identity_elements() {
    let v = AlgebraicValue::integer(42);

    // AbelianAdd: a + 0 = a
    let result = AlgebraicMerger::merge(OpType::AbelianAdd, &v, &AlgebraicValue::integer(0));
    assert_eq!(result.unwrap(), v);

    // AbelianMultiply: a * 1 = a
    let result = AlgebraicMerger::merge(OpType::AbelianMultiply, &v, &AlgebraicValue::integer(1));
    assert_eq!(result.unwrap(), v);

    // SemilatticeUnion: A ∪ {} = A
    let tags = AlgebraicValue::string_set(["a", "b"]);
    let empty: AlgebraicValue = AlgebraicValue::string_set::<[&str; 0], &str>([]);
    let result = AlgebraicMerger::merge(OpType::SemilatticeUnion, &tags, &empty);
    assert_eq!(result.unwrap(), tags);
}

// ============================================================================
// Edge Case Tests
// ============================================================================

#[test]
fn test_large_set_merge() {
    // Create large sets
    let v1: AlgebraicValue = AlgebraicValue::int_set(0..1000);
    let v2: AlgebraicValue = AlgebraicValue::int_set(500..1500);

    let result = AlgebraicMerger::merge(OpType::SemilatticeUnion, &v1, &v2);
    if let MergeResult::Merged(AlgebraicValue::IntSet(s)) = result {
        assert_eq!(s.len(), 1500); // 0-1499
    } else {
        panic!("Expected merged IntSet");
    }
}

#[test]
fn test_negative_counter_merge() {
    // Counters can go negative (decrements)
    let v1 = AlgebraicValue::integer(-10);
    let v2 = AlgebraicValue::integer(5);

    let result = AlgebraicMerger::merge(OpType::AbelianAdd, &v1, &v2);
    assert_eq!(result.unwrap(), AlgebraicValue::integer(-5));
}

#[test]
fn test_float_precision() {
    let v1 = AlgebraicValue::float(0.1);
    let v2 = AlgebraicValue::float(0.2);

    let result = AlgebraicMerger::merge(OpType::AbelianAdd, &v1, &v2);
    // Note: 0.1 + 0.2 has floating-point precision issues
    if let MergeResult::Merged(AlgebraicValue::Float(f)) = result {
        assert!((f - 0.3).abs() < 1e-10);
    }
}

#[test]
fn test_unregistered_table_no_auto_merge() {
    let registry = AlgebraicSchemaRegistry::new(); // Empty
    let analyzer = MergeAnalyzer::new(&registry);

    let source = create_branch_with_tables("feature", &[("unknown", 2)]);
    let target = create_branch_with_tables("main", &[("unknown", 1)]);

    let diff = BranchDiff::compute(&source, &target);
    let analysis = analyzer.analyze(&diff);

    // Unregistered table cannot be auto-merged
    assert!(analysis.conflicting.contains(&"unknown".to_string()));
}

// ============================================================================
// Schema Configuration Tests
// ============================================================================

#[test]
fn test_schema_default_operation() {
    let mut schema = TableAlgebraicSchema::new("counters");
    schema.set_default(OpType::AbelianAdd);

    // All columns inherit default
    assert_eq!(schema.get_op_type("any_column"), OpType::AbelianAdd);
    assert!(schema.is_fully_conflict_free());
}

#[test]
fn test_schema_column_override() {
    let mut schema = TableAlgebraicSchema::new("mixed");
    schema.set_default(OpType::AbelianAdd);
    schema.add_column("special", OpType::GenericOverwrite);

    assert_eq!(schema.get_op_type("normal"), OpType::AbelianAdd);
    assert_eq!(schema.get_op_type("special"), OpType::GenericOverwrite);
    assert!(!schema.is_fully_conflict_free()); // special breaks it
}

#[test]
fn test_conflict_free_columns_list() {
    let mut schema = TableAlgebraicSchema::new("mixed");
    schema.add_column("counter", OpType::AbelianAdd);
    schema.add_column("timestamp", OpType::SemilatticeMax);
    schema.add_column("name", OpType::GenericOverwrite);
    schema.add_column("tags", OpType::SemilatticeUnion);

    let conflict_free = schema.conflict_free_columns();
    assert_eq!(conflict_free.len(), 3);
    assert!(conflict_free.contains(&"counter"));
    assert!(conflict_free.contains(&"timestamp"));
    assert!(conflict_free.contains(&"tags"));

    let conflicting = schema.conflicting_columns();
    assert_eq!(conflicting.len(), 1);
    assert!(conflicting.contains(&"name"));
}

// ============================================================================
// Real-World Scenario Tests
// ============================================================================

/// E-commerce shopping cart scenario
#[test]
fn test_shopping_cart_scenario() {
    let mut schema = TableAlgebraicSchema::new("cart_items");
    schema.set_default(OpType::AbelianAdd); // Default to conflict-free
    schema.add_column("quantity", OpType::AbelianAdd);
    schema.add_column("added_at", OpType::SemilatticeMin); // First add time
    schema.add_column("updated_at", OpType::SemilatticeMax); // Last update

    let mut registry = AlgebraicSchemaRegistry::new();
    registry.register(schema);

    // User adds item on phone: quantity += 2
    // User adds same item on laptop: quantity += 1
    let merged = AlgebraicMerger::merge(
        OpType::AbelianAdd,
        &AlgebraicValue::integer(2),
        &AlgebraicValue::integer(1),
    );
    assert_eq!(merged.unwrap(), AlgebraicValue::integer(3));

    assert!(registry.get("cart_items").unwrap().is_fully_conflict_free());
}

/// Analytics event counting scenario
#[test]
fn test_analytics_scenario() {
    let mut schema = TableAlgebraicSchema::new("page_views");
    schema.set_default(OpType::AbelianAdd);

    // Multiple analytics servers increment counters
    // Server A: views += 100
    // Server B: views += 150
    // Server C: views += 75
    let ab = AlgebraicMerger::merge(
        OpType::AbelianAdd,
        &AlgebraicValue::integer(100),
        &AlgebraicValue::integer(150),
    )
    .unwrap();
    let abc = AlgebraicMerger::merge(OpType::AbelianAdd, &ab, &AlgebraicValue::integer(75));

    assert_eq!(abc.unwrap(), AlgebraicValue::integer(325));
}

/// Permission system scenario
#[test]
fn test_permissions_scenario() {
    let mut schema = TableAlgebraicSchema::new("user_permissions");
    schema.add_column("granted_permissions", OpType::SemilatticeUnion);
    schema.add_column("revoked_permissions", OpType::SemilatticeUnion);

    // Admin A grants: ["read", "write"]
    // Admin B grants: ["execute"]
    let merged = AlgebraicMerger::merge(
        OpType::SemilatticeUnion,
        &AlgebraicValue::string_set(["read", "write"]),
        &AlgebraicValue::string_set(["execute"]),
    );

    if let MergeResult::Merged(AlgebraicValue::StringSet(perms)) = merged {
        assert_eq!(perms.len(), 3);
        assert!(perms.contains("read"));
        assert!(perms.contains("write"));
        assert!(perms.contains("execute"));
    }
}
