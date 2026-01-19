//! Local commit protocol for coordination-free distributed transactions.
//!
//! This module implements the core insight that **algebraic operations can commit
//! locally without coordination**. When all operations in a transaction are
//! algebraic (semilattice or Abelian), the transaction can be applied immediately
//! on any node and will eventually converge to the same result regardless of
//! message ordering.
//!
//! # Theory
//!
//! For operations that are:
//! - **Commutative**: `op(a, b) = op(b, a)`
//! - **Associative**: `op(op(a, b), c) = op(a, op(b, c))`
//!
//! The following theorem holds:
//!
//! > **Theorem (Convergence)**: Given a set of nodes N, if each node applies
//! > the same set of algebraic operations (in any order), all nodes will
//! > converge to the same final state.
//!
//! # Usage
//!
//! ```
//! use rhizo_core::distributed::{
//!     AlgebraicOperation, AlgebraicTransaction, LocalCommitProtocol,
//!     VersionedUpdate, NodeId, VectorClock,
//! };
//! use rhizo_core::algebraic::{OpType, AlgebraicValue};
//!
//! // Create a transaction on node "sf"
//! let node_sf = NodeId::new("san-francisco");
//! let mut clock_sf = VectorClock::new();
//!
//! let mut tx = AlgebraicTransaction::new();
//! tx.add_operation(AlgebraicOperation::new(
//!     "page_views",
//!     OpType::AbelianAdd,
//!     AlgebraicValue::integer(1),
//! ));
//! tx.add_operation(AlgebraicOperation::new(
//!     "tags",
//!     OpType::SemilatticeUnion,
//!     AlgebraicValue::string_set(["featured"]),
//! ));
//!
//! // Check if we can commit locally (all ops are algebraic)
//! assert!(LocalCommitProtocol::can_commit_locally(&tx));
//!
//! // Commit locally - this returns immediately (no coordination!)
//! let update = LocalCommitProtocol::commit_local(&tx, &node_sf, &mut clock_sf);
//! assert!(update.is_ok());
//! ```
//!
//! # Merging Concurrent Updates
//!
//! When two nodes have concurrent updates, they can be merged:
//!
//! ```
//! use rhizo_core::distributed::{
//!     AlgebraicOperation, AlgebraicTransaction, LocalCommitProtocol,
//!     NodeId, VectorClock,
//! };
//! use rhizo_core::algebraic::{OpType, AlgebraicValue};
//!
//! // Node SF increments counter by 5
//! let node_sf = NodeId::new("sf");
//! let mut clock_sf = VectorClock::new();
//! let mut tx_sf = AlgebraicTransaction::new();
//! tx_sf.add_operation(AlgebraicOperation::new(
//!     "counter",
//!     OpType::AbelianAdd,
//!     AlgebraicValue::integer(5),
//! ));
//! let update_sf = LocalCommitProtocol::commit_local(&tx_sf, &node_sf, &mut clock_sf).unwrap();
//!
//! // Node Tokyo increments counter by 3 (concurrently)
//! let node_tokyo = NodeId::new("tokyo");
//! let mut clock_tokyo = VectorClock::new();
//! let mut tx_tokyo = AlgebraicTransaction::new();
//! tx_tokyo.add_operation(AlgebraicOperation::new(
//!     "counter",
//!     OpType::AbelianAdd,
//!     AlgebraicValue::integer(3),
//! ));
//! let update_tokyo = LocalCommitProtocol::commit_local(&tx_tokyo, &node_tokyo, &mut clock_tokyo).unwrap();
//!
//! // Merge the concurrent updates
//! let merged = LocalCommitProtocol::merge_updates(&update_sf, &update_tokyo).unwrap();
//!
//! // Result: counter = 5 + 3 = 8
//! let counter_op = merged.operations().iter().find(|op| op.key() == "counter").unwrap();
//! assert_eq!(counter_op.value().as_integer(), Some(8));
//! ```

use super::vector_clock::{CausalOrder, NodeId, VectorClock};
use crate::algebraic::{AlgebraicMerger, AlgebraicValue, MergeResult, OpType};
use serde::{Deserialize, Serialize};
use std::collections::HashMap;

/// A single algebraic operation on a key.
///
/// Operations are the building blocks of transactions. Each operation
/// specifies a key, an algebraic type, and a delta value.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct AlgebraicOperation {
    /// The key being operated on (e.g., "page_views", "user:123:tags")
    key: String,
    /// The algebraic operation type
    op_type: OpType,
    /// The value/delta to apply
    value: AlgebraicValue,
}

impl AlgebraicOperation {
    /// Create a new algebraic operation.
    ///
    /// # Arguments
    /// * `key` - The key to operate on
    /// * `op_type` - The algebraic operation type
    /// * `value` - The value or delta to apply
    pub fn new(key: impl Into<String>, op_type: OpType, value: AlgebraicValue) -> Self {
        Self {
            key: key.into(),
            op_type,
            value,
        }
    }

    /// Get the key.
    #[inline]
    pub fn key(&self) -> &str {
        &self.key
    }

    /// Get the operation type.
    #[inline]
    pub fn op_type(&self) -> OpType {
        self.op_type
    }

    /// Get the value.
    #[inline]
    pub fn value(&self) -> &AlgebraicValue {
        &self.value
    }

    /// Check if this operation can be committed locally (is algebraic).
    #[inline]
    pub fn is_algebraic(&self) -> bool {
        self.op_type.is_conflict_free()
    }
}

/// A transaction containing multiple algebraic operations.
///
/// Transactions group operations that should be applied atomically.
/// For coordination-free commits, all operations must be algebraic.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct AlgebraicTransaction {
    /// Operations in this transaction
    operations: Vec<AlgebraicOperation>,
    /// Optional transaction metadata
    metadata: HashMap<String, String>,
}

impl AlgebraicTransaction {
    /// Create a new empty transaction.
    pub fn new() -> Self {
        Self::default()
    }

    /// Add an operation to the transaction.
    pub fn add_operation(&mut self, op: AlgebraicOperation) {
        self.operations.push(op);
    }

    /// Add multiple operations.
    pub fn add_operations(&mut self, ops: impl IntoIterator<Item = AlgebraicOperation>) {
        self.operations.extend(ops);
    }

