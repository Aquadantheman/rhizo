# Algebraic Classification Architecture Design

> **Status: ✅ IMPLEMENTED** (v0.4.0 - January 2026)
>
> This design has been fully implemented in `rhizo_core::algebraic` with:
> - 306 tests (283 unit + 23 integration)
> - 4M+ ops/sec merge performance
> - Full Python bindings via PyO3
> - Branch-level merge analysis

## Overview

This document details the architecture for integrating algebraic classification into Rhizo's merge system. The goal is to enable conflict-free merging when operations have algebraic properties that guarantee order-independence.

## Mathematical Foundation

### Semilattice Operations
A join-semilattice satisfies:
- **Associative**: (a ⊔ b) ⊔ c = a ⊔ (b ⊔ c)
- **Commutative**: a ⊔ b = b ⊔ a
- **Idempotent**: a ⊔ a = a

Examples:
- `MAX(a, b)` — last-writer-wins timestamps
- `MIN(a, b)` — first-writer-wins
- `UNION(A, B)` — add-only sets (tags, permissions)
- `INTERSECT(A, B)` — restrictive sets

### Abelian Group Operations
An Abelian group satisfies:
- **Associative**: (a + b) + c = a + (b + c)
- **Commutative**: a + b = b + a
- **Identity**: a + 0 = a
- **Inverse**: a + (-a) = 0

Examples:
- `ADD(delta)` — counters, inventory deltas
- `MULTIPLY(factor)` — scaling factors

### Conflict-Free Theorem
**Theorem 1**: If operations O₁ and O₂ on column C both use algebraic type T ∈ {Semilattice, Abelian}, then:
```
merge(O₁, O₂) = T.combine(O₁.value, O₂.value)
```
is deterministic regardless of order.

**Proof**: By definition of commutativity: `combine(a, b) = combine(b, a)`.

## Module Structure

```
rhizo_core/src/
├── algebraic/
│   ├── mod.rs           # Module exports
│   ├── types.rs         # Core types: OpType, AlgebraicValue
│   ├── schema.rs        # ColumnAlgebraic annotation
│   ├── merge.rs         # Merge rules implementation
│   └── detector.rs      # AlgebraicConflictDetector
├── transaction/
│   ├── types.rs         # Add operation_type to TableWrite (modified)
│   └── conflict.rs      # Add AlgebraicConflictDetector (modified)
└── branch/
    └── manager.rs       # Add merge_algebraic (modified)
```

## Core Types

### `algebraic/types.rs`

```rust
/// Algebraic operation classification
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum OpType {
    // === Semilattice Operations (conflict-free) ===
    /// MAX(a, b) - last-writer-wins semantics
    SemilatticeMax,
    /// MIN(a, b) - first-writer-wins semantics
    SemilatticeMin,
    /// UNION(A, B) - add-only set
    SemilatticeUnion,
    /// INTERSECT(A, B) - remove-only set
    SemilatticeIntersect,

    // === Abelian Group Operations (conflict-free) ===
    /// a + b - counters, deltas
    AbelianAdd,
    /// a * b - multiplicative scaling
    AbelianMultiply,

    // === Non-Algebraic Operations (may conflict) ===
    /// Direct value overwrite (last-write-wins with ordering)
    GenericOverwrite,
    /// CAS-style (requires exact version match)
    GenericConditional,

    // === Unknown (conservative) ===
    Unknown,
}

impl OpType {
    /// Check if this operation type is conflict-free
    pub fn is_conflict_free(&self) -> bool {
        matches!(self,
            Self::SemilatticeMax | Self::SemilatticeMin |
            Self::SemilatticeUnion | Self::SemilatticeIntersect |
            Self::AbelianAdd | Self::AbelianMultiply
        )
    }

    /// Check if two operation types can be merged
    pub fn can_merge_with(&self, other: &Self) -> bool {
        self == other && self.is_conflict_free()
    }
}

/// Value that can be algebraically merged
#[derive(Debug, Clone, Serialize, Deserialize)]
pub enum AlgebraicValue {
    /// Numeric value (for max, min, add, multiply)
    Number(f64),
    /// Integer value (for counters)
    Integer(i64),
    /// Set of strings (for union, intersect)
    StringSet(HashSet<String>),
    /// Set of integers (for union, intersect)
    IntSet(HashSet<i64>),
    /// Raw bytes (for generic operations)
    Bytes(Vec<u8>),
    /// JSON value (for complex types)
    Json(serde_json::Value),
}
```

