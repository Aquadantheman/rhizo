//! Algebraic merge rules for conflict-free value combination.
//!
//! This module implements the mathematical merge operations for each
//! algebraic type. The key property is that all conflict-free operations
//! satisfy commutativity: `merge(a, b) = merge(b, a)`.
//!
//! # Semilattice Merges
//!
//! Semilattice operations are idempotent: `merge(a, a) = a`
//!
//! - MAX: Returns the larger value
//! - MIN: Returns the smaller value
//! - UNION: Returns the set union
//! - INTERSECT: Returns the set intersection
//!
//! # Abelian Merges
//!
//! Abelian operations combine values via group operation:
//!
//! - ADD: Returns `a + b`
//! - MULTIPLY: Returns `a * b`

use super::types::{AlgebraicValue, OpType};
use std::collections::HashSet;

/// Result of attempting an algebraic merge.
#[derive(Debug, Clone, PartialEq)]
pub enum MergeResult {
    /// Successfully merged to a single value.
    ///
    /// This indicates the merge was mathematically well-defined
    /// and produced a deterministic result.
    Merged(AlgebraicValue),

    /// Cannot merge - true conflict requiring manual resolution.
    ///
    /// This occurs when:
    /// - Operation type is not conflict-free (GenericOverwrite, Unknown)
    /// - Two different non-null values with non-algebraic operations
    Conflict {
        value1: AlgebraicValue,
        value2: AlgebraicValue,
        reason: String,
    },

    /// Type mismatch between values.
    ///
    /// This occurs when attempting to merge incompatible types,
    /// e.g., merging an Integer with a StringSet.
    TypeMismatch {
        type1: &'static str,
        type2: &'static str,
        operation: OpType,
    },
}

impl MergeResult {
    /// Check if merge was successful.
    #[inline]
    pub fn is_merged(&self) -> bool {
        matches!(self, Self::Merged(_))
    }

    /// Check if merge resulted in conflict.
    #[inline]
    pub fn is_conflict(&self) -> bool {
        matches!(self, Self::Conflict { .. })
    }

    /// Check if merge failed due to type mismatch.
    #[inline]
    pub fn is_type_mismatch(&self) -> bool {
        matches!(self, Self::TypeMismatch { .. })
    }

    /// Unwrap the merged value, panicking if not merged.
    #[inline]
    pub fn unwrap(self) -> AlgebraicValue {
        match self {
            Self::Merged(v) => v,
            Self::Conflict { reason, .. } => panic!("Called unwrap on Conflict: {}", reason),
            Self::TypeMismatch { type1, type2, operation } => {
                panic!("Called unwrap on TypeMismatch: {} vs {} for {:?}", type1, type2, operation)
            }
        }
    }

    /// Get the merged value if successful, None otherwise.
    #[inline]
    pub fn ok(self) -> Option<AlgebraicValue> {
        match self {
            Self::Merged(v) => Some(v),
            _ => None,
        }
    }
}

/// Algebraic merge engine.
///
/// This struct provides the core merge logic for all algebraic types.
/// All operations are stateless and deterministic.
///
/// # Mathematical Guarantees
///
/// For all conflict-free operation types:
/// - **Commutativity**: `merge(a, b) = merge(b, a)`
/// - **Associativity**: `merge(merge(a, b), c) = merge(a, merge(b, c))`
///
/// For semilattice operations additionally:
/// - **Idempotency**: `merge(a, a) = a`
pub struct AlgebraicMerger;