    /// Get all operations.
    #[inline]
    pub fn operations(&self) -> &[AlgebraicOperation] {
        &self.operations
    }

    /// Get the number of operations.
    #[inline]
    pub fn len(&self) -> usize {
        self.operations.len()
    }

    /// Check if the transaction is empty.
    #[inline]
    pub fn is_empty(&self) -> bool {
        self.operations.is_empty()
    }

    /// Check if all operations in this transaction are algebraic.
    ///
    /// This is a necessary condition for coordination-free local commit.
    pub fn is_fully_algebraic(&self) -> bool {
        self.operations.iter().all(|op| op.is_algebraic())
    }

    /// Get operations that are not algebraic.
    ///
    /// Returns the keys and operation types of non-algebraic operations.
    pub fn non_algebraic_operations(&self) -> Vec<(&str, OpType)> {
        self.operations
            .iter()
            .filter(|op| !op.is_algebraic())
            .map(|op| (op.key(), op.op_type()))
            .collect()
    }

    /// Set metadata.
    pub fn set_metadata(&mut self, key: impl Into<String>, value: impl Into<String>) {
        self.metadata.insert(key.into(), value.into());
    }

    /// Get metadata.
    pub fn get_metadata(&self, key: &str) -> Option<&str> {
        self.metadata.get(key).map(|s| s.as_str())
    }
}

/// The result of a local commit: operations with their causal context.
///
/// A VersionedUpdate represents a committed set of operations along with
/// the vector clock at the time of commit. This allows other nodes to:
/// 1. Determine the causal relationship with their own state
/// 2. Merge concurrent updates correctly
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct VersionedUpdate {
    /// The operations that were committed
    operations: Vec<AlgebraicOperation>,
    /// The vector clock at commit time
    clock: VectorClock,
    /// The node that created this update
    origin_node: NodeId,
    /// Optional update ID for deduplication
    update_id: Option<String>,
}

impl VersionedUpdate {
    /// Create a new versioned update.
    pub fn new(
        operations: Vec<AlgebraicOperation>,
        clock: VectorClock,
        origin_node: NodeId,
    ) -> Self {
        Self {
            operations,
            clock,
            origin_node,
            update_id: None,
        }
    }

    /// Create with an explicit update ID.
    pub fn with_id(
        operations: Vec<AlgebraicOperation>,
        clock: VectorClock,
        origin_node: NodeId,
        update_id: impl Into<String>,
    ) -> Self {
        Self {
            operations,
            clock,
            origin_node,
            update_id: Some(update_id.into()),
        }
    }

    /// Get the operations.
    #[inline]
    pub fn operations(&self) -> &[AlgebraicOperation] {
        &self.operations
    }

    /// Get the vector clock.
    #[inline]
    pub fn clock(&self) -> &VectorClock {
        &self.clock
    }

    /// Get the origin node.
    #[inline]
    pub fn origin_node(&self) -> &NodeId {
        &self.origin_node
    }

    /// Get the update ID if set.
    #[inline]
    pub fn update_id(&self) -> Option<&str> {
        self.update_id.as_deref()
    }

    /// Compare this update's causality with another.
    pub fn compare(&self, other: &VersionedUpdate) -> CausalOrder {
        self.clock.compare(&other.clock)
    }

    /// Check if this update is concurrent with another.
    pub fn is_concurrent_with(&self, other: &VersionedUpdate) -> bool {
        self.clock.concurrent_with(&other.clock)
    }
}

/// Error type for local commit operations.
#[derive(Debug, Clone, PartialEq)]
pub enum LocalCommitError {
    /// Transaction contains non-algebraic operations.
    NonAlgebraic {
        keys: Vec<String>,
        message: String,
    },
    /// Transaction is empty.
    EmptyTransaction,
    /// Merge failed for a specific key.
    MergeFailed {
        key: String,
        reason: String,
    },
    /// Type mismatch during merge.
    TypeMismatch {
        key: String,
        type1: &'static str,
        type2: &'static str,
    },
}

impl std::fmt::Display for LocalCommitError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::NonAlgebraic { keys, message } => {
                write!(f, "Non-algebraic operations on keys {:?}: {}", keys, message)
            }
            Self::EmptyTransaction => write!(f, "Transaction is empty"),
            Self::MergeFailed { key, reason } => {
                write!(f, "Merge failed for key '{}': {}", key, reason)
            }
            Self::TypeMismatch { key, type1, type2 } => {
                write!(f, "Type mismatch for key '{}': {} vs {}", key, type1, type2)
            }
        }
    }
}

impl std::error::Error for LocalCommitError {}

/// The local commit protocol for coordination-free transactions.
///
/// This struct provides the core logic for:
/// 1. Checking if a transaction can commit locally
/// 2. Committing a transaction locally (no coordination)
/// 3. Merging concurrent updates from different nodes
pub struct LocalCommitProtocol;

impl LocalCommitProtocol {
    /// Check if a transaction can be committed locally without coordination.
    ///
    /// Returns `true` if all operations in the transaction are algebraic
    /// (semilattice or Abelian), meaning they commute and can be applied
    /// in any order.
    ///
    /// # Example
    /// ```
    /// use rhizo_core::distributed::{AlgebraicOperation, AlgebraicTransaction, LocalCommitProtocol};
    /// use rhizo_core::algebraic::{OpType, AlgebraicValue};
    ///
    /// let mut tx = AlgebraicTransaction::new();
    /// tx.add_operation(AlgebraicOperation::new(
    ///     "counter",
    ///     OpType::AbelianAdd,
    ///     AlgebraicValue::integer(1),
    /// ));
    ///
    /// // AbelianAdd is algebraic, so we can commit locally
    /// assert!(LocalCommitProtocol::can_commit_locally(&tx));
    ///
    /// // But GenericOverwrite is not
    /// let mut tx2 = AlgebraicTransaction::new();
    /// tx2.add_operation(AlgebraicOperation::new(
    ///     "name",
    ///     OpType::GenericOverwrite,
    ///     AlgebraicValue::integer(1),
    /// ));
    /// assert!(!LocalCommitProtocol::can_commit_locally(&tx2));
    /// ```
    #[inline]
    pub fn can_commit_locally(tx: &AlgebraicTransaction) -> bool {
        !tx.is_empty() && tx.is_fully_algebraic()
    }

