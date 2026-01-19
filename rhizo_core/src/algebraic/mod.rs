//! Algebraic classification for conflict-free merge operations.
//!
//! This module provides the mathematical foundation for automatic conflict
//! resolution in Rhizo. By classifying operations by their algebraic properties,
//! we can determine which concurrent modifications can be merged without conflict.
//!
//! # Mathematical Foundation
//!
//! ## Semilattice Operations
//!
//! A join-semilattice (L, ⊔) satisfies:
//! - **Associative**: (a ⊔ b) ⊔ c = a ⊔ (b ⊔ c)
//! - **Commutative**: a ⊔ b = b ⊔ a
//! - **Idempotent**: a ⊔ a = a
//!
//! Examples in Rhizo:
//! - `SemilatticeMax`: max(a, b) — last-update-timestamp wins
//! - `SemilatticeMin`: min(a, b) — first-update-timestamp wins
//! - `SemilatticeUnion`: A ∪ B — add-only sets (tags, permissions)
//! - `SemilatticeIntersect`: A ∩ B — common elements only
//!
//! ## Abelian Group Operations
//!
//! An Abelian group (G, +) satisfies:
//! - **Associative**: (a + b) + c = a + (b + c)
//! - **Commutative**: a + b = b + a
//! - **Identity**: a + 0 = a
//! - **Inverse**: a + (-a) = 0
//!
//! Examples in Rhizo:
//! - `AbelianAdd`: a + b — counters, inventory deltas
//! - `AbelianMultiply`: a × b — scaling factors
//!
//! # Key Insight
//!
//! If operations form these algebraic structures, **order doesn't matter**.
//! Conflicts become mathematically impossible!
//!
//! # Example
//!
//! ```
//! use rhizo_core::algebraic::{
//!     OpType, AlgebraicValue, AlgebraicMerger, MergeResult,
//!     TableAlgebraicSchema, AlgebraicSchemaRegistry,
//! };
//!
//! // Define schema for inventory table
//! let mut schema = TableAlgebraicSchema::new("inventory");
//! schema.add_column("item_count", OpType::AbelianAdd);      // Counter
//! schema.add_column("last_updated", OpType::SemilatticeMax); // Timestamp
//! schema.add_column("tags", OpType::SemilatticeUnion);       // Add-only set
//!
//! // Two branches modify the same row concurrently:
//! // Branch A: item_count += 5
//! // Branch B: item_count += 3
//!
//! let value_a = AlgebraicValue::integer(5);
//! let value_b = AlgebraicValue::integer(3);
//!
//! let result = AlgebraicMerger::merge(
//!     OpType::AbelianAdd,
//!     &value_a,
//!     &value_b,
//! );
//!
//! // No conflict! Merged to 5 + 3 = 8
//! assert_eq!(result, MergeResult::Merged(AlgebraicValue::integer(8)));
//! ```
//!
//! # Module Structure
//!
//! - [`types`]: Core types (`OpType`, `AlgebraicValue`)
//! - [`merge`]: Merge rules (`AlgebraicMerger`, `MergeResult`)
//! - [`schema`]: Schema annotations (`ColumnAlgebraic`, `TableAlgebraicSchema`)

mod merge;
mod schema;
mod types;

// Re-export core types
pub use types::{AlgebraicValue, OpType};

// Re-export merge types
pub use merge::{AlgebraicMerger, MergeResult};

// Re-export schema types
pub use schema::{AlgebraicSchemaRegistry, ColumnAlgebraic, TableAlgebraicSchema};

#[cfg(test)]
mod integration_tests {
    use super::*;

    /// Test the complete workflow: schema definition -> merge resolution
    #[test]
    fn test_inventory_scenario() {
        // Define the schema
        let mut schema = TableAlgebraicSchema::new("inventory");
        schema.add_column("count", OpType::AbelianAdd);
        schema.add_column("last_seen", OpType::SemilatticeMax);
        schema.add_column("tags", OpType::SemilatticeUnion);
        schema.add_column("name", OpType::GenericOverwrite);

        // Verify which columns can auto-merge
        assert!(schema.can_auto_merge(&["count", "last_seen", "tags"]));
        assert!(!schema.can_auto_merge(&["name"]));

        // Simulate concurrent updates
        // Branch A: count += 5, last_seen = 1000, tags += {"featured"}
        // Branch B: count += 3, last_seen = 1500, tags += {"new", "sale"}

        // Merge count: 5 + 3 = 8
        let result = AlgebraicMerger::merge(
            OpType::AbelianAdd,
            &AlgebraicValue::integer(5),
            &AlgebraicValue::integer(3),
        );
        assert_eq!(result.unwrap(), AlgebraicValue::integer(8));

        // Merge last_seen: max(1000, 1500) = 1500
        let result = AlgebraicMerger::merge(
            OpType::SemilatticeMax,
            &AlgebraicValue::integer(1000),
            &AlgebraicValue::integer(1500),
        );
        assert_eq!(result.unwrap(), AlgebraicValue::integer(1500));

        // Merge tags: {"featured"} ∪ {"new", "sale"} = {"featured", "new", "sale"}
        let result = AlgebraicMerger::merge(
            OpType::SemilatticeUnion,
            &AlgebraicValue::string_set(["featured"]),
            &AlgebraicValue::string_set(["new", "sale"]),
        );
        if let AlgebraicValue::StringSet(s) = result.unwrap() {
            assert_eq!(s.len(), 3);
            assert!(s.contains("featured"));
            assert!(s.contains("new"));
            assert!(s.contains("sale"));
        } else {
            panic!("Expected StringSet");
        }
    }

