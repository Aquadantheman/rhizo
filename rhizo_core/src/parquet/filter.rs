//! Predicate filter types for Parquet predicate pushdown.
//!
//! This module provides types for expressing filter predicates that can be
//! pushed down to the Parquet reader for optimized query execution.
//!
//! # Two-Level Filtering
//!
//! Predicate pushdown operates at two levels:
//!
//! 1. **Row Group Pruning**: Use min/max statistics to skip entire row groups
//!    where no rows can possibly match the predicate.
//!
//! 2. **Row-Level Filtering**: For row groups that might contain matches,
//!    decode only the columns needed for the predicate, filter rows, then
//!    decode remaining columns only for matching rows.
//!
//! # Mathematical Model
//!
//! For a query with selectivity `s` (fraction of rows matching):
//!
//! ```text
//! Speedup ≈ G/g × 1/s
//! where G = total row groups, g = row groups that might match
//!
//! For 1% selectivity with good row group distribution:
//!   Speedup can reach 50-100x
//! ```

use std::fmt;

/// Comparison operations for filter predicates.
///
/// These map directly to Arrow's compute kernels for efficient evaluation.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FilterOp {
    /// Equal: `column = value`
    Eq,
    /// Not equal: `column != value`
    Ne,
    /// Less than: `column < value`
    Lt,
    /// Less than or equal: `column <= value`
    Le,
    /// Greater than: `column > value`
    Gt,
    /// Greater than or equal: `column >= value`
    Ge,
}

impl fmt::Display for FilterOp {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            FilterOp::Eq => write!(f, "="),
            FilterOp::Ne => write!(f, "!="),
            FilterOp::Lt => write!(f, "<"),
            FilterOp::Le => write!(f, "<="),
            FilterOp::Gt => write!(f, ">"),
            FilterOp::Ge => write!(f, ">="),
        }
    }
}

impl FilterOp {
    /// Returns the inverse operation (for row group pruning logic).
    ///
    /// For row group pruning with statistics:
    /// - `column > value` can skip groups where `max(column) <= value`
    /// - `column < value` can skip groups where `min(column) >= value`
    pub fn can_prune_with_max(&self) -> bool {
        matches!(self, FilterOp::Gt | FilterOp::Ge | FilterOp::Eq)
    }

    pub fn can_prune_with_min(&self) -> bool {
        matches!(self, FilterOp::Lt | FilterOp::Le | FilterOp::Eq)
    }
}

/// Scalar values for predicate comparison.
///
/// Supports the common types used in analytical queries.
#[derive(Debug, Clone, PartialEq)]
pub enum ScalarValue {
    /// 64-bit signed integer
    Int64(i64),
    /// 64-bit floating point
    Float64(f64),
    /// UTF-8 string
    Utf8(String),
    /// Boolean
    Boolean(bool),
    /// 32-bit signed integer
    Int32(i32),
    /// Null value (for IS NULL / IS NOT NULL predicates)
    Null,
}

impl fmt::Display for ScalarValue {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            ScalarValue::Int64(v) => write!(f, "{}", v),
            ScalarValue::Float64(v) => write!(f, "{}", v),
            ScalarValue::Utf8(v) => write!(f, "'{}'", v),
            ScalarValue::Boolean(v) => write!(f, "{}", v),
            ScalarValue::Int32(v) => write!(f, "{}", v),
            ScalarValue::Null => write!(f, "NULL"),
        }
    }
}

impl ScalarValue {
    /// Compare this scalar with another for ordering.
    /// Returns None if types don't match or comparison is not meaningful.
    pub fn compare(&self, other: &ScalarValue, op: FilterOp) -> Option<bool> {
        match (self, other) {
            (ScalarValue::Int64(a), ScalarValue::Int64(b)) => Some(compare_ord(*a, *b, op)),
            (ScalarValue::Int32(a), ScalarValue::Int32(b)) => Some(compare_ord(*a, *b, op)),
            (ScalarValue::Float64(a), ScalarValue::Float64(b)) => {
                // Handle NaN properly
                if a.is_nan() || b.is_nan() {
                    match op {
                        FilterOp::Ne => Some(true),
                        _ => Some(false),
                    }
                } else {
                    Some(compare_ord(*a, *b, op))
                }
            }
            (ScalarValue::Utf8(a), ScalarValue::Utf8(b)) => Some(compare_ord(a.as_str(), b.as_str(), op)),
            (ScalarValue::Boolean(a), ScalarValue::Boolean(b)) => {
                match op {
                    FilterOp::Eq => Some(a == b),
                    FilterOp::Ne => Some(a != b),
                    _ => None, // < > <= >= don't make sense for booleans
                }
            }
            _ => None, // Type mismatch
        }
    }
}