    /// Commit a transaction locally, returning a versioned update.
    ///
    /// This operation:
    /// 1. Validates that all operations are algebraic
    /// 2. Increments the local vector clock
    /// 3. Returns a VersionedUpdate that can be sent to other nodes
    ///
    /// # Arguments
    /// * `tx` - The transaction to commit
    /// * `node_id` - This node's ID
    /// * `clock` - This node's vector clock (will be mutated)
    ///
    /// # Returns
    /// * `Ok(VersionedUpdate)` - The committed update with causal context
    /// * `Err(LocalCommitError)` - If the transaction cannot be committed locally
    ///
    /// # Example
    /// ```
    /// use rhizo_core::distributed::{
    ///     AlgebraicOperation, AlgebraicTransaction, LocalCommitProtocol,
    ///     NodeId, VectorClock,
    /// };
    /// use rhizo_core::algebraic::{OpType, AlgebraicValue};
    ///
    /// let node = NodeId::new("my-node");
    /// let mut clock = VectorClock::new();
    ///
    /// let mut tx = AlgebraicTransaction::new();
    /// tx.add_operation(AlgebraicOperation::new(
    ///     "counter",
    ///     OpType::AbelianAdd,
    ///     AlgebraicValue::integer(5),
    /// ));
    ///
    /// let update = LocalCommitProtocol::commit_local(&tx, &node, &mut clock);
    /// assert!(update.is_ok());
    ///
    /// // Clock was incremented
    /// assert_eq!(clock.get(&node), 1);
    /// ```
    pub fn commit_local(
        tx: &AlgebraicTransaction,
        node_id: &NodeId,
        clock: &mut VectorClock,
    ) -> Result<VersionedUpdate, LocalCommitError> {
        // Validate: not empty
        if tx.is_empty() {
            return Err(LocalCommitError::EmptyTransaction);
        }

        // Validate: all algebraic
        if !tx.is_fully_algebraic() {
            let non_alg = tx.non_algebraic_operations();
            let keys: Vec<String> = non_alg.iter().map(|(k, _)| k.to_string()).collect();
            let ops: Vec<String> = non_alg.iter().map(|(_, op)| format!("{}", op)).collect();
            return Err(LocalCommitError::NonAlgebraic {
                keys,
                message: format!("Operations {} require coordination", ops.join(", ")),
            });
        }

        // Increment the clock for this commit
        clock.tick(node_id);

        // Create the versioned update
        Ok(VersionedUpdate::new(
            tx.operations.clone(),
            clock.clone(),
            node_id.clone(),
        ))
    }

    /// Merge two versioned updates into one.
    ///
    /// This is the core of coordination-free merging. Given two updates
    /// (potentially concurrent), this function:
    /// 1. Combines operations by key
    /// 2. Merges values using algebraic operations
    /// 3. Computes the merged vector clock
    ///
    /// # Mathematical Guarantees
    ///
    /// For algebraic operations:
    /// - `merge(A, B) = merge(B, A)` (commutative)
    /// - `merge(merge(A, B), C) = merge(A, merge(B, C))` (associative)
    ///
    /// These properties ensure that all nodes converge to the same state
    /// regardless of the order in which they receive updates.
    ///
    /// # Example
    /// ```
    /// use rhizo_core::distributed::{
    ///     AlgebraicOperation, AlgebraicTransaction, LocalCommitProtocol,
    ///     NodeId, VectorClock,
    /// };
    /// use rhizo_core::algebraic::{OpType, AlgebraicValue};
    ///
    /// // Two nodes increment the same counter concurrently
    /// let node_a = NodeId::new("a");
    /// let node_b = NodeId::new("b");
    /// let mut clock_a = VectorClock::new();
    /// let mut clock_b = VectorClock::new();
    ///
    /// let mut tx_a = AlgebraicTransaction::new();
    /// tx_a.add_operation(AlgebraicOperation::new(
    ///     "counter", OpType::AbelianAdd, AlgebraicValue::integer(5),
    /// ));
    ///
    /// let mut tx_b = AlgebraicTransaction::new();
    /// tx_b.add_operation(AlgebraicOperation::new(
    ///     "counter", OpType::AbelianAdd, AlgebraicValue::integer(3),
    /// ));
    ///
    /// let update_a = LocalCommitProtocol::commit_local(&tx_a, &node_a, &mut clock_a).unwrap();
    /// let update_b = LocalCommitProtocol::commit_local(&tx_b, &node_b, &mut clock_b).unwrap();
    ///
    /// // Merge the updates (order doesn't matter!)
    /// let merged_ab = LocalCommitProtocol::merge_updates(&update_a, &update_b).unwrap();
    /// let merged_ba = LocalCommitProtocol::merge_updates(&update_b, &update_a).unwrap();
    ///
    /// // Both produce the same result: 5 + 3 = 8
    /// let get_counter = |u: &rhizo_core::distributed::VersionedUpdate| {
    ///     u.operations().iter()
    ///         .find(|op| op.key() == "counter")
    ///         .and_then(|op| op.value().as_integer())
    /// };
    /// assert_eq!(get_counter(&merged_ab), Some(8));
    /// assert_eq!(get_counter(&merged_ba), Some(8));
    /// ```
    pub fn merge_updates(
        update1: &VersionedUpdate,
        update2: &VersionedUpdate,
    ) -> Result<VersionedUpdate, LocalCommitError> {
        // Group operations by key
        let mut by_key: HashMap<String, Vec<&AlgebraicOperation>> = HashMap::new();

        for op in update1.operations() {
            by_key.entry(op.key().to_string()).or_default().push(op);
        }
        for op in update2.operations() {
            by_key.entry(op.key().to_string()).or_default().push(op);
        }

        // Merge operations for each key
        let mut merged_ops = Vec::new();

        for (key, ops) in by_key {
            if ops.len() == 1 {
                // No merge needed, just include the operation
                merged_ops.push(ops[0].clone());
            } else {
                // Need to merge multiple operations on the same key
                let merged_op = Self::merge_operations_for_key(&key, &ops)?;
                merged_ops.push(merged_op);
            }
        }

        // Merge the vector clocks
        let merged_clock = VectorClock::max(update1.clock(), update2.clock());

        // The origin is a synthetic merge node
        // In practice, you might want to track this differently
        let origin = NodeId::new(format!(
            "merged:{}+{}",
            update1.origin_node(),
            update2.origin_node()
        ));

        Ok(VersionedUpdate::new(merged_ops, merged_clock, origin))
    }

