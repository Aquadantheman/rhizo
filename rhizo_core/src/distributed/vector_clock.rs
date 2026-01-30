//! Vector clock implementation for causality tracking in distributed systems.
//!
//! Vector clocks provide a mechanism to track the "happened-before" relationship
//! between events in a distributed system, enabling correct merge decisions.
//!
//! # Theory
//!
//! A vector clock V for N nodes is a vector [v₁, v₂, ..., vₙ] where vᵢ represents
//! the logical time at node i. The key properties are:
//!
//! - **Happened-before**: Event A happened before B iff V(A) < V(B)
//! - **Concurrent**: Events are concurrent iff neither V(A) < V(B) nor V(B) < V(A)
//!
//! # Usage
//!
//! ```
//! use rhizo_core::distributed::{VectorClock, NodeId};
//!
//! let node_a = NodeId::new("node-a");
//! let node_b = NodeId::new("node-b");
//!
//! // Node A performs an operation
//! let mut clock_a = VectorClock::new();
//! clock_a.tick(&node_a);
//!
//! // Node B performs an operation
//! let mut clock_b = VectorClock::new();
//! clock_b.tick(&node_b);
//!
//! // These are concurrent (neither happened before the other)
//! assert!(clock_a.concurrent_with(&clock_b));
//!
//! // Node A receives message from B
//! clock_a.merge(&clock_b);
//! clock_a.tick(&node_a);
//!
//! // Now clock_a is strictly after clock_b
//! assert!(clock_b.happened_before(&clock_a));
//! ```

use serde::{Deserialize, Serialize};
use std::cmp::Ordering;
use std::collections::HashMap;
use std::fmt;

/// Unique identifier for a node in the distributed system.
///
/// Node IDs should be stable across restarts and unique across the cluster.
#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub struct NodeId(String);

impl NodeId {
    /// Create a new node ID.
    pub fn new(id: impl Into<String>) -> Self {
        Self(id.into())
    }

    /// Get the string representation of this node ID.
    pub fn as_str(&self) -> &str {
        &self.0
    }
}

impl fmt::Display for NodeId {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}", self.0)
    }
}

impl From<&str> for NodeId {
    fn from(s: &str) -> Self {
        Self::new(s)
    }
}

impl From<String> for NodeId {
    fn from(s: String) -> Self {
        Self::new(s)
    }
}

impl From<&NodeId> for NodeId {
    fn from(n: &NodeId) -> Self {
        n.clone()
    }
}

/// A vector clock for tracking causality in distributed systems.
///
/// Vector clocks map node IDs to logical timestamps. They enable determining
/// whether events happened-before each other or are concurrent.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, Default)]
pub struct VectorClock {
    /// Map from node ID to logical timestamp
    clocks: HashMap<NodeId, u64>,
}

impl VectorClock {
    /// Create a new, empty vector clock.
    ///
    /// All logical times start at 0 (implicitly, since missing entries are 0).
    pub fn new() -> Self {
        Self {
            clocks: HashMap::new(),
        }
    }

    /// Create a vector clock with a single node's time initialized.
    pub fn with_node(node_id: impl Into<NodeId>, time: u64) -> Self {
        let mut clock = Self::new();
        clock.clocks.insert(node_id.into(), time);
        clock
    }

    /// Increment this node's logical time.
    ///
    /// Call this before performing a local operation that should be tracked.
    ///
    /// # Example
    /// ```
    /// use rhizo_core::distributed::{VectorClock, NodeId};
    ///
    /// let node = NodeId::new("node-1");
    /// let mut clock = VectorClock::new();
    ///
    /// clock.tick(&node);
    /// assert_eq!(clock.get(&node), 1);
    ///
    /// clock.tick(&node);
    /// assert_eq!(clock.get(&node), 2);
    /// ```
    pub fn tick(&mut self, node_id: &NodeId) {
        *self.clocks.entry(node_id.clone()).or_insert(0) += 1;
    }

