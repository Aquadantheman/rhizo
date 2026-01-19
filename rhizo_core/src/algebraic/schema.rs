//! Schema-level algebraic annotations for tables and columns.
//!
//! This module provides the metadata structures that define which columns
//! use which algebraic operations. These annotations are stored alongside
//! table schemas and used during merge operations.
//!
//! # Example
//!
//! ```
//! use rhizo_core::algebraic::{OpType, TableAlgebraicSchema, ColumnAlgebraic};
//!
//! let mut schema = TableAlgebraicSchema::new("inventory");
//! schema.set_default(OpType::GenericOverwrite);
//! schema.add_column("item_count", OpType::AbelianAdd);
//! schema.add_column("last_updated", OpType::SemilatticeMax);
//! schema.add_column("tags", OpType::SemilatticeUnion);
//!
//! // Conflict-free columns can be auto-merged
//! assert!(schema.get_op_type("item_count").is_conflict_free());
//! assert!(schema.get_op_type("last_updated").is_conflict_free());
//! assert!(schema.get_op_type("tags").is_conflict_free());
//!
//! // Unannotated columns use the default
//! assert!(!schema.get_op_type("name").is_conflict_free());
//! ```

use super::types::{AlgebraicValue, OpType};
use serde::{Deserialize, Serialize};
use std::collections::HashMap;

/// Algebraic annotation for a single column.
///
/// This defines how values in this column should be merged
/// when concurrent changes occur.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ColumnAlgebraic {
    /// Column name
    pub column: String,

    /// Operation type for merging this column's values
    pub op_type: OpType,

    /// Identity element for Abelian operations.
    ///
    /// For AbelianAdd, this is 0.
    /// For AbelianMultiply, this is 1.
    /// For sets, this is the empty set.
    pub identity: Option<AlgebraicValue>,

    /// Optional description for documentation
    pub description: Option<String>,
}

impl ColumnAlgebraic {
    /// Create a new column annotation with the specified operation type.
    pub fn new(column: impl Into<String>, op_type: OpType) -> Self {
        let identity = Self::default_identity(op_type);
        Self {
            column: column.into(),
            op_type,
            identity,
            description: None,
        }
    }

    /// Add a description to this annotation.
    pub fn with_description(mut self, desc: impl Into<String>) -> Self {
        self.description = Some(desc.into());
        self
    }

    /// Override the identity element.
    pub fn with_identity(mut self, identity: AlgebraicValue) -> Self {
        self.identity = Some(identity);
        self
    }

    /// Get the default identity element for an operation type.
    fn default_identity(op_type: OpType) -> Option<AlgebraicValue> {
        match op_type {
            OpType::AbelianAdd => Some(AlgebraicValue::Integer(0)),
            OpType::AbelianMultiply => Some(AlgebraicValue::Integer(1)),
            OpType::SemilatticeUnion => Some(AlgebraicValue::StringSet(Default::default())),
            OpType::SemilatticeIntersect => None, // Universal set has no representation
            OpType::SemilatticeMax => None,       // Negative infinity
            OpType::SemilatticeMin => None,       // Positive infinity
            _ => None,
        }
    }

    /// Check if this column can be automatically merged.
    #[inline]
    pub fn is_conflict_free(&self) -> bool {
        self.op_type.is_conflict_free()
    }
}

/// Schema-level algebraic configuration for a table.
///
/// This defines the merge behavior for all columns in a table,
/// with per-column overrides and a default for unannotated columns.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct TableAlgebraicSchema {
    /// Table name
    pub table: String,

    /// Column-level algebraic annotations, keyed by column name
    pub columns: HashMap<String, ColumnAlgebraic>,

    /// Default operation type for unannotated columns.
    ///
    /// Defaults to `OpType::Unknown` for safety.
    pub default_op_type: OpType,

    /// Optional description for documentation
    pub description: Option<String>,
}