    /// Merge multiple operations on the same key.
    fn merge_operations_for_key(
        key: &str,
        ops: &[&AlgebraicOperation],
    ) -> Result<AlgebraicOperation, LocalCommitError> {
        if ops.is_empty() {
            return Err(LocalCommitError::MergeFailed {
                key: key.to_string(),
                reason: "No operations to merge".to_string(),
            });
        }

        // Start with the first operation
        let mut result_value = ops[0].value().clone();
        let op_type = ops[0].op_type();

        // Verify all operations have the same op_type
        for op in ops.iter().skip(1) {
            if op.op_type() != op_type {
                return Err(LocalCommitError::MergeFailed {
                    key: key.to_string(),
                    reason: format!(
                        "Conflicting operation types: {} vs {}",
                        op_type,
                        op.op_type()
                    ),
                });
            }
        }

        // Merge all subsequent values
        for op in ops.iter().skip(1) {
            let merge_result = AlgebraicMerger::merge(op_type, &result_value, op.value());

            match merge_result {
                MergeResult::Merged(v) => result_value = v,
                MergeResult::Conflict { reason, .. } => {
                    return Err(LocalCommitError::MergeFailed {
                        key: key.to_string(),
                        reason,
                    });
                }
                MergeResult::TypeMismatch {
                    type1, type2, ..
                } => {
                    return Err(LocalCommitError::TypeMismatch {
                        key: key.to_string(),
                        type1,
                        type2,
                    });
                }
            }
        }

        Ok(AlgebraicOperation::new(key, op_type, result_value))
    }