### `algebraic/schema.rs`

```rust
/// Algebraic annotation for a column
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ColumnAlgebraic {
    /// Column name
    pub column: String,
    /// Operation type for this column
    pub op_type: OpType,
    /// Identity element (for Abelian operations)
    pub identity: Option<AlgebraicValue>,
    /// Description for documentation
    pub description: Option<String>,
}

/// Schema-level algebraic configuration for a table
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct TableAlgebraicSchema {
    /// Table name
    pub table: String,
    /// Column-level algebraic annotations
    pub columns: HashMap<String, ColumnAlgebraic>,
    /// Default operation type for unannotated columns
    pub default_op_type: OpType,
}

impl TableAlgebraicSchema {
    pub fn new(table: impl Into<String>) -> Self {
        Self {
            table: table.into(),
            columns: HashMap::new(),
            default_op_type: OpType::Unknown,
        }
    }

    /// Add a column annotation
    pub fn add_column(&mut self, column: impl Into<String>, op_type: OpType) {
        let col_name = column.into();
        self.columns.insert(col_name.clone(), ColumnAlgebraic {
            column: col_name,
            op_type,
            identity: None,
            description: None,
        });
    }

    /// Get operation type for a column
    pub fn get_op_type(&self, column: &str) -> OpType {
        self.columns
            .get(column)
            .map(|c| c.op_type)
            .unwrap_or(self.default_op_type)
    }
}
```

### `algebraic/merge.rs`