impl TableAlgebraicSchema {
    /// Create a new schema for the given table.
    ///
    /// Default operation type is `Unknown` (conservative).
    pub fn new(table: impl Into<String>) -> Self {
        Self {
            table: table.into(),
            columns: HashMap::new(),
            default_op_type: OpType::Unknown,
            description: None,
        }
    }

    /// Create a schema with all columns defaulting to conflict-free add.
    ///
    /// Use this for counter/accumulator tables.
    pub fn all_additive(table: impl Into<String>) -> Self {
        Self {
            table: table.into(),
            columns: HashMap::new(),
            default_op_type: OpType::AbelianAdd,
            description: Some("All columns use additive merge".to_string()),
        }
    }

    /// Create a schema with all columns defaulting to max (last-writer-wins).
    ///
    /// Use this for timestamp-based tables.
    pub fn all_max(table: impl Into<String>) -> Self {
        Self {
            table: table.into(),
            columns: HashMap::new(),
            default_op_type: OpType::SemilatticeMax,
            description: Some("All columns use max merge".to_string()),
        }
    }

    /// Set the default operation type for unannotated columns.
    pub fn set_default(&mut self, op_type: OpType) {
        self.default_op_type = op_type;
    }

    /// Add a description to this schema.
    pub fn with_description(mut self, desc: impl Into<String>) -> Self {
        self.description = Some(desc.into());
        self
    }

    /// Add a column with the specified operation type.
    pub fn add_column(&mut self, column: impl Into<String>, op_type: OpType) {
        let col_name = column.into();
        let annotation = ColumnAlgebraic::new(col_name.clone(), op_type);
        self.columns.insert(col_name, annotation);
    }

    /// Add a column with full annotation.
    pub fn add_column_annotation(&mut self, annotation: ColumnAlgebraic) {
        self.columns.insert(annotation.column.clone(), annotation);
    }

    /// Get the operation type for a column.
    ///
    /// Returns the column's specific type if annotated,
    /// otherwise returns the table's default.
    pub fn get_op_type(&self, column: &str) -> OpType {
        self.columns
            .get(column)
            .map(|c| c.op_type)
            .unwrap_or(self.default_op_type)
    }

    /// Get the full column annotation if it exists.
    pub fn get_column(&self, column: &str) -> Option<&ColumnAlgebraic> {
        self.columns.get(column)
    }

    /// Check if all columns are conflict-free.
    ///
    /// Returns true if:
    /// - The default is conflict-free, AND
    /// - All explicit column annotations are conflict-free
    pub fn is_fully_conflict_free(&self) -> bool {
        self.default_op_type.is_conflict_free()
            && self.columns.values().all(|c| c.op_type.is_conflict_free())
    }

    /// Get list of all annotated columns.
    pub fn annotated_columns(&self) -> Vec<&str> {
        self.columns.keys().map(|s| s.as_str()).collect()
    }

    /// Get list of conflict-free columns.
    pub fn conflict_free_columns(&self) -> Vec<&str> {
        self.columns
            .iter()
            .filter(|(_, c)| c.op_type.is_conflict_free())
            .map(|(k, _)| k.as_str())
            .collect()
    }

    /// Get list of columns that may conflict.
    pub fn conflicting_columns(&self) -> Vec<&str> {
        self.columns
            .iter()
            .filter(|(_, c)| !c.op_type.is_conflict_free())
            .map(|(k, _)| k.as_str())
            .collect()
    }

    /// Check if two writes to this table can be auto-merged.
    ///
    /// Returns true if all affected columns are conflict-free.
    ///
    /// # Arguments
    /// * `columns` - List of column names being written
    pub fn can_auto_merge(&self, columns: &[&str]) -> bool {
        columns.iter().all(|c| self.get_op_type(c).is_conflict_free())
    }
}

/// Registry for table algebraic schemas.
///
/// This provides a centralized lookup for schemas across all tables.
#[derive(Debug, Clone, Default)]
pub struct AlgebraicSchemaRegistry {
    schemas: HashMap<String, TableAlgebraicSchema>,
}

