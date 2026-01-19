//! Core algebraic types for conflict-free merge operations.
//!
//! # Mathematical Foundation
//!
//! Operations that form algebraic structures can be merged conflict-free:
//!
//! ## Semilattice (join-semilattice)
//! - Associative: (a ⊔ b) ⊔ c = a ⊔ (b ⊔ c)
//! - Commutative: a ⊔ b = b ⊔ a
//! - Idempotent: a ⊔ a = a
//!
//! Examples: max(), min(), set union, set intersection
//!
//! ## Abelian Group
//! - Associative: (a + b) + c = a + (b + c)
//! - Commutative: a + b = b + a
//! - Identity: a + 0 = a
//! - Inverse: a + (-a) = 0
//!
//! Examples: addition, counters, deltas
//!
//! If operations form these structures, ORDER DOESN'T MATTER.
//! Conflicts become mathematically impossible!

use serde::{Deserialize, Serialize};
use std::collections::HashSet;

/// Algebraic operation classification.
///
/// This enum categorizes operations by their algebraic properties,
/// determining whether concurrent operations can be automatically merged.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum OpType {
    // === Semilattice Operations (always conflict-free) ===
    /// MAX(a, b) - larger value wins
    ///
    /// Use for: timestamps, version numbers, high-water marks
    /// Guarantees: idempotent, commutative, associative
    SemilatticeMax,

    /// MIN(a, b) - smaller value wins
    ///
    /// Use for: earliest timestamps, minimum thresholds
    /// Guarantees: idempotent, commutative, associative
    SemilatticeMin,

    /// UNION(A, B) - combine all elements
    ///
    /// Use for: tags, permissions, add-only sets
    /// Guarantees: idempotent, commutative, associative
    SemilatticeUnion,

    /// INTERSECT(A, B) - keep common elements
    ///
    /// Use for: required permissions, common features
    /// Guarantees: idempotent, commutative, associative
    SemilatticeIntersect,

    // === Abelian Group Operations (conflict-free via combination) ===
    /// a + b - additive combination
    ///
    /// Use for: counters, inventory deltas, numeric accumulators
    /// Guarantees: commutative, associative, has identity (0) and inverse (-x)
    AbelianAdd,

    /// a * b - multiplicative combination
    ///
    /// Use for: scaling factors, probability combinations
    /// Guarantees: commutative, associative, has identity (1)
    /// Note: No inverse for 0
    AbelianMultiply,

    // === Non-Algebraic Operations (may conflict) ===
    /// Direct value overwrite
    ///
    /// Last-write-wins semantics, requires total ordering.
    /// May conflict if two branches write different values.
    GenericOverwrite,

    /// Conditional update (compare-and-swap style)
    ///
    /// Requires exact version match to succeed.
    /// Always conflicts if two branches attempt CAS on same cell.
    GenericConditional,

    // === Unknown (conservative fallback) ===
    /// Unknown operation type
    ///
    /// Treated as potentially conflicting. Use when operation
    /// semantics cannot be determined.
    Unknown,
}

impl OpType {
    /// Check if this operation type guarantees conflict-free merging.
    ///
    /// Returns `true` for semilattice and Abelian operations,
    /// `false` for generic and unknown operations.
    ///
    /// # Example
    /// ```
    /// use rhizo_core::algebraic::OpType;
    ///
    /// assert!(OpType::SemilatticeMax.is_conflict_free());
    /// assert!(OpType::AbelianAdd.is_conflict_free());
    /// assert!(!OpType::GenericOverwrite.is_conflict_free());
    /// ```
    #[inline]
    pub fn is_conflict_free(&self) -> bool {
        matches!(
            self,
            Self::SemilatticeMax
                | Self::SemilatticeMin
                | Self::SemilatticeUnion
                | Self::SemilatticeIntersect
                | Self::AbelianAdd
                | Self::AbelianMultiply
        )
    }

    /// Check if this is a semilattice operation.
    #[inline]
    pub fn is_semilattice(&self) -> bool {
        matches!(
            self,
            Self::SemilatticeMax
                | Self::SemilatticeMin
                | Self::SemilatticeUnion
                | Self::SemilatticeIntersect
        )
    }

    /// Check if this is an Abelian (group) operation.
    #[inline]
    pub fn is_abelian(&self) -> bool {
        matches!(self, Self::AbelianAdd | Self::AbelianMultiply)
    }

    /// Check if two operation types can be merged.
    ///
    /// Operations can be merged if:
    /// 1. They are the same type, AND
    /// 2. The type is conflict-free
    ///
    /// # Example
    /// ```
    /// use rhizo_core::algebraic::OpType;
    ///
    /// assert!(OpType::AbelianAdd.can_merge_with(&OpType::AbelianAdd));
    /// assert!(!OpType::AbelianAdd.can_merge_with(&OpType::AbelianMultiply));
    /// assert!(!OpType::GenericOverwrite.can_merge_with(&OpType::GenericOverwrite));
    /// ```
    #[inline]
    pub fn can_merge_with(&self, other: &Self) -> bool {
        self == other && self.is_conflict_free()
    }