    /// Merge multiple updates at once (more efficient than pairwise).
    ///
    /// This function merges N updates in a single pass, which is more
    /// efficient than calling `merge_updates` repeatedly.
    ///
    /// # Mathematical Note
    ///
    /// Due to associativity, `merge([A, B, C])` produces the same result as
    /// `merge(merge(A, B), C)` or `merge(A, merge(B, C))`.
    pub fn merge_all(updates: &[VersionedUpdate]) -> Result<VersionedUpdate, LocalCommitError> {
        if updates.is_empty() {
            return Err(LocalCommitError::EmptyTransaction);
        }

        if updates.len() == 1 {
            return Ok(updates[0].clone());
        }

        // Group all operations by key across all updates
        let mut by_key: HashMap<String, Vec<&AlgebraicOperation>> = HashMap::new();

        for update in updates {
            for op in update.operations() {
                by_key.entry(op.key().to_string()).or_default().push(op);
            }
        }

        // Merge operations for each key
        let mut merged_ops = Vec::new();
        for (key, ops) in by_key {
            if ops.len() == 1 {
                merged_ops.push(ops[0].clone());
            } else {
                let merged_op = Self::merge_operations_for_key(&key, &ops)?;
                merged_ops.push(merged_op);
            }
        }

        // Merge all vector clocks
        let mut merged_clock = updates[0].clock().clone();
        for update in updates.iter().skip(1) {
            merged_clock.merge(update.clock());
        }

        // Create synthetic origin
        let origins: Vec<String> = updates.iter().map(|u| u.origin_node().to_string()).collect();
        let origin = NodeId::new(format!("merged:{}", origins.join("+")));

        Ok(VersionedUpdate::new(merged_ops, merged_clock, origin))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    // ============ Helper Functions ============

    fn add_op(key: &str, value: i64) -> AlgebraicOperation {
        AlgebraicOperation::new(key, OpType::AbelianAdd, AlgebraicValue::integer(value))
    }

    fn max_op(key: &str, value: i64) -> AlgebraicOperation {
        AlgebraicOperation::new(key, OpType::SemilatticeMax, AlgebraicValue::integer(value))
    }

    fn union_op(key: &str, values: &[&str]) -> AlgebraicOperation {
        AlgebraicOperation::new(key, OpType::SemilatticeUnion, AlgebraicValue::string_set(values.to_vec()))
    }

    fn overwrite_op(key: &str, value: i64) -> AlgebraicOperation {
        AlgebraicOperation::new(key, OpType::GenericOverwrite, AlgebraicValue::integer(value))
    }

    // ============ AlgebraicOperation Tests ============

    #[test]
    fn test_operation_creation() {
        let op = add_op("counter", 5);
        assert_eq!(op.key(), "counter");
        assert_eq!(op.op_type(), OpType::AbelianAdd);
        assert_eq!(op.value().as_integer(), Some(5));
        assert!(op.is_algebraic());
    }

    #[test]
    fn test_operation_is_algebraic() {
        assert!(add_op("k", 1).is_algebraic());
        assert!(max_op("k", 1).is_algebraic());
        assert!(union_op("k", &["a"]).is_algebraic());
        assert!(!overwrite_op("k", 1).is_algebraic());
    }

    // ============ AlgebraicTransaction Tests ============

    #[test]
    fn test_transaction_creation() {
        let mut tx = AlgebraicTransaction::new();
        assert!(tx.is_empty());
        assert_eq!(tx.len(), 0);

        tx.add_operation(add_op("counter", 5));
        assert!(!tx.is_empty());
        assert_eq!(tx.len(), 1);
    }

    #[test]
    fn test_transaction_is_fully_algebraic() {
        let mut tx = AlgebraicTransaction::new();
        tx.add_operation(add_op("a", 1));
        tx.add_operation(max_op("b", 2));
        assert!(tx.is_fully_algebraic());

        tx.add_operation(overwrite_op("c", 3));
        assert!(!tx.is_fully_algebraic());
    }

    #[test]
    fn test_transaction_non_algebraic_operations() {
        let mut tx = AlgebraicTransaction::new();
        tx.add_operation(add_op("a", 1));
        tx.add_operation(overwrite_op("b", 2));
        tx.add_operation(max_op("c", 3));

        let non_alg = tx.non_algebraic_operations();
        assert_eq!(non_alg.len(), 1);
        assert_eq!(non_alg[0].0, "b");
        assert_eq!(non_alg[0].1, OpType::GenericOverwrite);
    }

    #[test]
    fn test_transaction_metadata() {
        let mut tx = AlgebraicTransaction::new();
        tx.set_metadata("user", "alice");
        tx.set_metadata("source", "web");

        assert_eq!(tx.get_metadata("user"), Some("alice"));
        assert_eq!(tx.get_metadata("source"), Some("web"));
        assert_eq!(tx.get_metadata("missing"), None);
    }

    // ============ can_commit_locally Tests ============

    #[test]
    fn test_can_commit_locally_algebraic() {
        let mut tx = AlgebraicTransaction::new();
        tx.add_operation(add_op("counter", 5));
        tx.add_operation(max_op("timestamp", 1000));
        tx.add_operation(union_op("tags", &["featured", "new"]));

        assert!(LocalCommitProtocol::can_commit_locally(&tx));
    }

    #[test]
    fn test_cannot_commit_locally_non_algebraic() {
        let mut tx = AlgebraicTransaction::new();
        tx.add_operation(add_op("counter", 5));
        tx.add_operation(overwrite_op("name", 1)); // Not algebraic!

        assert!(!LocalCommitProtocol::can_commit_locally(&tx));
    }

    #[test]
    fn test_cannot_commit_locally_empty() {
        let tx = AlgebraicTransaction::new();
        assert!(!LocalCommitProtocol::can_commit_locally(&tx));
    }

    // ============ commit_local Tests ============

    #[test]
    fn test_commit_local_success() {
        let node = NodeId::new("node-1");
        let mut clock = VectorClock::new();

        let mut tx = AlgebraicTransaction::new();
        tx.add_operation(add_op("counter", 5));

        let result = LocalCommitProtocol::commit_local(&tx, &node, &mut clock);
        assert!(result.is_ok());

        let update = result.unwrap();
        assert_eq!(update.operations().len(), 1);
        assert_eq!(update.origin_node(), &node);
        assert_eq!(update.clock().get(&node), 1);
        assert_eq!(clock.get(&node), 1); // Clock was mutated
    }

    #[test]
    fn test_commit_local_increments_clock() {
        let node = NodeId::new("node-1");
        let mut clock = VectorClock::new();
        clock.set(&node, 5); // Start at 5

        let mut tx = AlgebraicTransaction::new();
        tx.add_operation(add_op("counter", 1));

        LocalCommitProtocol::commit_local(&tx, &node, &mut clock).unwrap();
        assert_eq!(clock.get(&node), 6);

        LocalCommitProtocol::commit_local(&tx, &node, &mut clock).unwrap();
        assert_eq!(clock.get(&node), 7);
    }

    #[test]
    fn test_commit_local_fails_non_algebraic() {
        let node = NodeId::new("node-1");
        let mut clock = VectorClock::new();

        let mut tx = AlgebraicTransaction::new();
        tx.add_operation(overwrite_op("name", 1));

        let result = LocalCommitProtocol::commit_local(&tx, &node, &mut clock);
        assert!(matches!(result, Err(LocalCommitError::NonAlgebraic { .. })));

        // Clock should NOT be incremented on failure
        assert_eq!(clock.get(&node), 0);
    }

    #[test]
    fn test_commit_local_fails_empty() {
        let node = NodeId::new("node-1");
        let mut clock = VectorClock::new();

        let tx = AlgebraicTransaction::new();
        let result = LocalCommitProtocol::commit_local(&tx, &node, &mut clock);
        assert!(matches!(result, Err(LocalCommitError::EmptyTransaction)));
    }

    // ============ merge_updates Tests ============

    #[test]
    fn test_merge_updates_disjoint_keys() {
        let node_a = NodeId::new("a");
        let node_b = NodeId::new("b");
        let mut clock_a = VectorClock::new();
        let mut clock_b = VectorClock::new();

        let mut tx_a = AlgebraicTransaction::new();
        tx_a.add_operation(add_op("counter_a", 5));

        let mut tx_b = AlgebraicTransaction::new();
        tx_b.add_operation(add_op("counter_b", 3));

        let update_a = LocalCommitProtocol::commit_local(&tx_a, &node_a, &mut clock_a).unwrap();
        let update_b = LocalCommitProtocol::commit_local(&tx_b, &node_b, &mut clock_b).unwrap();

        let merged = LocalCommitProtocol::merge_updates(&update_a, &update_b).unwrap();

        assert_eq!(merged.operations().len(), 2);

        let find_op = |key: &str| {
            merged
                .operations()
                .iter()
                .find(|op| op.key() == key)
                .unwrap()
                .value()
                .as_integer()
        };

        assert_eq!(find_op("counter_a"), Some(5));
        assert_eq!(find_op("counter_b"), Some(3));
    }

    #[test]
    fn test_merge_updates_same_key_add() {
        let node_a = NodeId::new("a");
        let node_b = NodeId::new("b");
        let mut clock_a = VectorClock::new();
        let mut clock_b = VectorClock::new();

        let mut tx_a = AlgebraicTransaction::new();
        tx_a.add_operation(add_op("counter", 5));

        let mut tx_b = AlgebraicTransaction::new();
        tx_b.add_operation(add_op("counter", 3));

        let update_a = LocalCommitProtocol::commit_local(&tx_a, &node_a, &mut clock_a).unwrap();
        let update_b = LocalCommitProtocol::commit_local(&tx_b, &node_b, &mut clock_b).unwrap();

        let merged = LocalCommitProtocol::merge_updates(&update_a, &update_b).unwrap();

        assert_eq!(merged.operations().len(), 1);
        assert_eq!(merged.operations()[0].key(), "counter");
        assert_eq!(merged.operations()[0].value().as_integer(), Some(8)); // 5 + 3
    }

    #[test]
    fn test_merge_updates_same_key_max() {
        let node_a = NodeId::new("a");
        let node_b = NodeId::new("b");
        let mut clock_a = VectorClock::new();
        let mut clock_b = VectorClock::new();

        let mut tx_a = AlgebraicTransaction::new();
        tx_a.add_operation(max_op("timestamp", 1000));

        let mut tx_b = AlgebraicTransaction::new();
        tx_b.add_operation(max_op("timestamp", 1500));

        let update_a = LocalCommitProtocol::commit_local(&tx_a, &node_a, &mut clock_a).unwrap();
        let update_b = LocalCommitProtocol::commit_local(&tx_b, &node_b, &mut clock_b).unwrap();

        let merged = LocalCommitProtocol::merge_updates(&update_a, &update_b).unwrap();

        assert_eq!(merged.operations()[0].value().as_integer(), Some(1500)); // max(1000, 1500)
    }

    #[test]
    fn test_merge_updates_same_key_union() {
        let node_a = NodeId::new("a");
        let node_b = NodeId::new("b");
        let mut clock_a = VectorClock::new();
        let mut clock_b = VectorClock::new();

        let mut tx_a = AlgebraicTransaction::new();
        tx_a.add_operation(union_op("tags", &["featured"]));

        let mut tx_b = AlgebraicTransaction::new();
        tx_b.add_operation(union_op("tags", &["new", "sale"]));

        let update_a = LocalCommitProtocol::commit_local(&tx_a, &node_a, &mut clock_a).unwrap();
        let update_b = LocalCommitProtocol::commit_local(&tx_b, &node_b, &mut clock_b).unwrap();

        let merged = LocalCommitProtocol::merge_updates(&update_a, &update_b).unwrap();

        if let AlgebraicValue::StringSet(s) = merged.operations()[0].value() {
            assert_eq!(s.len(), 3);
            assert!(s.contains("featured"));
            assert!(s.contains("new"));
            assert!(s.contains("sale"));
        } else {
            panic!("Expected StringSet");
        }
    }

    // ============ CRITICAL: Commutativity Tests ============

    #[test]
    fn test_merge_is_commutative_add() {
        let node_a = NodeId::new("a");
        let node_b = NodeId::new("b");
        let mut clock_a = VectorClock::new();
        let mut clock_b = VectorClock::new();

        let mut tx_a = AlgebraicTransaction::new();
        tx_a.add_operation(add_op("counter", 5));

        let mut tx_b = AlgebraicTransaction::new();
        tx_b.add_operation(add_op("counter", 3));

        let update_a = LocalCommitProtocol::commit_local(&tx_a, &node_a, &mut clock_a).unwrap();
        let update_b = LocalCommitProtocol::commit_local(&tx_b, &node_b, &mut clock_b).unwrap();

        // merge(A, B) should equal merge(B, A)
        let merged_ab = LocalCommitProtocol::merge_updates(&update_a, &update_b).unwrap();
        let merged_ba = LocalCommitProtocol::merge_updates(&update_b, &update_a).unwrap();

        let get_counter = |u: &VersionedUpdate| {
            u.operations()
                .iter()
                .find(|op| op.key() == "counter")
                .and_then(|op| op.value().as_integer())
        };

        assert_eq!(get_counter(&merged_ab), get_counter(&merged_ba));
        assert_eq!(get_counter(&merged_ab), Some(8));
    }

    #[test]
    fn test_merge_is_commutative_max() {
        let node_a = NodeId::new("a");
        let node_b = NodeId::new("b");
        let mut clock_a = VectorClock::new();
        let mut clock_b = VectorClock::new();

        let mut tx_a = AlgebraicTransaction::new();
        tx_a.add_operation(max_op("ts", 100));

        let mut tx_b = AlgebraicTransaction::new();
        tx_b.add_operation(max_op("ts", 200));

        let update_a = LocalCommitProtocol::commit_local(&tx_a, &node_a, &mut clock_a).unwrap();
        let update_b = LocalCommitProtocol::commit_local(&tx_b, &node_b, &mut clock_b).unwrap();

        let merged_ab = LocalCommitProtocol::merge_updates(&update_a, &update_b).unwrap();
        let merged_ba = LocalCommitProtocol::merge_updates(&update_b, &update_a).unwrap();

        assert_eq!(
            merged_ab.operations()[0].value().as_integer(),
            merged_ba.operations()[0].value().as_integer()
        );
    }

    #[test]
    fn test_merge_is_commutative_union() {
        let node_a = NodeId::new("a");
        let node_b = NodeId::new("b");
        let mut clock_a = VectorClock::new();
        let mut clock_b = VectorClock::new();

        let mut tx_a = AlgebraicTransaction::new();
        tx_a.add_operation(union_op("tags", &["x", "y"]));

        let mut tx_b = AlgebraicTransaction::new();
        tx_b.add_operation(union_op("tags", &["y", "z"]));

        let update_a = LocalCommitProtocol::commit_local(&tx_a, &node_a, &mut clock_a).unwrap();
        let update_b = LocalCommitProtocol::commit_local(&tx_b, &node_b, &mut clock_b).unwrap();

        let merged_ab = LocalCommitProtocol::merge_updates(&update_a, &update_b).unwrap();
        let merged_ba = LocalCommitProtocol::merge_updates(&update_b, &update_a).unwrap();

        assert_eq!(merged_ab.operations()[0].value(), merged_ba.operations()[0].value());
    }

    // ============ CRITICAL: Associativity Tests ============

    #[test]
    fn test_merge_is_associative_add() {
        let node_a = NodeId::new("a");
        let node_b = NodeId::new("b");
        let node_c = NodeId::new("c");
        let mut clock_a = VectorClock::new();
        let mut clock_b = VectorClock::new();
        let mut clock_c = VectorClock::new();

        let mut tx_a = AlgebraicTransaction::new();
        tx_a.add_operation(add_op("counter", 1));

        let mut tx_b = AlgebraicTransaction::new();
        tx_b.add_operation(add_op("counter", 2));

        let mut tx_c = AlgebraicTransaction::new();
        tx_c.add_operation(add_op("counter", 3));

        let update_a = LocalCommitProtocol::commit_local(&tx_a, &node_a, &mut clock_a).unwrap();
        let update_b = LocalCommitProtocol::commit_local(&tx_b, &node_b, &mut clock_b).unwrap();
        let update_c = LocalCommitProtocol::commit_local(&tx_c, &node_c, &mut clock_c).unwrap();

        // (A merge B) merge C
        let ab = LocalCommitProtocol::merge_updates(&update_a, &update_b).unwrap();
        let ab_c = LocalCommitProtocol::merge_updates(&ab, &update_c).unwrap();

        // A merge (B merge C)
        let bc = LocalCommitProtocol::merge_updates(&update_b, &update_c).unwrap();
        let a_bc = LocalCommitProtocol::merge_updates(&update_a, &bc).unwrap();

        let get_counter = |u: &VersionedUpdate| {
            u.operations()
                .iter()
                .find(|op| op.key() == "counter")
                .and_then(|op| op.value().as_integer())
        };

        // Both should equal 1 + 2 + 3 = 6
        assert_eq!(get_counter(&ab_c), Some(6));
        assert_eq!(get_counter(&a_bc), Some(6));
        assert_eq!(get_counter(&ab_c), get_counter(&a_bc));
    }

    #[test]
    fn test_merge_is_associative_max() {
        let node_a = NodeId::new("a");
        let node_b = NodeId::new("b");
        let node_c = NodeId::new("c");
        let mut clock_a = VectorClock::new();
        let mut clock_b = VectorClock::new();
        let mut clock_c = VectorClock::new();

        let mut tx_a = AlgebraicTransaction::new();
        tx_a.add_operation(max_op("ts", 100));

        let mut tx_b = AlgebraicTransaction::new();
        tx_b.add_operation(max_op("ts", 300));

        let mut tx_c = AlgebraicTransaction::new();
        tx_c.add_operation(max_op("ts", 200));

        let update_a = LocalCommitProtocol::commit_local(&tx_a, &node_a, &mut clock_a).unwrap();
        let update_b = LocalCommitProtocol::commit_local(&tx_b, &node_b, &mut clock_b).unwrap();
        let update_c = LocalCommitProtocol::commit_local(&tx_c, &node_c, &mut clock_c).unwrap();

        let ab = LocalCommitProtocol::merge_updates(&update_a, &update_b).unwrap();
        let ab_c = LocalCommitProtocol::merge_updates(&ab, &update_c).unwrap();

        let bc = LocalCommitProtocol::merge_updates(&update_b, &update_c).unwrap();
        let a_bc = LocalCommitProtocol::merge_updates(&update_a, &bc).unwrap();

        // Both should equal max(100, 300, 200) = 300
        assert_eq!(ab_c.operations()[0].value().as_integer(), Some(300));
        assert_eq!(a_bc.operations()[0].value().as_integer(), Some(300));
    }

    // ============ merge_all Tests ============

    #[test]
    fn test_merge_all() {
        let nodes: Vec<NodeId> = (0..5).map(|i| NodeId::new(format!("node-{}", i))).collect();
        let mut clocks: Vec<VectorClock> = (0..5).map(|_| VectorClock::new()).collect();

        let updates: Vec<VersionedUpdate> = nodes
            .iter()
            .zip(clocks.iter_mut())
            .enumerate()
            .map(|(i, (node, clock))| {
                let mut tx = AlgebraicTransaction::new();
                tx.add_operation(add_op("total", (i + 1) as i64));
                LocalCommitProtocol::commit_local(&tx, node, clock).unwrap()
            })
            .collect();

        let merged = LocalCommitProtocol::merge_all(&updates).unwrap();

        // 1 + 2 + 3 + 4 + 5 = 15
        assert_eq!(merged.operations()[0].value().as_integer(), Some(15));
    }

    #[test]
    fn test_merge_all_equals_pairwise() {
        let node_a = NodeId::new("a");
        let node_b = NodeId::new("b");
        let node_c = NodeId::new("c");
        let mut clock_a = VectorClock::new();
        let mut clock_b = VectorClock::new();
        let mut clock_c = VectorClock::new();

        let mut tx_a = AlgebraicTransaction::new();
        tx_a.add_operation(add_op("counter", 10));

        let mut tx_b = AlgebraicTransaction::new();
        tx_b.add_operation(add_op("counter", 20));

        let mut tx_c = AlgebraicTransaction::new();
        tx_c.add_operation(add_op("counter", 30));

        let update_a = LocalCommitProtocol::commit_local(&tx_a, &node_a, &mut clock_a).unwrap();
        let update_b = LocalCommitProtocol::commit_local(&tx_b, &node_b, &mut clock_b).unwrap();
        let update_c = LocalCommitProtocol::commit_local(&tx_c, &node_c, &mut clock_c).unwrap();

        // merge_all should equal pairwise merge
        let merged_all = LocalCommitProtocol::merge_all(&[update_a.clone(), update_b.clone(), update_c.clone()]).unwrap();

        let ab = LocalCommitProtocol::merge_updates(&update_a, &update_b).unwrap();
        let abc_pairwise = LocalCommitProtocol::merge_updates(&ab, &update_c).unwrap();

        assert_eq!(
            merged_all.operations()[0].value().as_integer(),
            abc_pairwise.operations()[0].value().as_integer()
        );
        assert_eq!(merged_all.operations()[0].value().as_integer(), Some(60));
    }

    // ============ VersionedUpdate Tests ============

    #[test]
    fn test_versioned_update_compare() {
        let node_a = NodeId::new("a");
        let node_b = NodeId::new("b");
        let mut clock_a = VectorClock::new();
        let mut clock_b = VectorClock::new();

        let mut tx_a = AlgebraicTransaction::new();
        tx_a.add_operation(add_op("x", 1));

        let mut tx_b = AlgebraicTransaction::new();
        tx_b.add_operation(add_op("y", 1));

        let update_a = LocalCommitProtocol::commit_local(&tx_a, &node_a, &mut clock_a).unwrap();
        let update_b = LocalCommitProtocol::commit_local(&tx_b, &node_b, &mut clock_b).unwrap();

        // These should be concurrent (different nodes, no communication)
        assert!(update_a.is_concurrent_with(&update_b));
        assert_eq!(update_a.compare(&update_b), CausalOrder::Concurrent);
    }

    #[test]
    fn test_versioned_update_causality() {
        let node = NodeId::new("node-1");
        let mut clock = VectorClock::new();

        let mut tx1 = AlgebraicTransaction::new();
        tx1.add_operation(add_op("counter", 1));

        let mut tx2 = AlgebraicTransaction::new();
        tx2.add_operation(add_op("counter", 2));

        let update1 = LocalCommitProtocol::commit_local(&tx1, &node, &mut clock).unwrap();
        let update2 = LocalCommitProtocol::commit_local(&tx2, &node, &mut clock).unwrap();

        // update1 happened before update2 (same node, sequential)
        assert_eq!(update1.compare(&update2), CausalOrder::Before);
        assert_eq!(update2.compare(&update1), CausalOrder::After);
    }

    // ============ Error Handling Tests ============

    #[test]
    fn test_error_display() {
        let err = LocalCommitError::NonAlgebraic {
            keys: vec!["name".to_string()],
            message: "test".to_string(),
        };
        let s = format!("{}", err);
        assert!(s.contains("name"));
        assert!(s.contains("test"));
    }

    // ============ Serialization Tests ============

    #[test]
    fn test_operation_serialization() {
        let op = add_op("counter", 42);
        let json = serde_json::to_string(&op).unwrap();
        let parsed: AlgebraicOperation = serde_json::from_str(&json).unwrap();
        assert_eq!(op, parsed);
    }

    #[test]
    fn test_transaction_serialization() {
        let mut tx = AlgebraicTransaction::new();
        tx.add_operation(add_op("a", 1));
        tx.add_operation(max_op("b", 2));
        tx.set_metadata("user", "test");

        let json = serde_json::to_string(&tx).unwrap();
        let parsed: AlgebraicTransaction = serde_json::from_str(&json).unwrap();

        assert_eq!(tx.len(), parsed.len());
        assert_eq!(tx.get_metadata("user"), parsed.get_metadata("user"));
    }

    #[test]
    fn test_versioned_update_serialization() {
        let node = NodeId::new("node-1");
        let mut clock = VectorClock::new();

        let mut tx = AlgebraicTransaction::new();
        tx.add_operation(add_op("counter", 5));

        let update = LocalCommitProtocol::commit_local(&tx, &node, &mut clock).unwrap();

        let json = serde_json::to_string(&update).unwrap();
        let parsed: VersionedUpdate = serde_json::from_str(&json).unwrap();

        assert_eq!(update.origin_node(), parsed.origin_node());
        assert_eq!(update.operations().len(), parsed.operations().len());
    }

    // ============ Complex Scenario Tests ============

    #[test]
    fn test_realistic_multi_key_scenario() {
        // Simulate an e-commerce scenario:
        // - Node SF: increment page_views, add tags, update last_seen
        // - Node Tokyo: increment page_views, add different tags, update last_seen

        let node_sf = NodeId::new("san-francisco");
        let node_tokyo = NodeId::new("tokyo");
        let mut clock_sf = VectorClock::new();
        let mut clock_tokyo = VectorClock::new();

        // SF transaction
        let mut tx_sf = AlgebraicTransaction::new();
        tx_sf.add_operation(add_op("page_views", 100));
        tx_sf.add_operation(union_op("tags", &["featured", "promoted"]));
        tx_sf.add_operation(max_op("last_seen", 1000));

        // Tokyo transaction
        let mut tx_tokyo = AlgebraicTransaction::new();
        tx_tokyo.add_operation(add_op("page_views", 50));
        tx_tokyo.add_operation(union_op("tags", &["new", "promoted"]));
        tx_tokyo.add_operation(max_op("last_seen", 1200));

        let update_sf = LocalCommitProtocol::commit_local(&tx_sf, &node_sf, &mut clock_sf).unwrap();
        let update_tokyo = LocalCommitProtocol::commit_local(&tx_tokyo, &node_tokyo, &mut clock_tokyo).unwrap();

        // Merge them
        let merged = LocalCommitProtocol::merge_updates(&update_sf, &update_tokyo).unwrap();

        // Find operations by key
        let find_op = |key: &str| merged.operations().iter().find(|op| op.key() == key).unwrap();

        // page_views: 100 + 50 = 150
        assert_eq!(find_op("page_views").value().as_integer(), Some(150));

        // last_seen: max(1000, 1200) = 1200
        assert_eq!(find_op("last_seen").value().as_integer(), Some(1200));

        // tags: {"featured", "promoted"} ∪ {"new", "promoted"} = {"featured", "promoted", "new"}
        if let AlgebraicValue::StringSet(tags) = find_op("tags").value() {
            assert_eq!(tags.len(), 3);
            assert!(tags.contains("featured"));
            assert!(tags.contains("promoted"));
            assert!(tags.contains("new"));
        } else {
            panic!("Expected StringSet");
        }
    }

    #[test]
    fn test_five_node_convergence() {
        // Simulate 5 nodes all incrementing the same counter concurrently
        // All permutations of merge order should produce the same result

        let nodes: Vec<NodeId> = vec!["sf", "tokyo", "london", "sydney", "berlin"]
            .into_iter()
            .map(NodeId::new)
            .collect();

        let mut clocks: Vec<VectorClock> = (0..5).map(|_| VectorClock::new()).collect();

        let updates: Vec<VersionedUpdate> = nodes
            .iter()
            .zip(clocks.iter_mut())
            .enumerate()
            .map(|(i, (node, clock))| {
                let mut tx = AlgebraicTransaction::new();
                tx.add_operation(add_op("global_counter", (i + 1) as i64 * 10)); // 10, 20, 30, 40, 50
                LocalCommitProtocol::commit_local(&tx, node, clock).unwrap()
            })
            .collect();

        // Merge in order: ((((0, 1), 2), 3), 4)
        let merge_forward = {
            let mut result = updates[0].clone();
            for update in updates.iter().skip(1) {
                result = LocalCommitProtocol::merge_updates(&result, update).unwrap();
            }
            result
        };

        // Merge in reverse order: ((((4, 3), 2), 1), 0)
        let merge_reverse = {
            let mut result = updates[4].clone();
            for update in updates.iter().rev().skip(1) {
                result = LocalCommitProtocol::merge_updates(&result, update).unwrap();
            }
            result
        };

        // Merge using merge_all
        let merge_all_result = LocalCommitProtocol::merge_all(&updates).unwrap();

        let get_counter = |u: &VersionedUpdate| {
            u.operations()
                .iter()
                .find(|op| op.key() == "global_counter")
                .and_then(|op| op.value().as_integer())
        };

        // All should equal 10 + 20 + 30 + 40 + 50 = 150
        assert_eq!(get_counter(&merge_forward), Some(150));
        assert_eq!(get_counter(&merge_reverse), Some(150));
        assert_eq!(get_counter(&merge_all_result), Some(150));
    }
}