/// Helper function to compare ordered values.
fn compare_ord<T: PartialOrd>(a: T, b: T, op: FilterOp) -> bool {
    match op {
        FilterOp::Eq => a == b,
        FilterOp::Ne => a != b,
        FilterOp::Lt => a < b,
        FilterOp::Le => a <= b,
        FilterOp::Gt => a > b,
        FilterOp::Ge => a >= b,
    }
}

/// A predicate filter for Parquet data.
///
/// Represents a simple comparison: `column <op> value`
///
/// # Example
///
/// ```ignore
/// use rhizo_core::parquet::{PredicateFilter, FilterOp, ScalarValue};
///
/// // Create filter: age > 50
/// let filter = PredicateFilter::new("age", FilterOp::Gt, ScalarValue::Int64(50));
///
/// // Create filter: status = 'active'
/// let filter = PredicateFilter::new("status", FilterOp::Eq, ScalarValue::Utf8("active".to_string()));
/// ```
#[derive(Debug, Clone)]
pub struct PredicateFilter {
    /// Column name to filter on
    pub column: String,
    /// Comparison operation
    pub op: FilterOp,
    /// Value to compare against
    pub value: ScalarValue,
}

impl PredicateFilter {
    /// Create a new predicate filter.
    pub fn new(column: impl Into<String>, op: FilterOp, value: ScalarValue) -> Self {
        Self {
            column: column.into(),
            op,
            value,
        }
    }

    /// Check if a row group can be pruned based on its statistics.
    ///
    /// Returns `true` if the row group can definitely be skipped
    /// (no rows can possibly match the predicate).
    ///
    /// # Arguments
    /// * `min` - Minimum value in the row group (None if unknown)
    /// * `max` - Maximum value in the row group (None if unknown)
    pub fn can_prune_row_group(&self, min: Option<&ScalarValue>, max: Option<&ScalarValue>) -> bool {
        match self.op {
            // column > value: skip if max <= value
            FilterOp::Gt => {
                if let Some(max_val) = max {
                    max_val.compare(&self.value, FilterOp::Le).unwrap_or(false)
                } else {
                    false
                }
            }
            // column >= value: skip if max < value
            FilterOp::Ge => {
                if let Some(max_val) = max {
                    max_val.compare(&self.value, FilterOp::Lt).unwrap_or(false)
                } else {
                    false
                }
            }
            // column < value: skip if min >= value
            FilterOp::Lt => {
                if let Some(min_val) = min {
                    min_val.compare(&self.value, FilterOp::Ge).unwrap_or(false)
                } else {
                    false
                }
            }
            // column <= value: skip if min > value
            FilterOp::Le => {
                if let Some(min_val) = min {
                    min_val.compare(&self.value, FilterOp::Gt).unwrap_or(false)
                } else {
                    false
                }
            }
            // column = value: skip if value < min OR value > max
            FilterOp::Eq => {
                let below_min = min
                    .and_then(|m| self.value.compare(m, FilterOp::Lt))
                    .unwrap_or(false);
                let above_max = max
                    .and_then(|m| self.value.compare(m, FilterOp::Gt))
                    .unwrap_or(false);
                below_min || above_max
            }
            // column != value: can only skip if min = max = value (entire group is that value)
            FilterOp::Ne => {
                if let (Some(min_val), Some(max_val)) = (min, max) {
                    let min_eq = min_val.compare(&self.value, FilterOp::Eq).unwrap_or(false);
                    let max_eq = max_val.compare(&self.value, FilterOp::Eq).unwrap_or(false);
                    min_eq && max_eq
                } else {
                    false
                }
            }
        }
    }
}