```rust
/// Result of attempting an algebraic merge
#[derive(Debug)]
pub enum MergeResult {
    /// Successfully merged to single value
    Merged(AlgebraicValue),
    /// Cannot merge - true conflict
    Conflict { value1: AlgebraicValue, value2: AlgebraicValue },
    /// Type mismatch - cannot compare
    TypeMismatch(String),
}

/// Algebraic merge engine
pub struct AlgebraicMerger;

impl AlgebraicMerger {
    /// Attempt to merge two values using algebraic properties
    pub fn merge(
        op_type: OpType,
        value1: &AlgebraicValue,
        value2: &AlgebraicValue,
    ) -> MergeResult {
        if !op_type.is_conflict_free() {
            return MergeResult::Conflict {
                value1: value1.clone(),
                value2: value2.clone(),
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
            },
        }
    }

    fn merge_max(v1: &AlgebraicValue, v2: &AlgebraicValue) -> MergeResult {
        match (v1, v2) {
            (AlgebraicValue::Number(a), AlgebraicValue::Number(b)) => {
                MergeResult::Merged(AlgebraicValue::Number(a.max(*b)))
            }
            (AlgebraicValue::Integer(a), AlgebraicValue::Integer(b)) => {
                MergeResult::Merged(AlgebraicValue::Integer(*a.max(b)))
            }
            _ => MergeResult::TypeMismatch("MAX requires numeric types".into()),
        }
    }

    fn merge_min(v1: &AlgebraicValue, v2: &AlgebraicValue) -> MergeResult {
        match (v1, v2) {
            (AlgebraicValue::Number(a), AlgebraicValue::Number(b)) => {
                MergeResult::Merged(AlgebraicValue::Number(a.min(*b)))
            }
            (AlgebraicValue::Integer(a), AlgebraicValue::Integer(b)) => {
                MergeResult::Merged(AlgebraicValue::Integer(*a.min(b)))
            }
            _ => MergeResult::TypeMismatch("MIN requires numeric types".into()),
        }
    }

    fn merge_union(v1: &AlgebraicValue, v2: &AlgebraicValue) -> MergeResult {
        match (v1, v2) {
            (AlgebraicValue::StringSet(a), AlgebraicValue::StringSet(b)) => {
                MergeResult::Merged(AlgebraicValue::StringSet(a.union(b).cloned().collect()))
            }
            (AlgebraicValue::IntSet(a), AlgebraicValue::IntSet(b)) => {
                MergeResult::Merged(AlgebraicValue::IntSet(a.union(b).copied().collect()))
            }
            _ => MergeResult::TypeMismatch("UNION requires set types".into()),
        }
    }

    fn merge_intersect(v1: &AlgebraicValue, v2: &AlgebraicValue) -> MergeResult {
        match (v1, v2) {
            (AlgebraicValue::StringSet(a), AlgebraicValue::StringSet(b)) => {
                MergeResult::Merged(AlgebraicValue::StringSet(a.intersection(b).cloned().collect()))
            }
            (AlgebraicValue::IntSet(a), AlgebraicValue::IntSet(b)) => {
                MergeResult::Merged(AlgebraicValue::IntSet(a.intersection(b).copied().collect()))
            }
            _ => MergeResult::TypeMismatch("INTERSECT requires set types".into()),
        }
    }

    fn merge_add(v1: &AlgebraicValue, v2: &AlgebraicValue) -> MergeResult {
        match (v1, v2) {
            (AlgebraicValue::Number(a), AlgebraicValue::Number(b)) => {
                MergeResult::Merged(AlgebraicValue::Number(a + b))
            }
            (AlgebraicValue::Integer(a), AlgebraicValue::Integer(b)) => {
                MergeResult::Merged(AlgebraicValue::Integer(a + b))
            }
            _ => MergeResult::TypeMismatch("ADD requires numeric types".into()),
        }
    }

    fn merge_multiply(v1: &AlgebraicValue, v2: &AlgebraicValue) -> MergeResult {
        match (v1, v2) {
            (AlgebraicValue::Number(a), AlgebraicValue::Number(b)) => {
                MergeResult::Merged(AlgebraicValue::Number(a * b))
            }
            (AlgebraicValue::Integer(a), AlgebraicValue::Integer(b)) => {
                MergeResult::Merged(AlgebraicValue::Integer(a * b))
            }
            _ => MergeResult::TypeMismatch("MULTIPLY requires numeric types".into()),
        }
    }
}
```

### `algebraic/detector.rs`

```rust
/// Algebraic conflict detector
///
/// Uses schema annotations to determine if conflicts can be auto-resolved.
pub struct AlgebraicConflictDetector {
    /// Schema registry for algebraic annotations
    schemas: HashMap<String, TableAlgebraicSchema>,
    /// Fallback detector for non-algebraic operations
    fallback: Box<dyn ConflictDetector>,
}

impl AlgebraicConflictDetector {
    pub fn new() -> Self {
        Self {
            schemas: HashMap::new(),
            fallback: Box::new(TableLevelConflictDetector),
        }
    }

    pub fn register_schema(&mut self, schema: TableAlgebraicSchema) {
        self.schemas.insert(schema.table.clone(), schema);
    }
}

impl ConflictDetector for AlgebraicConflictDetector {
    fn detect(&self, tx1: &TransactionRecord, tx2: &TransactionRecord) -> Option<Conflict> {
        let mut conflicting_tables = Vec::new();

        // Find tables written by both
        let tables1: HashSet<_> = tx1.writes.iter().map(|w| &w.table_name).collect();
        let tables2: HashSet<_> = tx2.writes.iter().map(|w| &w.table_name).collect();

        for table in tables1.intersection(&tables2) {
            // Check if this table has algebraic schema
            if let Some(schema) = self.schemas.get(*table) {
                // Get writes for this table
                let write1 = tx1.writes.iter().find(|w| &w.table_name == *table);
                let write2 = tx2.writes.iter().find(|w| &w.table_name == *table);

                if let (Some(w1), Some(w2)) = (write1, write2) {
                    // Check if all affected columns are algebraically mergeable
                    // For now, check if table default is conflict-free
                    if !schema.default_op_type.is_conflict_free() {
                        conflicting_tables.push(table.to_string());
                    }
                    // If conflict-free, no conflict for this table
                }
            } else {
                // No schema = assume conflict
                conflicting_tables.push(table.to_string());
            }
        }

        if conflicting_tables.is_empty() {
            None
        } else {
            Some(Conflict::new(conflicting_tables, tx1.tx_id, tx2.tx_id))
        }
    }

    fn name(&self) -> &'static str {
        "AlgebraicConflictDetector"
    }
}
```