impl AlgebraicMerger {
    /// Attempt to merge two values using the specified algebraic operation.
    ///
    /// # Arguments
    /// * `op_type` - The algebraic operation type
    /// * `value1` - First value
    /// * `value2` - Second value
    ///
    /// # Returns
    /// - `MergeResult::Merged(v)` - Successfully merged
    /// - `MergeResult::Conflict{..}` - True conflict (non-algebraic operation)
    /// - `MergeResult::TypeMismatch{..}` - Incompatible value types
    ///
    /// # Example
    /// ```
    /// use rhizo_core::algebraic::{OpType, AlgebraicValue, AlgebraicMerger, MergeResult};
    ///
    /// // Counter merge: 5 + 3 = 8
    /// let result = AlgebraicMerger::merge(
    ///     OpType::AbelianAdd,
    ///     &AlgebraicValue::integer(5),
    ///     &AlgebraicValue::integer(3),
    /// );
    /// assert_eq!(result, MergeResult::Merged(AlgebraicValue::integer(8)));
    ///
    /// // Max merge: max(10, 20) = 20
    /// let result = AlgebraicMerger::merge(
    ///     OpType::SemilatticeMax,
    ///     &AlgebraicValue::integer(10),
    ///     &AlgebraicValue::integer(20),
    /// );
    /// assert_eq!(result, MergeResult::Merged(AlgebraicValue::integer(20)));
    /// ```
    pub fn merge(
        op_type: OpType,
        value1: &AlgebraicValue,
        value2: &AlgebraicValue,
    ) -> MergeResult {
        // Handle null values
        if value1.is_null() {
            return MergeResult::Merged(value2.clone());
        }
        if value2.is_null() {
            return MergeResult::Merged(value1.clone());
        }

        // Check if operation is conflict-free
        if !op_type.is_conflict_free() {
            return MergeResult::Conflict {
                value1: value1.clone(),
                value2: value2.clone(),
                reason: format!(
                    "Operation type {} is not conflict-free",
                    op_type
                ),
            };
        }

        match op_type {
            OpType::SemilatticeMax => Self::merge_max(value1, value2),
            OpType::SemilatticeMin => Self::merge_min(value1, value2),
            OpType::SemilatticeUnion => Self::merge_union(value1, value2),
            OpType::SemilatticeIntersect => Self::merge_intersect(value1, value2),
            OpType::AbelianAdd => Self::merge_add(value1, value2),
            OpType::AbelianMultiply => Self::merge_multiply(value1, value2),
            _ => MergeResult::Conflict {
                value1: value1.clone(),
                value2: value2.clone(),
                reason: format!("Unexpected operation type: {}", op_type),
            },
        }
    }

    /// Merge using MAX (semilattice join with ordering).
    ///
    /// Mathematical property: max(a, b) = max(b, a) and max(a, a) = a
    fn merge_max(v1: &AlgebraicValue, v2: &AlgebraicValue) -> MergeResult {
        match (v1, v2) {
            (AlgebraicValue::Integer(a), AlgebraicValue::Integer(b)) => {
                MergeResult::Merged(AlgebraicValue::Integer(*a.max(b)))
            }
            (AlgebraicValue::Float(a), AlgebraicValue::Float(b)) => {
                MergeResult::Merged(AlgebraicValue::Float(a.max(*b)))
            }
            // Cross-type numeric comparison (promote to float)
            (AlgebraicValue::Integer(a), AlgebraicValue::Float(b)) => {
                let af = *a as f64;
                MergeResult::Merged(AlgebraicValue::Float(af.max(*b)))
            }
            (AlgebraicValue::Float(a), AlgebraicValue::Integer(b)) => {
                let bf = *b as f64;
                MergeResult::Merged(AlgebraicValue::Float(a.max(bf)))
            }
            (AlgebraicValue::Boolean(a), AlgebraicValue::Boolean(b)) => {
                // true > false in boolean ordering
                MergeResult::Merged(AlgebraicValue::Boolean(*a || *b))
            }
            _ => MergeResult::TypeMismatch {
                type1: v1.type_name(),
                type2: v2.type_name(),
                operation: OpType::SemilatticeMax,
            },
        }
    }