    /// Get the logical time for a specific node.
    ///
    /// Returns 0 if the node has no entry (never observed).
    pub fn get(&self, node_id: &NodeId) -> u64 {
        self.clocks.get(node_id).copied().unwrap_or(0)
    }

    /// Set the logical time for a specific node.
    ///
    /// This is primarily used for testing or initialization.
    pub fn set(&mut self, node_id: impl Into<NodeId>, time: u64) {
        self.clocks.insert(node_id.into(), time);
    }

    /// Merge another vector clock into this one.
    ///
    /// After merging, this clock will have the component-wise maximum of both clocks.
    /// This should be called when receiving a message from another node.
    ///
    /// # Example
    /// ```
    /// use rhizo_core::distributed::{VectorClock, NodeId};
    ///
    /// let node_a = NodeId::new("a");
    /// let node_b = NodeId::new("b");
    ///
    /// let mut clock1 = VectorClock::new();
    /// clock1.set(&node_a, 3);
    /// clock1.set(&node_b, 1);
    ///
    /// let mut clock2 = VectorClock::new();
    /// clock2.set(&node_a, 2);
    /// clock2.set(&node_b, 4);
    ///
    /// clock1.merge(&clock2);
    ///
    /// assert_eq!(clock1.get(&node_a), 3); // max(3, 2)
    /// assert_eq!(clock1.get(&node_b), 4); // max(1, 4)
    /// ```
    pub fn merge(&mut self, other: &VectorClock) {
        for (node_id, &time) in &other.clocks {
            let entry = self.clocks.entry(node_id.clone()).or_insert(0);
            *entry = (*entry).max(time);
        }
    }

    /// Check if this clock happened strictly before another clock.
    ///
    /// Returns true iff all components of self are ≤ other, and at least one is strictly <.
    ///
    /// # Example
    /// ```
    /// use rhizo_core::distributed::{VectorClock, NodeId};
    ///
    /// let node = NodeId::new("node-1");
    ///
    /// let mut clock1 = VectorClock::new();
    /// clock1.set(&node, 1);
    ///
    /// let mut clock2 = VectorClock::new();
    /// clock2.set(&node, 2);
    ///
    /// assert!(clock1.happened_before(&clock2));
    /// assert!(!clock2.happened_before(&clock1));
    /// ```
    pub fn happened_before(&self, other: &VectorClock) -> bool {
        self.partial_cmp(other) == Some(Ordering::Less)
    }

    /// Check if this clock happened strictly after another clock.
    pub fn happened_after(&self, other: &VectorClock) -> bool {
        other.happened_before(self)
    }

    /// Check if two clocks are concurrent (neither happened before the other).
    ///
    /// Concurrent events may need algebraic merging.
    ///
    /// # Example
    /// ```
    /// use rhizo_core::distributed::{VectorClock, NodeId};
    ///
    /// let node_a = NodeId::new("a");
    /// let node_b = NodeId::new("b");
    ///
    /// let mut clock1 = VectorClock::new();
    /// clock1.set(&node_a, 1);
    ///
    /// let mut clock2 = VectorClock::new();
    /// clock2.set(&node_b, 1);
    ///
    /// // Neither happened before the other
    /// assert!(clock1.concurrent_with(&clock2));
    /// ```
    pub fn concurrent_with(&self, other: &VectorClock) -> bool {
        self.partial_cmp(other).is_none()
    }

    /// Compare two clocks and return the causal relationship.
    ///
    /// Returns:
    /// - `CausalOrder::Before` if self happened before other
    /// - `CausalOrder::After` if self happened after other
    /// - `CausalOrder::Equal` if clocks are identical
    /// - `CausalOrder::Concurrent` if events are concurrent
    pub fn compare(&self, other: &VectorClock) -> CausalOrder {
        match self.partial_cmp(other) {
            Some(Ordering::Less) => CausalOrder::Before,
            Some(Ordering::Greater) => CausalOrder::After,
            Some(Ordering::Equal) => CausalOrder::Equal,
            None => CausalOrder::Concurrent,
        }
    }