    /// Get the identity element for this operation type, if applicable.
    ///
    /// - AbelianAdd: 0 (a + 0 = a)
    /// - AbelianMultiply: 1 (a * 1 = a)
    /// - SemilatticeUnion: empty set
    /// - SemilatticeIntersect: universal set (represented as None)
    /// - SemilatticeMax: negative infinity (represented as None)
    /// - SemilatticeMin: positive infinity (represented as None)
    pub fn identity_hint(&self) -> Option<&'static str> {
        match self {
            Self::AbelianAdd => Some("0"),
            Self::AbelianMultiply => Some("1"),
            Self::SemilatticeUnion => Some("empty_set"),
            _ => None,
        }
    }

    /// Get a human-readable description of this operation type.
    pub fn description(&self) -> &'static str {
        match self {
            Self::SemilatticeMax => "Maximum value wins (last-writer-wins for timestamps)",
            Self::SemilatticeMin => "Minimum value wins (first-writer-wins)",
            Self::SemilatticeUnion => "Set union (add-only collection)",
            Self::SemilatticeIntersect => "Set intersection (common elements only)",
            Self::AbelianAdd => "Additive delta (counters, accumulators)",
            Self::AbelianMultiply => "Multiplicative scaling",
            Self::GenericOverwrite => "Direct overwrite (may conflict)",
            Self::GenericConditional => "Conditional update (requires version match)",
            Self::Unknown => "Unknown operation type (conservative)",
        }
    }
}

impl Default for OpType {
    /// Default to Unknown for safety
    fn default() -> Self {
        Self::Unknown
    }
}

impl std::fmt::Display for OpType {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::SemilatticeMax => write!(f, "MAX"),
            Self::SemilatticeMin => write!(f, "MIN"),
            Self::SemilatticeUnion => write!(f, "UNION"),
            Self::SemilatticeIntersect => write!(f, "INTERSECT"),
            Self::AbelianAdd => write!(f, "ADD"),
            Self::AbelianMultiply => write!(f, "MULTIPLY"),
            Self::GenericOverwrite => write!(f, "OVERWRITE"),
            Self::GenericConditional => write!(f, "CONDITIONAL"),
            Self::Unknown => write!(f, "UNKNOWN"),
        }
    }
}

/// A value that can be algebraically merged.
///
/// This enum wraps various value types and provides type-safe
/// algebraic operations on them.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub enum AlgebraicValue {
    /// Signed 64-bit integer
    ///
    /// Use for: counters, integer deltas, discrete values
    Integer(i64),

    /// 64-bit floating point
    ///
    /// Use for: metrics, continuous values, timestamps as floats
    Float(f64),

    /// Set of strings
    ///
    /// Use for: tags, labels, string-based permissions
    StringSet(HashSet<String>),

    /// Set of integers
    ///
    /// Use for: ID collections, numeric flags
    IntSet(HashSet<i64>),

    /// Boolean value
    ///
    /// Use for: flags with OR (union) or AND (intersect) semantics
    Boolean(bool),

    /// Null/None value
    ///
    /// Represents absence of value
    Null,
}

impl AlgebraicValue {
    /// Create an integer value.
    #[inline]
    pub fn integer(v: i64) -> Self {
        Self::Integer(v)
    }

    /// Create a float value.
    #[inline]
    pub fn float(v: f64) -> Self {
        Self::Float(v)
    }

    /// Create a string set from an iterator.
    pub fn string_set<I, S>(iter: I) -> Self
    where
        I: IntoIterator<Item = S>,
        S: Into<String>,
    {
        Self::StringSet(iter.into_iter().map(|s| s.into()).collect())
    }

    /// Create an integer set from an iterator.
    pub fn int_set<I>(iter: I) -> Self
    where
        I: IntoIterator<Item = i64>,
    {
        Self::IntSet(iter.into_iter().collect())
    }

    /// Create a boolean value.
    #[inline]
    pub fn boolean(v: bool) -> Self {
        Self::Boolean(v)
    }

    /// Create a null value.
    #[inline]
    pub fn null() -> Self {
        Self::Null
    }