impl AlgebraicSchemaRegistry {
    /// Create an empty registry.
    pub fn new() -> Self {
        Self {
            schemas: HashMap::new(),
        }
    }

    /// Register a schema for a table.
    pub fn register(&mut self, schema: TableAlgebraicSchema) {
        self.schemas.insert(schema.table.clone(), schema);
    }

    /// Get the schema for a table.
    pub fn get(&self, table: &str) -> Option<&TableAlgebraicSchema> {
        self.schemas.get(table)
    }

    /// Get the operation type for a table/column.
    ///
    /// Returns `Unknown` if table is not registered.
    pub fn get_op_type(&self, table: &str, column: &str) -> OpType {
        self.schemas
            .get(table)
            .map(|s| s.get_op_type(column))
            .unwrap_or(OpType::Unknown)
    }

    /// Check if a table is registered.
    pub fn has_table(&self, table: &str) -> bool {
        self.schemas.contains_key(table)
    }

    /// Get all registered table names.
    pub fn tables(&self) -> Vec<&str> {
        self.schemas.keys().map(|s| s.as_str()).collect()
    }

    /// Remove a schema from the registry.
    pub fn unregister(&mut self, table: &str) -> Option<TableAlgebraicSchema> {
        self.schemas.remove(table)
    }

    /// Clear all schemas.
    pub fn clear(&mut self) {
        self.schemas.clear();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_column_algebraic_new() {
        let col = ColumnAlgebraic::new("count", OpType::AbelianAdd);
        assert_eq!(col.column, "count");
        assert_eq!(col.op_type, OpType::AbelianAdd);
        assert_eq!(col.identity, Some(AlgebraicValue::Integer(0)));
        assert!(col.is_conflict_free());
    }

    #[test]
    fn test_column_algebraic_with_description() {
        let col = ColumnAlgebraic::new("last_seen", OpType::SemilatticeMax)
            .with_description("Timestamp of last activity");
        assert_eq!(col.description, Some("Timestamp of last activity".to_string()));
    }

    #[test]
    fn test_table_schema_new() {
        let schema = TableAlgebraicSchema::new("users");
        assert_eq!(schema.table, "users");
        assert_eq!(schema.default_op_type, OpType::Unknown);
        assert!(schema.columns.is_empty());
    }

    #[test]
    fn test_table_schema_add_columns() {
        let mut schema = TableAlgebraicSchema::new("inventory");
        schema.add_column("count", OpType::AbelianAdd);
        schema.add_column("tags", OpType::SemilatticeUnion);
        schema.add_column("name", OpType::GenericOverwrite);

        assert_eq!(schema.get_op_type("count"), OpType::AbelianAdd);
        assert_eq!(schema.get_op_type("tags"), OpType::SemilatticeUnion);
        assert_eq!(schema.get_op_type("name"), OpType::GenericOverwrite);
        assert_eq!(schema.get_op_type("unknown"), OpType::Unknown);
    }

    #[test]
    fn test_table_schema_default() {
        let mut schema = TableAlgebraicSchema::new("counters");
        schema.set_default(OpType::AbelianAdd);

        // All columns inherit the default
        assert_eq!(schema.get_op_type("any_column"), OpType::AbelianAdd);
        assert_eq!(schema.get_op_type("another"), OpType::AbelianAdd);
    }

    #[test]
    fn test_table_schema_all_additive() {
        let schema = TableAlgebraicSchema::all_additive("metrics");
        assert_eq!(schema.default_op_type, OpType::AbelianAdd);
        assert!(schema.is_fully_conflict_free());
    }

    #[test]
    fn test_table_schema_all_max() {
        let schema = TableAlgebraicSchema::all_max("timestamps");
        assert_eq!(schema.default_op_type, OpType::SemilatticeMax);
        assert!(schema.is_fully_conflict_free());
    }

    #[test]
    fn test_table_schema_is_fully_conflict_free() {
        let mut schema = TableAlgebraicSchema::new("test");
        schema.set_default(OpType::AbelianAdd);
        schema.add_column("max_val", OpType::SemilatticeMax);
        assert!(schema.is_fully_conflict_free());

        // Adding a conflicting column breaks it
        schema.add_column("name", OpType::GenericOverwrite);
        assert!(!schema.is_fully_conflict_free());
    }

    #[test]
    fn test_table_schema_can_auto_merge() {
        let mut schema = TableAlgebraicSchema::new("inventory");
        schema.add_column("count", OpType::AbelianAdd);
        schema.add_column("name", OpType::GenericOverwrite);

        assert!(schema.can_auto_merge(&["count"]));
        assert!(!schema.can_auto_merge(&["name"]));
        assert!(!schema.can_auto_merge(&["count", "name"]));
    }

    #[test]
    fn test_table_schema_conflict_free_columns() {
        let mut schema = TableAlgebraicSchema::new("test");
        schema.add_column("a", OpType::AbelianAdd);
        schema.add_column("b", OpType::SemilatticeMax);
        schema.add_column("c", OpType::GenericOverwrite);

        let cf = schema.conflict_free_columns();
        assert!(cf.contains(&"a"));
        assert!(cf.contains(&"b"));
        assert!(!cf.contains(&"c"));

        let conflicting = schema.conflicting_columns();
        assert!(!conflicting.contains(&"a"));
        assert!(!conflicting.contains(&"b"));
        assert!(conflicting.contains(&"c"));
    }

    #[test]
    fn test_registry_basic() {
        let mut registry = AlgebraicSchemaRegistry::new();

        let mut schema = TableAlgebraicSchema::new("users");
        schema.add_column("login_count", OpType::AbelianAdd);
        registry.register(schema);

        assert!(registry.has_table("users"));
        assert!(!registry.has_table("orders"));

        assert_eq!(
            registry.get_op_type("users", "login_count"),
            OpType::AbelianAdd
        );
        assert_eq!(
            registry.get_op_type("users", "unknown"),
            OpType::Unknown
        );
        assert_eq!(
            registry.get_op_type("nonexistent", "col"),
            OpType::Unknown
        );
    }

    #[test]
    fn test_registry_multiple_tables() {
        let mut registry = AlgebraicSchemaRegistry::new();

        registry.register(TableAlgebraicSchema::all_additive("counters"));
        registry.register(TableAlgebraicSchema::all_max("timestamps"));

        assert_eq!(
            registry.get_op_type("counters", "any"),
            OpType::AbelianAdd
        );
        assert_eq!(
            registry.get_op_type("timestamps", "any"),
            OpType::SemilatticeMax
        );
    }

    #[test]
    fn test_registry_unregister() {
        let mut registry = AlgebraicSchemaRegistry::new();
        registry.register(TableAlgebraicSchema::new("test"));

        assert!(registry.has_table("test"));

        let removed = registry.unregister("test");
        assert!(removed.is_some());
        assert!(!registry.has_table("test"));
    }

    #[test]
    fn test_registry_tables() {
        let mut registry = AlgebraicSchemaRegistry::new();
        registry.register(TableAlgebraicSchema::new("a"));
        registry.register(TableAlgebraicSchema::new("b"));
        registry.register(TableAlgebraicSchema::new("c"));

        let tables = registry.tables();
        assert_eq!(tables.len(), 3);
        assert!(tables.contains(&"a"));
        assert!(tables.contains(&"b"));
        assert!(tables.contains(&"c"));
    }

    #[test]
    fn test_schema_serialization() {
        let mut schema = TableAlgebraicSchema::new("test");
        schema.add_column("count", OpType::AbelianAdd);
        schema.add_column("tags", OpType::SemilatticeUnion);

        let json = serde_json::to_string(&schema).unwrap();
        let parsed: TableAlgebraicSchema = serde_json::from_str(&json).unwrap();

        assert_eq!(schema, parsed);
    }
}