impl fmt::Display for PredicateFilter {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{} {} {}", self.column, self.op, self.value)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_filter_op_display() {
        assert_eq!(format!("{}", FilterOp::Eq), "=");
        assert_eq!(format!("{}", FilterOp::Gt), ">");
        assert_eq!(format!("{}", FilterOp::Le), "<=");
    }

    #[test]
    fn test_scalar_value_display() {
        assert_eq!(format!("{}", ScalarValue::Int64(42)), "42");
        assert_eq!(format!("{}", ScalarValue::Float64(3.5)), "3.5");
        assert_eq!(format!("{}", ScalarValue::Utf8("hello".to_string())), "'hello'");
        assert_eq!(format!("{}", ScalarValue::Boolean(true)), "true");
    }

    #[test]
    fn test_scalar_compare_int64() {
        let a = ScalarValue::Int64(10);
        let b = ScalarValue::Int64(20);

        assert_eq!(a.compare(&b, FilterOp::Lt), Some(true));
        assert_eq!(a.compare(&b, FilterOp::Gt), Some(false));
        assert_eq!(a.compare(&b, FilterOp::Eq), Some(false));
        assert_eq!(a.compare(&a, FilterOp::Eq), Some(true));
    }

    #[test]
    fn test_scalar_compare_float64() {
        let a = ScalarValue::Float64(1.5);
        let b = ScalarValue::Float64(2.5);

        assert_eq!(a.compare(&b, FilterOp::Lt), Some(true));
        assert_eq!(a.compare(&b, FilterOp::Ge), Some(false));
    }

    #[test]
    fn test_scalar_compare_type_mismatch() {
        let a = ScalarValue::Int64(10);
        let b = ScalarValue::Float64(10.0);

        assert_eq!(a.compare(&b, FilterOp::Eq), None);
    }

    #[test]
    fn test_predicate_filter_display() {
        let filter = PredicateFilter::new("age", FilterOp::Gt, ScalarValue::Int64(50));
        assert_eq!(format!("{}", filter), "age > 50");
    }

    #[test]
    fn test_row_group_pruning_gt() {
        let filter = PredicateFilter::new("value", FilterOp::Gt, ScalarValue::Float64(0.95));

        // Row group with max=0.5: can be pruned (all values <= 0.5 < 0.95)
        let can_prune = filter.can_prune_row_group(
            Some(&ScalarValue::Float64(0.1)),
            Some(&ScalarValue::Float64(0.5)),
        );
        assert!(can_prune, "Should prune when max < threshold");

        // Row group with max=1.0: cannot be pruned (might have values > 0.95)
        let can_prune = filter.can_prune_row_group(
            Some(&ScalarValue::Float64(0.8)),
            Some(&ScalarValue::Float64(1.0)),
        );
        assert!(!can_prune, "Should not prune when max > threshold");
    }

    #[test]
    fn test_row_group_pruning_lt() {
        let filter = PredicateFilter::new("value", FilterOp::Lt, ScalarValue::Int64(10));

        // Row group with min=20: can be pruned (all values >= 20 > 10)
        let can_prune = filter.can_prune_row_group(
            Some(&ScalarValue::Int64(20)),
            Some(&ScalarValue::Int64(30)),
        );
        assert!(can_prune, "Should prune when min > threshold");

        // Row group with min=5: cannot be pruned (might have values < 10)
        let can_prune = filter.can_prune_row_group(
            Some(&ScalarValue::Int64(5)),
            Some(&ScalarValue::Int64(30)),
        );
        assert!(!can_prune, "Should not prune when min < threshold");
    }

    #[test]
    fn test_row_group_pruning_eq() {
        let filter = PredicateFilter::new("category", FilterOp::Eq, ScalarValue::Int64(5));

        // Row group with range [10, 20]: can be pruned (5 not in range)
        let can_prune = filter.can_prune_row_group(
            Some(&ScalarValue::Int64(10)),
            Some(&ScalarValue::Int64(20)),
        );
        assert!(can_prune, "Should prune when value not in [min, max]");

        // Row group with range [1, 10]: cannot be pruned (5 in range)
        let can_prune = filter.can_prune_row_group(
            Some(&ScalarValue::Int64(1)),
            Some(&ScalarValue::Int64(10)),
        );
        assert!(!can_prune, "Should not prune when value in [min, max]");
    }

    #[test]
    fn test_row_group_pruning_no_stats() {
        let filter = PredicateFilter::new("value", FilterOp::Gt, ScalarValue::Int64(50));

        // No statistics: cannot prune (must be conservative)
        let can_prune = filter.can_prune_row_group(None, None);
        assert!(!can_prune, "Should not prune when statistics unavailable");
    }
}