    /// Test registry-based lookup
    #[test]
    fn test_registry_workflow() {
        let mut registry = AlgebraicSchemaRegistry::new();

        // Register multiple tables
        let mut inv_schema = TableAlgebraicSchema::new("inventory");
        inv_schema.add_column("count", OpType::AbelianAdd);
        registry.register(inv_schema);

        let mut user_schema = TableAlgebraicSchema::new("users");
        user_schema.add_column("login_count", OpType::AbelianAdd);
        user_schema.add_column("last_login", OpType::SemilatticeMax);
        registry.register(user_schema);

        // Look up operation types
        assert_eq!(
            registry.get_op_type("inventory", "count"),
            OpType::AbelianAdd
        );
        assert_eq!(
            registry.get_op_type("users", "last_login"),
            OpType::SemilatticeMax
        );

        // Unknown table/column returns Unknown
        assert_eq!(
            registry.get_op_type("orders", "total"),
            OpType::Unknown
        );
    }

    /// Test mathematical properties
    #[test]
    fn test_commutativity_guarantee() {
        let v1 = AlgebraicValue::integer(17);
        let v2 = AlgebraicValue::integer(23);

        // All conflict-free operations must be commutative
        for op in [
            OpType::SemilatticeMax,
            OpType::SemilatticeMin,
            OpType::AbelianAdd,
            OpType::AbelianMultiply,
        ] {
            let result1 = AlgebraicMerger::merge(op, &v1, &v2);
            let result2 = AlgebraicMerger::merge(op, &v2, &v1);
            assert_eq!(
                result1, result2,
                "{:?} should be commutative",
                op
            );
        }
    }

    /// Test idempotency for semilattice operations
    #[test]
    fn test_idempotency_guarantee() {
        let v = AlgebraicValue::integer(42);

        for op in [
            OpType::SemilatticeMax,
            OpType::SemilatticeMin,
        ] {
            let result = AlgebraicMerger::merge(op, &v, &v);
            assert_eq!(
                result.unwrap(),
                v,
                "{:?} should be idempotent",
                op
            );
        }
    }

    /// Test conflict scenarios
    #[test]
    fn test_conflict_detection() {
        let v1 = AlgebraicValue::integer(1);
        let v2 = AlgebraicValue::integer(2);

        // GenericOverwrite should conflict
        let result = AlgebraicMerger::merge(OpType::GenericOverwrite, &v1, &v2);
        assert!(result.is_conflict());

        // Unknown should conflict
        let result = AlgebraicMerger::merge(OpType::Unknown, &v1, &v2);
        assert!(result.is_conflict());
    }

    /// Test edge cases
    #[test]
    fn test_edge_cases() {
        // Empty set union
        let empty = AlgebraicValue::string_set::<[&str; 0], &str>([]);
        let tags = AlgebraicValue::string_set(["a", "b"]);
        let result = AlgebraicMerger::merge(OpType::SemilatticeUnion, &empty, &tags);
        assert_eq!(result.unwrap(), tags);

        // Add zero
        let v = AlgebraicValue::integer(42);
        let zero = AlgebraicValue::integer(0);
        let result = AlgebraicMerger::merge(OpType::AbelianAdd, &v, &zero);
        assert_eq!(result.unwrap(), v);

        // Multiply by one
        let one = AlgebraicValue::integer(1);
        let result = AlgebraicMerger::merge(OpType::AbelianMultiply, &v, &one);
        assert_eq!(result.unwrap(), v);

        // Null handling
        let null = AlgebraicValue::null();
        let result = AlgebraicMerger::merge(OpType::AbelianAdd, &null, &v);
        assert_eq!(result.unwrap(), v);
    }
}