    /// Merge using MIN (semilattice meet with ordering).
    ///
    /// Mathematical property: min(a, b) = min(b, a) and min(a, a) = a
    fn merge_min(v1: &AlgebraicValue, v2: &AlgebraicValue) -> MergeResult {
        match (v1, v2) {
            (AlgebraicValue::Integer(a), AlgebraicValue::Integer(b)) => {
                MergeResult::Merged(AlgebraicValue::Integer(*a.min(b)))
            }
            (AlgebraicValue::Float(a), AlgebraicValue::Float(b)) => {
                MergeResult::Merged(AlgebraicValue::Float(a.min(*b)))
            }
            // Cross-type numeric comparison (promote to float)
            (AlgebraicValue::Integer(a), AlgebraicValue::Float(b)) => {
                let af = *a as f64;
                MergeResult::Merged(AlgebraicValue::Float(af.min(*b)))
            }
            (AlgebraicValue::Float(a), AlgebraicValue::Integer(b)) => {
                let bf = *b as f64;
                MergeResult::Merged(AlgebraicValue::Float(a.min(bf)))
            }
            (AlgebraicValue::Boolean(a), AlgebraicValue::Boolean(b)) => {
                // false < true in boolean ordering
                MergeResult::Merged(AlgebraicValue::Boolean(*a && *b))
            }
            _ => MergeResult::TypeMismatch {
                type1: v1.type_name(),
                type2: v2.type_name(),
                operation: OpType::SemilatticeMin,
            },
        }
    }

    /// Merge using UNION (semilattice join for sets).
    ///
    /// Mathematical property: A ∪ B = B ∪ A and A ∪ A = A
    fn merge_union(v1: &AlgebraicValue, v2: &AlgebraicValue) -> MergeResult {
        match (v1, v2) {
            (AlgebraicValue::StringSet(a), AlgebraicValue::StringSet(b)) => {
                let union: HashSet<String> = a.union(b).cloned().collect();
                MergeResult::Merged(AlgebraicValue::StringSet(union))
            }
            (AlgebraicValue::IntSet(a), AlgebraicValue::IntSet(b)) => {
                let union: HashSet<i64> = a.union(b).copied().collect();
                MergeResult::Merged(AlgebraicValue::IntSet(union))
            }
            (AlgebraicValue::Boolean(a), AlgebraicValue::Boolean(b)) => {
                // Boolean union = OR
                MergeResult::Merged(AlgebraicValue::Boolean(*a || *b))
            }
            _ => MergeResult::TypeMismatch {
                type1: v1.type_name(),
                type2: v2.type_name(),
                operation: OpType::SemilatticeUnion,
            },
        }
    }

    /// Merge using INTERSECT (semilattice meet for sets).
    ///
    /// Mathematical property: A ∩ B = B ∩ A and A ∩ A = A
    fn merge_intersect(v1: &AlgebraicValue, v2: &AlgebraicValue) -> MergeResult {
        match (v1, v2) {
            (AlgebraicValue::StringSet(a), AlgebraicValue::StringSet(b)) => {
                let intersect: HashSet<String> = a.intersection(b).cloned().collect();
                MergeResult::Merged(AlgebraicValue::StringSet(intersect))
            }
            (AlgebraicValue::IntSet(a), AlgebraicValue::IntSet(b)) => {
                let intersect: HashSet<i64> = a.intersection(b).copied().collect();
                MergeResult::Merged(AlgebraicValue::IntSet(intersect))
            }
            (AlgebraicValue::Boolean(a), AlgebraicValue::Boolean(b)) => {
                // Boolean intersection = AND
                MergeResult::Merged(AlgebraicValue::Boolean(*a && *b))
            }
            _ => MergeResult::TypeMismatch {
                type1: v1.type_name(),
                type2: v2.type_name(),
                operation: OpType::SemilatticeIntersect,
            },
        }
    }

    /// Merge using ADD (Abelian group addition).
    ///
    /// Mathematical property: a + b = b + a
    fn merge_add(v1: &AlgebraicValue, v2: &AlgebraicValue) -> MergeResult {
        match (v1, v2) {
            (AlgebraicValue::Integer(a), AlgebraicValue::Integer(b)) => {
                // Use checked_add for overflow safety
                match a.checked_add(*b) {
                    Some(sum) => MergeResult::Merged(AlgebraicValue::Integer(sum)),
                    None => MergeResult::Conflict {
                        value1: v1.clone(),
                        value2: v2.clone(),
                        reason: format!("Integer overflow: {} + {}", a, b),
                    },
                }
            }
            (AlgebraicValue::Float(a), AlgebraicValue::Float(b)) => {
                MergeResult::Merged(AlgebraicValue::Float(a + b))
            }
            // Cross-type addition (promote to float)
            (AlgebraicValue::Integer(a), AlgebraicValue::Float(b)) => {
                MergeResult::Merged(AlgebraicValue::Float(*a as f64 + b))
            }
            (AlgebraicValue::Float(a), AlgebraicValue::Integer(b)) => {
                MergeResult::Merged(AlgebraicValue::Float(a + *b as f64))
            }
            _ => MergeResult::TypeMismatch {
                type1: v1.type_name(),
                type2: v2.type_name(),
                operation: OpType::AbelianAdd,
            },
        }
    }