## Integration Points

### 1. Transaction Types (transaction/types.rs)

Add to `TableWrite`:
```rust
/// Algebraic operation type for this write (None = use schema default)
pub operation_type: Option<OpType>,

/// Column-level operation values for algebraic merge
pub column_operations: HashMap<String, (OpType, AlgebraicValue)>,
```

### 2. Branch Manager (branch/manager.rs)

Add new method:
```rust
/// Merge using algebraic properties when possible
pub fn merge_algebraic(
    &self,
    source: &str,
    into: &str,
    schemas: &HashMap<String, TableAlgebraicSchema>,
) -> Result<MergeOutcome, BranchError> {
    // 1. Compute diff
    // 2. For each modified table:
    //    a. If all columns are conflict-free, merge algebraically
    //    b. Otherwise, report conflict
    // 3. Return merged state or conflict list
}
```

### 3. Catalog Enhancement

Store `TableAlgebraicSchema` alongside table metadata:
```rust
pub struct TableMetadata {
    // ... existing fields ...

    /// Algebraic schema for merge operations
    pub algebraic_schema: Option<TableAlgebraicSchema>,
}
```

## Implementation Phases

### Phase 1: Core Types (this PR)
- [ ] Create `rhizo_core/src/algebraic/mod.rs`
- [ ] Implement `types.rs` with OpType, AlgebraicValue
- [ ] Implement `merge.rs` with AlgebraicMerger
- [ ] Add comprehensive unit tests

### Phase 2: Schema Integration
- [ ] Implement `schema.rs`
- [ ] Add schema storage to catalog
- [ ] CLI commands for schema management

### Phase 3: Conflict Detection
- [ ] Implement `detector.rs`
- [ ] Integrate with TransactionManager
- [ ] Update conflict detection to use algebraic detector

### Phase 4: Branch Merge
- [ ] Add `merge_algebraic` to BranchManager
- [ ] Compute actual merged values for algebraic columns
- [ ] Write merged state to new version

### Phase 5: Python Bindings
- [ ] Expose OpType, AlgebraicValue to Python
- [ ] Python API for schema annotations
- [ ] Python API for algebraic merge

## Testing Strategy

### Unit Tests
- OpType classification (is_conflict_free, can_merge_with)
- AlgebraicMerger for each operation type
- Edge cases (empty sets, zero values, overflow)

### Integration Tests
- Two transactions with algebraic columns → no conflict
- Two transactions with mixed columns → partial conflict
- Schema loading from catalog

### Property-Based Tests
- Commutativity: merge(a, b) = merge(b, a)
- Associativity: merge(merge(a, b), c) = merge(a, merge(b, c))
- Idempotency (semilattice): merge(a, a) = a

### Benchmark Tests
- Compare merge latency: fast-forward vs algebraic
- Memory usage for schema storage
- Conflict detection throughput

## Success Metrics

1. **Correctness**: 100% of algebraic merges produce mathematically correct results
2. **Performance**: Algebraic merge adds < 5% overhead vs fast-forward
3. **Conflict Reduction**: 50%+ of table-level conflicts auto-resolved
4. **API Ergonomics**: Schema annotation requires < 5 lines per table