    /// Get the type name for error messages.
    pub fn type_name(&self) -> &'static str {
        match self {
            Self::Integer(_) => "Integer",
            Self::Float(_) => "Float",
            Self::StringSet(_) => "StringSet",
            Self::IntSet(_) => "IntSet",
            Self::Boolean(_) => "Boolean",
            Self::Null => "Null",
        }
    }

    /// Check if this is a numeric type (Integer or Float).
    #[inline]
    pub fn is_numeric(&self) -> bool {
        matches!(self, Self::Integer(_) | Self::Float(_))
    }

    /// Check if this is a set type (StringSet or IntSet).
    #[inline]
    pub fn is_set(&self) -> bool {
        matches!(self, Self::StringSet(_) | Self::IntSet(_))
    }

    /// Check if this is null.
    #[inline]
    pub fn is_null(&self) -> bool {
        matches!(self, Self::Null)
    }

    /// Try to get as i64.
    pub fn as_integer(&self) -> Option<i64> {
        match self {
            Self::Integer(v) => Some(*v),
            Self::Float(v) => Some(*v as i64),
            _ => None,
        }
    }

    /// Try to get as f64.
    pub fn as_float(&self) -> Option<f64> {
        match self {
            Self::Float(v) => Some(*v),
            Self::Integer(v) => Some(*v as f64),
            _ => None,
        }
    }
}

impl Default for AlgebraicValue {
    fn default() -> Self {
        Self::Null
    }
}

impl std::fmt::Display for AlgebraicValue {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Integer(v) => write!(f, "{}", v),
            Self::Float(v) => write!(f, "{}", v),
            Self::StringSet(s) => {
                let items: Vec<_> = s.iter().take(5).collect();
                if s.len() > 5 {
                    write!(f, "{{{:?}... ({} total)}}", items, s.len())
                } else {
                    write!(f, "{:?}", items)
                }
            }
            Self::IntSet(s) => {
                let items: Vec<_> = s.iter().take(5).collect();
                if s.len() > 5 {
                    write!(f, "{{{:?}... ({} total)}}", items, s.len())
                } else {
                    write!(f, "{:?}", items)
                }
            }
            Self::Boolean(v) => write!(f, "{}", v),
            Self::Null => write!(f, "null"),
        }
    }
}

// Convenience conversions
impl From<i64> for AlgebraicValue {
    fn from(v: i64) -> Self {
        Self::Integer(v)
    }
}

impl From<i32> for AlgebraicValue {
    fn from(v: i32) -> Self {
        Self::Integer(v as i64)
    }
}

impl From<f64> for AlgebraicValue {
    fn from(v: f64) -> Self {
        Self::Float(v)
    }
}

impl From<f32> for AlgebraicValue {
    fn from(v: f32) -> Self {
        Self::Float(v as f64)
    }
}

impl From<bool> for AlgebraicValue {
    fn from(v: bool) -> Self {
        Self::Boolean(v)
    }
}

impl From<HashSet<String>> for AlgebraicValue {
    fn from(v: HashSet<String>) -> Self {
        Self::StringSet(v)
    }
}

impl From<HashSet<i64>> for AlgebraicValue {
    fn from(v: HashSet<i64>) -> Self {
        Self::IntSet(v)
    }
}