    /// Merge using MULTIPLY (Abelian group multiplication).
    ///
    /// Mathematical property: a * b = b * a
    fn merge_multiply(v1: &AlgebraicValue, v2: &AlgebraicValue) -> MergeResult {
        match (v1, v2) {
            (AlgebraicValue::Integer(a), AlgebraicValue::Integer(b)) => {
                // Use checked_mul for overflow safety
                match a.checked_mul(*b) {
                    Some(product) => MergeResult::Merged(AlgebraicValue::Integer(product)),
                    None => MergeResult::Conflict {
                        value1: v1.clone(),
                        value2: v2.clone(),
                        reason: format!("Integer overflow: {} * {}", a, b),
                    },
                }
            }
            (AlgebraicValue::Float(a), AlgebraicValue::Float(b)) => {
                MergeResult::Merged(AlgebraicValue::Float(a * b))
            }
            // Cross-type multiplication (promote to float)
            (AlgebraicValue::Integer(a), AlgebraicValue::Float(b)) => {
                MergeResult::Merged(AlgebraicValue::Float(*a as f64 * b))
            }
            (AlgebraicValue::Float(a), AlgebraicValue::Integer(b)) => {
                MergeResult::Merged(AlgebraicValue::Float(a * *b as f64))
            }
            _ => MergeResult::TypeMismatch {
                type1: v1.type_name(),
                type2: v2.type_name(),
                operation: OpType::AbelianMultiply,
            },
        }
    }

    /// Verify commutativity property: merge(a, b) = merge(b, a)
    ///
    /// This is a test helper to verify the mathematical guarantees.
    #[cfg(test)]
    pub fn verify_commutativity(
        op_type: OpType,
        v1: &AlgebraicValue,
        v2: &AlgebraicValue,
    ) -> bool {
        let result1 = Self::merge(op_type, v1, v2);
        let result2 = Self::merge(op_type, v2, v1);
        result1 == result2
    }