    /// Get all node IDs that have entries in this clock.
    pub fn nodes(&self) -> impl Iterator<Item = &NodeId> {
        self.clocks.keys()
    }

    /// Get all (node_id, time) entries in this clock.
    pub fn entries(&self) -> impl Iterator<Item = (&NodeId, &u64)> {
        self.clocks.iter()
    }

    /// Get the number of nodes with entries in this clock.
    pub fn node_count(&self) -> usize {
        self.clocks.len()
    }

    /// Check if this clock is empty (no nodes have ticked).
    pub fn is_empty(&self) -> bool {
        self.clocks.is_empty() || self.clocks.values().all(|&v| v == 0)
    }

    /// Get the sum of all logical times (useful for rough ordering heuristics).
    pub fn sum(&self) -> u64 {
        self.clocks.values().sum()
    }

    /// Create a clock that is the component-wise maximum of two clocks.
    ///
    /// This is useful for computing the merge without modifying either clock.
    pub fn max(a: &VectorClock, b: &VectorClock) -> VectorClock {
        let mut result = a.clone();
        result.merge(b);
        result
    }

    /// Increment and return a copy (useful for functional style).
    pub fn ticked(&self, node_id: &NodeId) -> VectorClock {
        let mut result = self.clone();
        result.tick(node_id);
        result
    }
}

impl PartialOrd for VectorClock {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        // Collect all node IDs from both clocks
        let all_nodes: std::collections::HashSet<_> =
            self.clocks.keys().chain(other.clocks.keys()).collect();

        let mut less_than = false;
        let mut greater_than = false;

        for node_id in all_nodes {
            let self_time = self.get(node_id);
            let other_time = other.get(node_id);

            match self_time.cmp(&other_time) {
                Ordering::Less => less_than = true,
                Ordering::Greater => greater_than = true,
                Ordering::Equal => {}
            }

            // If we've seen both less and greater, they're concurrent
            if less_than && greater_than {
                return None;
            }
        }

        match (less_than, greater_than) {
            (true, false) => Some(Ordering::Less),
            (false, true) => Some(Ordering::Greater),
            (false, false) => Some(Ordering::Equal),
            (true, true) => None, // Concurrent (already handled above, but for clarity)
        }
    }
}

impl fmt::Display for VectorClock {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        let entries: Vec<String> = self
            .clocks
            .iter()
            .map(|(k, v)| format!("{}:{}", k, v))
            .collect();
        write!(f, "[{}]", entries.join(", "))
    }
}

/// The causal relationship between two events.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CausalOrder {
    /// First event happened before second.
    Before,
    /// First event happened after second.
    After,
    /// Events are concurrent (neither happened before the other).
    Concurrent,
    /// Events have identical timestamps.
    Equal,
}

impl CausalOrder {
    /// Check if merge is needed (concurrent events require merge).
    pub fn needs_merge(&self) -> bool {
        matches!(self, Self::Concurrent)
    }