impl<const N: usize> From<[&str; N]> for AlgebraicValue {
    fn from(arr: [&str; N]) -> Self {
        Self::StringSet(arr.iter().map(|s| s.to_string()).collect())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_optype_is_conflict_free() {
        // Semilattice operations are conflict-free
        assert!(OpType::SemilatticeMax.is_conflict_free());
        assert!(OpType::SemilatticeMin.is_conflict_free());
        assert!(OpType::SemilatticeUnion.is_conflict_free());
        assert!(OpType::SemilatticeIntersect.is_conflict_free());

        // Abelian operations are conflict-free
        assert!(OpType::AbelianAdd.is_conflict_free());
        assert!(OpType::AbelianMultiply.is_conflict_free());

        // Generic operations are NOT conflict-free
        assert!(!OpType::GenericOverwrite.is_conflict_free());
        assert!(!OpType::GenericConditional.is_conflict_free());
        assert!(!OpType::Unknown.is_conflict_free());
    }

    #[test]
    fn test_optype_is_semilattice() {
        assert!(OpType::SemilatticeMax.is_semilattice());
        assert!(OpType::SemilatticeMin.is_semilattice());
        assert!(OpType::SemilatticeUnion.is_semilattice());
        assert!(OpType::SemilatticeIntersect.is_semilattice());

        assert!(!OpType::AbelianAdd.is_semilattice());
        assert!(!OpType::GenericOverwrite.is_semilattice());
    }

    #[test]
    fn test_optype_is_abelian() {
        assert!(OpType::AbelianAdd.is_abelian());
        assert!(OpType::AbelianMultiply.is_abelian());

        assert!(!OpType::SemilatticeMax.is_abelian());
        assert!(!OpType::GenericOverwrite.is_abelian());
    }

    #[test]
    fn test_optype_can_merge_with() {
        // Same conflict-free types can merge
        assert!(OpType::AbelianAdd.can_merge_with(&OpType::AbelianAdd));
        assert!(OpType::SemilatticeMax.can_merge_with(&OpType::SemilatticeMax));

        // Different types cannot merge
        assert!(!OpType::AbelianAdd.can_merge_with(&OpType::AbelianMultiply));
        assert!(!OpType::SemilatticeMax.can_merge_with(&OpType::SemilatticeMin));

        // Non-conflict-free types cannot merge even with themselves
        assert!(!OpType::GenericOverwrite.can_merge_with(&OpType::GenericOverwrite));
        assert!(!OpType::Unknown.can_merge_with(&OpType::Unknown));
    }

    #[test]
    fn test_optype_display() {
        assert_eq!(OpType::SemilatticeMax.to_string(), "MAX");
        assert_eq!(OpType::AbelianAdd.to_string(), "ADD");
        assert_eq!(OpType::GenericOverwrite.to_string(), "OVERWRITE");
    }

    #[test]
    fn test_optype_default() {
        assert_eq!(OpType::default(), OpType::Unknown);
    }

    #[test]
    fn test_algebraic_value_integer() {
        let v = AlgebraicValue::integer(42);
        assert!(v.is_numeric());
        assert!(!v.is_set());
        assert_eq!(v.as_integer(), Some(42));
        assert_eq!(v.as_float(), Some(42.0));
        assert_eq!(v.type_name(), "Integer");
    }

    #[test]
    fn test_algebraic_value_float() {
        let v = AlgebraicValue::float(3.14);
        assert!(v.is_numeric());
        assert_eq!(v.as_float(), Some(3.14));
        assert_eq!(v.as_integer(), Some(3)); // truncates
        assert_eq!(v.type_name(), "Float");
    }

    #[test]
    fn test_algebraic_value_string_set() {
        let v = AlgebraicValue::string_set(["a", "b", "c"]);
        assert!(v.is_set());
        assert!(!v.is_numeric());
        assert_eq!(v.type_name(), "StringSet");

        if let AlgebraicValue::StringSet(s) = v {
            assert_eq!(s.len(), 3);
            assert!(s.contains("a"));
            assert!(s.contains("b"));
            assert!(s.contains("c"));
        } else {
            panic!("Expected StringSet");
        }
    }

    #[test]
    fn test_algebraic_value_int_set() {
        let v = AlgebraicValue::int_set([1, 2, 3]);
        assert!(v.is_set());
        assert_eq!(v.type_name(), "IntSet");

        if let AlgebraicValue::IntSet(s) = v {
            assert_eq!(s.len(), 3);
            assert!(s.contains(&1));
        } else {
            panic!("Expected IntSet");
        }
    }

    #[test]
    fn test_algebraic_value_null() {
        let v = AlgebraicValue::null();
        assert!(v.is_null());
        assert!(!v.is_numeric());
        assert!(!v.is_set());
        assert_eq!(v.type_name(), "Null");
    }

    #[test]
    fn test_algebraic_value_from_traits() {
        let v1: AlgebraicValue = 42i64.into();
        assert_eq!(v1, AlgebraicValue::Integer(42));

        let v2: AlgebraicValue = 42i32.into();
        assert_eq!(v2, AlgebraicValue::Integer(42));

        let v3: AlgebraicValue = 3.14f64.into();
        assert_eq!(v3, AlgebraicValue::Float(3.14));

        let v4: AlgebraicValue = true.into();
        assert_eq!(v4, AlgebraicValue::Boolean(true));

        let v5: AlgebraicValue = ["a", "b"].into();
        assert!(matches!(v5, AlgebraicValue::StringSet(_)));
    }

    #[test]
    fn test_algebraic_value_display() {
        assert_eq!(AlgebraicValue::integer(42).to_string(), "42");
        assert_eq!(AlgebraicValue::null().to_string(), "null");
        assert_eq!(AlgebraicValue::boolean(true).to_string(), "true");
    }

    #[test]
    fn test_algebraic_value_equality() {
        assert_eq!(AlgebraicValue::integer(42), AlgebraicValue::integer(42));
        assert_ne!(AlgebraicValue::integer(42), AlgebraicValue::integer(43));
        assert_ne!(AlgebraicValue::integer(42), AlgebraicValue::float(42.0));
    }

    #[test]
    fn test_algebraic_value_serialization() {
        let v = AlgebraicValue::integer(42);
        let json = serde_json::to_string(&v).unwrap();
        let parsed: AlgebraicValue = serde_json::from_str(&json).unwrap();
        assert_eq!(v, parsed);

        let set = AlgebraicValue::string_set(["x", "y"]);
        let json = serde_json::to_string(&set).unwrap();
        let parsed: AlgebraicValue = serde_json::from_str(&json).unwrap();
        assert_eq!(set, parsed);
    }
}