    /// Verify idempotency property: merge(a, a) = a (for semilattice operations)
    ///
    /// This is a test helper to verify the mathematical guarantees.
    #[cfg(test)]
    pub fn verify_idempotency(op_type: OpType, v: &AlgebraicValue) -> bool {
        if !op_type.is_semilattice() {
            return true; // Only semilattice operations need to be idempotent
        }
        let result = Self::merge(op_type, v, v);
        match result {
            MergeResult::Merged(merged) => merged == *v,
            _ => false,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    // ============ MAX Tests ============

    #[test]
    fn test_merge_max_integers() {
        let result = AlgebraicMerger::merge(
            OpType::SemilatticeMax,
            &AlgebraicValue::integer(10),
            &AlgebraicValue::integer(20),
        );
        assert_eq!(result, MergeResult::Merged(AlgebraicValue::integer(20)));

        // Verify commutativity
        let result2 = AlgebraicMerger::merge(
            OpType::SemilatticeMax,
            &AlgebraicValue::integer(20),
            &AlgebraicValue::integer(10),
        );
        assert_eq!(result, result2);
    }

    #[test]
    fn test_merge_max_floats() {
        let result = AlgebraicMerger::merge(
            OpType::SemilatticeMax,
            &AlgebraicValue::float(3.5),
            &AlgebraicValue::float(2.5),
        );
        assert_eq!(result, MergeResult::Merged(AlgebraicValue::float(3.5)));
    }

    #[test]
    fn test_merge_max_negative() {
        let result = AlgebraicMerger::merge(
            OpType::SemilatticeMax,
            &AlgebraicValue::integer(-10),
            &AlgebraicValue::integer(-5),
        );
        assert_eq!(result, MergeResult::Merged(AlgebraicValue::integer(-5)));
    }

    #[test]
    fn test_merge_max_idempotent() {
        let v = AlgebraicValue::integer(42);
        let result = AlgebraicMerger::merge(OpType::SemilatticeMax, &v, &v);
        assert_eq!(result, MergeResult::Merged(AlgebraicValue::integer(42)));
    }

    // ============ MIN Tests ============

    #[test]
    fn test_merge_min_integers() {
        let result = AlgebraicMerger::merge(
            OpType::SemilatticeMin,
            &AlgebraicValue::integer(10),
            &AlgebraicValue::integer(20),
        );
        assert_eq!(result, MergeResult::Merged(AlgebraicValue::integer(10)));
    }

    #[test]
    fn test_merge_min_floats() {
        let result = AlgebraicMerger::merge(
            OpType::SemilatticeMin,
            &AlgebraicValue::float(3.5),
            &AlgebraicValue::float(2.5),
        );
        assert_eq!(result, MergeResult::Merged(AlgebraicValue::float(2.5)));
    }

    // ============ UNION Tests ============

    #[test]
    fn test_merge_union_string_sets() {
        let v1 = AlgebraicValue::string_set(["a", "b"]);
        let v2 = AlgebraicValue::string_set(["b", "c"]);
        let result = AlgebraicMerger::merge(OpType::SemilatticeUnion, &v1, &v2);

        if let MergeResult::Merged(AlgebraicValue::StringSet(s)) = result {
            assert_eq!(s.len(), 3);
            assert!(s.contains("a"));
            assert!(s.contains("b"));
            assert!(s.contains("c"));
        } else {
            panic!("Expected Merged StringSet");
        }
    }

    #[test]
    fn test_merge_union_int_sets() {
        let v1 = AlgebraicValue::int_set([1, 2]);
        let v2 = AlgebraicValue::int_set([2, 3]);
        let result = AlgebraicMerger::merge(OpType::SemilatticeUnion, &v1, &v2);

        if let MergeResult::Merged(AlgebraicValue::IntSet(s)) = result {
            assert_eq!(s.len(), 3);
            assert!(s.contains(&1));
            assert!(s.contains(&2));
            assert!(s.contains(&3));
        } else {
            panic!("Expected Merged IntSet");
        }
    }

    #[test]
    fn test_merge_union_idempotent() {
        let v = AlgebraicValue::string_set(["x", "y"]);
        let result = AlgebraicMerger::merge(OpType::SemilatticeUnion, &v, &v);

        if let MergeResult::Merged(AlgebraicValue::StringSet(s)) = result {
            assert_eq!(s.len(), 2);
        } else {
            panic!("Expected Merged StringSet");
        }
    }

    // ============ INTERSECT Tests ============

    #[test]
    fn test_merge_intersect_string_sets() {
        let v1 = AlgebraicValue::string_set(["a", "b", "c"]);
        let v2 = AlgebraicValue::string_set(["b", "c", "d"]);
        let result = AlgebraicMerger::merge(OpType::SemilatticeIntersect, &v1, &v2);

        if let MergeResult::Merged(AlgebraicValue::StringSet(s)) = result {
            assert_eq!(s.len(), 2);
            assert!(s.contains("b"));
            assert!(s.contains("c"));
        } else {
            panic!("Expected Merged StringSet");
        }
    }

    #[test]
    fn test_merge_intersect_empty_result() {
        let v1 = AlgebraicValue::string_set(["a", "b"]);
        let v2 = AlgebraicValue::string_set(["c", "d"]);
        let result = AlgebraicMerger::merge(OpType::SemilatticeIntersect, &v1, &v2);

        if let MergeResult::Merged(AlgebraicValue::StringSet(s)) = result {
            assert!(s.is_empty());
        } else {
            panic!("Expected Merged StringSet");
        }
    }

    // ============ ADD Tests ============

    #[test]
    fn test_merge_add_integers() {
        let result = AlgebraicMerger::merge(
            OpType::AbelianAdd,
            &AlgebraicValue::integer(5),
            &AlgebraicValue::integer(3),
        );
        assert_eq!(result, MergeResult::Merged(AlgebraicValue::integer(8)));
    }

    #[test]
    fn test_merge_add_negative() {
        let result = AlgebraicMerger::merge(
            OpType::AbelianAdd,
            &AlgebraicValue::integer(10),
            &AlgebraicValue::integer(-3),
        );
        assert_eq!(result, MergeResult::Merged(AlgebraicValue::integer(7)));
    }

    #[test]
    fn test_merge_add_floats() {
        let result = AlgebraicMerger::merge(
            OpType::AbelianAdd,
            &AlgebraicValue::float(1.5),
            &AlgebraicValue::float(2.5),
        );
        assert_eq!(result, MergeResult::Merged(AlgebraicValue::float(4.0)));
    }

    #[test]
    fn test_merge_add_commutativity() {
        let v1 = AlgebraicValue::integer(17);
        let v2 = AlgebraicValue::integer(23);
        assert!(AlgebraicMerger::verify_commutativity(OpType::AbelianAdd, &v1, &v2));
    }

    #[test]
    fn test_merge_add_overflow() {
        let result = AlgebraicMerger::merge(
            OpType::AbelianAdd,
            &AlgebraicValue::integer(i64::MAX),
            &AlgebraicValue::integer(1),
        );
        assert!(result.is_conflict());
    }

    // ============ MULTIPLY Tests ============

    #[test]
    fn test_merge_multiply_integers() {
        let result = AlgebraicMerger::merge(
            OpType::AbelianMultiply,
            &AlgebraicValue::integer(4),
            &AlgebraicValue::integer(5),
        );
        assert_eq!(result, MergeResult::Merged(AlgebraicValue::integer(20)));
    }

    #[test]
    fn test_merge_multiply_floats() {
        let result = AlgebraicMerger::merge(
            OpType::AbelianMultiply,
            &AlgebraicValue::float(2.0),
            &AlgebraicValue::float(3.0),
        );
        assert_eq!(result, MergeResult::Merged(AlgebraicValue::float(6.0)));
    }

    #[test]
    fn test_merge_multiply_by_zero() {
        let result = AlgebraicMerger::merge(
            OpType::AbelianMultiply,
            &AlgebraicValue::integer(100),
            &AlgebraicValue::integer(0),
        );
        assert_eq!(result, MergeResult::Merged(AlgebraicValue::integer(0)));
    }

    #[test]
    fn test_merge_multiply_overflow() {
        let result = AlgebraicMerger::merge(
            OpType::AbelianMultiply,
            &AlgebraicValue::integer(i64::MAX),
            &AlgebraicValue::integer(2),
        );
        assert!(result.is_conflict());
    }

    // ============ Null Handling Tests ============

    #[test]
    fn test_merge_with_null() {
        let v = AlgebraicValue::integer(42);
        let null = AlgebraicValue::null();

        let result1 = AlgebraicMerger::merge(OpType::AbelianAdd, &v, &null);
        assert_eq!(result1, MergeResult::Merged(AlgebraicValue::integer(42)));

        let result2 = AlgebraicMerger::merge(OpType::AbelianAdd, &null, &v);
        assert_eq!(result2, MergeResult::Merged(AlgebraicValue::integer(42)));

        let result3 = AlgebraicMerger::merge(OpType::AbelianAdd, &null, &null);
        assert!(result3.is_merged());
    }

    // ============ Conflict Tests ============

    #[test]
    fn test_merge_generic_overwrite_conflicts() {
        let result = AlgebraicMerger::merge(
            OpType::GenericOverwrite,
            &AlgebraicValue::integer(1),
            &AlgebraicValue::integer(2),
        );
        assert!(result.is_conflict());
    }

    #[test]
    fn test_merge_unknown_conflicts() {
        let result = AlgebraicMerger::merge(
            OpType::Unknown,
            &AlgebraicValue::integer(1),
            &AlgebraicValue::integer(2),
        );
        assert!(result.is_conflict());
    }

    // ============ Type Mismatch Tests ============

    #[test]
    fn test_merge_type_mismatch() {
        let result = AlgebraicMerger::merge(
            OpType::AbelianAdd,
            &AlgebraicValue::integer(1),
            &AlgebraicValue::string_set(["a"]),
        );
        assert!(result.is_type_mismatch());
    }

    // ============ Cross-Type Numeric Tests ============

    #[test]
    fn test_merge_add_int_float() {
        let result = AlgebraicMerger::merge(
            OpType::AbelianAdd,
            &AlgebraicValue::integer(5),
            &AlgebraicValue::float(2.5),
        );
        assert_eq!(result, MergeResult::Merged(AlgebraicValue::float(7.5)));
    }

    #[test]
    fn test_merge_max_int_float() {
        let result = AlgebraicMerger::merge(
            OpType::SemilatticeMax,
            &AlgebraicValue::integer(5),
            &AlgebraicValue::float(10.5),
        );
        assert_eq!(result, MergeResult::Merged(AlgebraicValue::float(10.5)));
    }

    // ============ Boolean Tests ============

    #[test]
    fn test_merge_bool_union() {
        let result = AlgebraicMerger::merge(
            OpType::SemilatticeUnion,
            &AlgebraicValue::boolean(false),
            &AlgebraicValue::boolean(true),
        );
        assert_eq!(result, MergeResult::Merged(AlgebraicValue::boolean(true)));
    }

    #[test]
    fn test_merge_bool_intersect() {
        let result = AlgebraicMerger::merge(
            OpType::SemilatticeIntersect,
            &AlgebraicValue::boolean(true),
            &AlgebraicValue::boolean(false),
        );
        assert_eq!(result, MergeResult::Merged(AlgebraicValue::boolean(false)));
    }

    // ============ Property Tests ============

    #[test]
    fn test_all_semilattice_idempotent() {
        // Max and Min are idempotent for integers
        for op in [OpType::SemilatticeMax, OpType::SemilatticeMin] {
            assert!(
                AlgebraicMerger::verify_idempotency(op, &AlgebraicValue::integer(42)),
                "{:?} should be idempotent for integers",
                op
            );
        }

        // Union and Intersect are idempotent for sets
        for op in [OpType::SemilatticeUnion, OpType::SemilatticeIntersect] {
            assert!(
                AlgebraicMerger::verify_idempotency(
                    op,
                    &AlgebraicValue::string_set(["a", "b"])
                ),
                "{:?} should be idempotent for sets",
                op
            );
        }
    }

    #[test]
    fn test_all_conflict_free_commutative() {
        let ops = [
            OpType::SemilatticeMax,
            OpType::SemilatticeMin,
            OpType::AbelianAdd,
            OpType::AbelianMultiply,
        ];

        for op in ops {
            assert!(
                AlgebraicMerger::verify_commutativity(
                    op,
                    &AlgebraicValue::integer(17),
                    &AlgebraicValue::integer(23),
                ),
                "{:?} should be commutative",
                op
            );
        }
    }

    // ============ MergeResult Tests ============

    #[test]
    fn test_merge_result_unwrap() {
        let result = MergeResult::Merged(AlgebraicValue::integer(42));
        assert_eq!(result.unwrap(), AlgebraicValue::integer(42));
    }

    #[test]
    #[should_panic(expected = "Called unwrap on Conflict")]
    fn test_merge_result_unwrap_conflict_panics() {
        let result = MergeResult::Conflict {
            value1: AlgebraicValue::integer(1),
            value2: AlgebraicValue::integer(2),
            reason: "test".to_string(),
        };
        result.unwrap();
    }

    #[test]
    fn test_merge_result_ok() {
        let merged = MergeResult::Merged(AlgebraicValue::integer(42));
        assert_eq!(merged.ok(), Some(AlgebraicValue::integer(42)));

        let conflict = MergeResult::Conflict {
            value1: AlgebraicValue::integer(1),
            value2: AlgebraicValue::integer(2),
            reason: "test".to_string(),
        };
        assert_eq!(conflict.ok(), None);
    }
}