    /// Check if update should be applied (other is newer or concurrent).
    pub fn should_apply(&self) -> bool {
        matches!(self, Self::Before | Self::Concurrent)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn node(s: &str) -> NodeId {
        NodeId::new(s)
    }

    #[test]
    fn test_new_clock_is_empty() {
        let clock = VectorClock::new();
        assert!(clock.is_empty());
        assert_eq!(clock.node_count(), 0);
    }

    #[test]
    fn test_tick_increments_clock() {
        let mut clock = VectorClock::new();
        let n = node("node-1");

        assert_eq!(clock.get(&n), 0);

        clock.tick(&n);
        assert_eq!(clock.get(&n), 1);

        clock.tick(&n);
        assert_eq!(clock.get(&n), 2);

        clock.tick(&n);
        assert_eq!(clock.get(&n), 3);
    }

    #[test]
    fn test_tick_only_affects_one_node() {
        let mut clock = VectorClock::new();
        let n1 = node("node-1");
        let n2 = node("node-2");

        clock.tick(&n1);
        clock.tick(&n1);
        clock.tick(&n2);

        assert_eq!(clock.get(&n1), 2);
        assert_eq!(clock.get(&n2), 1);
    }

    #[test]
    fn test_merge_takes_max() {
        let n1 = node("a");
        let n2 = node("b");
        let n3 = node("c");

        let mut clock1 = VectorClock::new();
        clock1.set(&n1, 3);
        clock1.set(&n2, 1);

        let mut clock2 = VectorClock::new();
        clock2.set(&n1, 2);
        clock2.set(&n2, 4);
        clock2.set(&n3, 5);

        clock1.merge(&clock2);

        assert_eq!(clock1.get(&n1), 3); // max(3, 2)
        assert_eq!(clock1.get(&n2), 4); // max(1, 4)
        assert_eq!(clock1.get(&n3), 5); // max(0, 5)
    }

    #[test]
    fn test_happened_before_simple() {
        let n = node("node-1");

        let mut clock1 = VectorClock::new();
        clock1.set(&n, 1);

        let mut clock2 = VectorClock::new();
        clock2.set(&n, 2);

        assert!(clock1.happened_before(&clock2));
        assert!(!clock2.happened_before(&clock1));
        assert!(!clock1.happened_before(&clock1));
    }

    #[test]
    fn test_happened_before_multiple_nodes() {
        let n1 = node("a");
        let n2 = node("b");

        let mut clock1 = VectorClock::new();
        clock1.set(&n1, 1);
        clock1.set(&n2, 2);

        let mut clock2 = VectorClock::new();
        clock2.set(&n1, 2);
        clock2.set(&n2, 3);

        assert!(clock1.happened_before(&clock2));
        assert!(!clock2.happened_before(&clock1));
    }

    #[test]
    fn test_concurrent_different_nodes() {
        let n1 = node("a");
        let n2 = node("b");

        let mut clock1 = VectorClock::new();
        clock1.set(&n1, 1);

        let mut clock2 = VectorClock::new();
        clock2.set(&n2, 1);

        assert!(clock1.concurrent_with(&clock2));
        assert!(clock2.concurrent_with(&clock1));
        assert!(!clock1.happened_before(&clock2));
        assert!(!clock2.happened_before(&clock1));
    }

    #[test]
    fn test_concurrent_crossed_updates() {
        let n1 = node("a");
        let n2 = node("b");

        // Clock1: a=2, b=1
        let mut clock1 = VectorClock::new();
        clock1.set(&n1, 2);
        clock1.set(&n2, 1);

        // Clock2: a=1, b=2
        let mut clock2 = VectorClock::new();
        clock2.set(&n1, 1);
        clock2.set(&n2, 2);

        // Neither dominates: concurrent
        assert!(clock1.concurrent_with(&clock2));
        assert!(clock2.concurrent_with(&clock1));
    }

    #[test]
    fn test_compare_all_cases() {
        let n1 = node("a");
        let n2 = node("b");

        // Equal
        let clock1 = VectorClock::with_node(&n1, 1);
        let clock2 = VectorClock::with_node(&n1, 1);
        assert_eq!(clock1.compare(&clock2), CausalOrder::Equal);

        // Before
        let clock3 = VectorClock::with_node(&n1, 2);
        assert_eq!(clock1.compare(&clock3), CausalOrder::Before);

        // After
        assert_eq!(clock3.compare(&clock1), CausalOrder::After);

        // Concurrent
        let clock4 = VectorClock::with_node(&n2, 1);
        assert_eq!(clock1.compare(&clock4), CausalOrder::Concurrent);
    }

    #[test]
    fn test_message_passing_scenario() {
        // Simulate: Node A sends message to Node B
        let node_a = node("a");
        let node_b = node("b");

        // A does local work
        let mut clock_a = VectorClock::new();
        clock_a.tick(&node_a);
        clock_a.tick(&node_a);
        assert_eq!(clock_a.get(&node_a), 2);

        // B does local work
        let mut clock_b = VectorClock::new();
        clock_b.tick(&node_b);
        assert_eq!(clock_b.get(&node_b), 1);

        // At this point, A and B are concurrent
        assert!(clock_a.concurrent_with(&clock_b));

        // A sends message to B (B receives and merges)
        clock_b.merge(&clock_a);
        clock_b.tick(&node_b); // Increment after receive

        // Now B has seen A's events
        assert!(clock_a.happened_before(&clock_b));
        assert_eq!(clock_b.get(&node_a), 2); // Saw A's time
        assert_eq!(clock_b.get(&node_b), 2); // Own time incremented
    }

    #[test]
    fn test_three_node_scenario() {
        let node_a = node("a");
        let node_b = node("b");
        let node_c = node("c");

        // Each node does independent work
        let mut clock_a = VectorClock::new();
        clock_a.tick(&node_a);

        let mut clock_b = VectorClock::new();
        clock_b.tick(&node_b);

        let mut clock_c = VectorClock::new();
        clock_c.tick(&node_c);

        // All are concurrent
        assert!(clock_a.concurrent_with(&clock_b));
        assert!(clock_b.concurrent_with(&clock_c));
        assert!(clock_a.concurrent_with(&clock_c));

        // A receives from B and C
        clock_a.merge(&clock_b);
        clock_a.merge(&clock_c);
        clock_a.tick(&node_a);

        // A is now after all others
        assert!(clock_b.happened_before(&clock_a));
        assert!(clock_c.happened_before(&clock_a));
    }

    #[test]
    fn test_serialization_roundtrip() {
        let n1 = node("node-1");
        let n2 = node("node-2");

        let mut clock = VectorClock::new();
        clock.set(&n1, 42);
        clock.set(&n2, 17);

        let json = serde_json::to_string(&clock).unwrap();
        let parsed: VectorClock = serde_json::from_str(&json).unwrap();

        assert_eq!(clock, parsed);
        assert_eq!(parsed.get(&n1), 42);
        assert_eq!(parsed.get(&n2), 17);
    }

    #[test]
    fn test_display() {
        let mut clock = VectorClock::new();
        clock.set(node("a"), 1);

        let s = clock.to_string();
        assert!(s.contains("a:1"));
    }

    #[test]
    fn test_max_static_method() {
        let n1 = node("a");
        let n2 = node("b");

        let mut clock1 = VectorClock::new();
        clock1.set(&n1, 3);
        clock1.set(&n2, 1);

        let mut clock2 = VectorClock::new();
        clock2.set(&n1, 2);
        clock2.set(&n2, 4);

        let merged = VectorClock::max(&clock1, &clock2);

        // Original clocks unchanged
        assert_eq!(clock1.get(&n1), 3);
        assert_eq!(clock1.get(&n2), 1);

        // Merged has max
        assert_eq!(merged.get(&n1), 3);
        assert_eq!(merged.get(&n2), 4);
    }

    #[test]
    fn test_ticked_returns_copy() {
        let n = node("node-1");
        let clock1 = VectorClock::with_node(&n, 1);
        let clock2 = clock1.ticked(&n);

        // Original unchanged
        assert_eq!(clock1.get(&n), 1);
        // Copy incremented
        assert_eq!(clock2.get(&n), 2);
    }

    #[test]
    fn test_causal_order_needs_merge() {
        assert!(!CausalOrder::Before.needs_merge());
        assert!(!CausalOrder::After.needs_merge());
        assert!(!CausalOrder::Equal.needs_merge());
        assert!(CausalOrder::Concurrent.needs_merge());
    }

    #[test]
    fn test_causal_order_should_apply() {
        assert!(CausalOrder::Before.should_apply()); // Other is newer
        assert!(!CausalOrder::After.should_apply()); // We're newer
        assert!(!CausalOrder::Equal.should_apply()); // Same
        assert!(CausalOrder::Concurrent.should_apply()); // Need to merge
    }

    #[test]
    fn test_empty_clock_comparison() {
        let empty1 = VectorClock::new();
        let empty2 = VectorClock::new();

        assert_eq!(empty1.compare(&empty2), CausalOrder::Equal);
        assert!(!empty1.happened_before(&empty2));
        assert!(!empty1.concurrent_with(&empty2));
    }

    #[test]
    fn test_empty_vs_nonempty() {
        let empty = VectorClock::new();
        let mut nonempty = VectorClock::new();
        nonempty.tick(&node("a"));

        assert!(empty.happened_before(&nonempty));
        assert!(!nonempty.happened_before(&empty));
    }

    #[test]
    fn test_sum() {
        let mut clock = VectorClock::new();
        clock.set(node("a"), 10);
        clock.set(node("b"), 20);
        clock.set(node("c"), 30);

        assert_eq!(clock.sum(), 60);
    }

    #[test]
    fn test_nodes_iterator() {
        let mut clock = VectorClock::new();
        clock.set(node("a"), 1);
        clock.set(node("b"), 2);

        let nodes: Vec<&NodeId> = clock.nodes().collect();
        assert_eq!(nodes.len(), 2);
    }

    // Property-based style tests

    #[test]
    fn test_merge_is_commutative() {
        let n1 = node("a");
        let n2 = node("b");

        let mut clock1 = VectorClock::new();
        clock1.set(&n1, 3);
        clock1.set(&n2, 1);

        let mut clock2 = VectorClock::new();
        clock2.set(&n1, 2);
        clock2.set(&n2, 4);

        // merge(c1, c2) should equal merge(c2, c1)
        let merged_1_2 = VectorClock::max(&clock1, &clock2);
        let merged_2_1 = VectorClock::max(&clock2, &clock1);

        assert_eq!(merged_1_2, merged_2_1);
    }

    #[test]
    fn test_merge_is_associative() {
        let n1 = node("a");
        let n2 = node("b");
        let n3 = node("c");

        let mut clock1 = VectorClock::new();
        clock1.set(&n1, 1);

        let mut clock2 = VectorClock::new();
        clock2.set(&n2, 2);

        let mut clock3 = VectorClock::new();
        clock3.set(&n3, 3);

        // (c1 merge c2) merge c3 should equal c1 merge (c2 merge c3)
        let merged_12 = VectorClock::max(&clock1, &clock2);
        let merged_12_3 = VectorClock::max(&merged_12, &clock3);

        let merged_23 = VectorClock::max(&clock2, &clock3);
        let merged_1_23 = VectorClock::max(&clock1, &merged_23);

        assert_eq!(merged_12_3, merged_1_23);
    }

    #[test]
    fn test_merge_is_idempotent() {
        let n = node("a");

        let mut clock = VectorClock::new();
        clock.set(&n, 5);

        let merged_once = VectorClock::max(&clock, &clock);
        let merged_twice = VectorClock::max(&merged_once, &clock);

        assert_eq!(clock, merged_once);
        assert_eq!(clock, merged_twice);
    }

    #[test]
    fn test_happened_before_is_transitive() {
        let n = node("a");

        let clock1 = VectorClock::with_node(&n, 1);
        let clock2 = VectorClock::with_node(&n, 2);
        let clock3 = VectorClock::with_node(&n, 3);

        assert!(clock1.happened_before(&clock2));
        assert!(clock2.happened_before(&clock3));
        // Transitive: clock1 < clock3
        assert!(clock1.happened_before(&clock3));
    }
}
