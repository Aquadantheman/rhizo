"""
Algebraic Operation Classification

Mathematical Foundation:
Operations that form algebraic structures can be merged conflict-free.

Semilattice (join-semilattice):
  - Associative: (a ⊔ b) ⊔ c = a ⊔ (b ⊔ c)
  - Commutative: a ⊔ b = b ⊔ a
  - Idempotent: a ⊔ a = a
  Examples: max(), min(), set union, set intersection

Abelian Group:
  - Associative: (a + b) + c = a + (b + c)
  - Commutative: a + b = b + a
  - Identity: a + 0 = a
  - Inverse: a + (-a) = 0
  Examples: addition, counters, deltas

If operations form these structures, ORDER DOESN'T MATTER.
Conflicts become mathematically impossible!
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Union
from enum import Enum, auto
import operator


class OpType(Enum):
    """Classification of operation types."""
    # Conflict-free types (can always merge)
    SEMILATTICE_MAX = auto()      # max(a, b) - last writer wins
    SEMILATTICE_MIN = auto()      # min(a, b) - first/smallest wins
    SEMILATTICE_UNION = auto()    # set union - add-only set
    SEMILATTICE_INTERSECT = auto()  # set intersection

    # Commutative types (can merge via combination)
    ABELIAN_ADD = auto()          # a + b - counters, deltas
    ABELIAN_MULTIPLY = auto()     # a * b - scaling factors

    # Requires coordination
    GENERIC_OVERWRITE = auto()    # Last write wins, but needs ordering
    GENERIC_CONDITIONAL = auto()  # CAS-style, needs exact version match

    # Unknown - must be conservative
    UNKNOWN = auto()


@dataclass
class AlgebraicOperation:
    """Represents an operation with algebraic classification."""
    table: str
    row: str
    column: str
    op_type: OpType
    value: Any
    timestamp: float = 0.0

    def can_merge_with(self, other: 'AlgebraicOperation') -> bool:
        """Check if this operation can be automatically merged with another."""
        if self.table != other.table or self.row != other.row or self.column != other.column:
            return True  # Different targets, no conflict

        # Same target - check algebraic compatibility
        return self.op_type == other.op_type and self.op_type in {
            OpType.SEMILATTICE_MAX,
            OpType.SEMILATTICE_MIN,
            OpType.SEMILATTICE_UNION,
            OpType.SEMILATTICE_INTERSECT,
            OpType.ABELIAN_ADD,
            OpType.ABELIAN_MULTIPLY,
        }

    def merge(self, other: 'AlgebraicOperation') -> 'AlgebraicOperation':
        """Merge two operations of the same algebraic type."""
        if not self.can_merge_with(other):
            raise ValueError(f"Cannot merge {self.op_type} with {other.op_type}")

        merged_value = self._compute_merge(other)
        return AlgebraicOperation(
            table=self.table,
            row=self.row,
            column=self.column,
            op_type=self.op_type,
            value=merged_value,
            timestamp=max(self.timestamp, other.timestamp),
        )

    def _compute_merge(self, other: 'AlgebraicOperation') -> Any:
        """Compute merged value based on algebraic type."""
        if self.op_type == OpType.SEMILATTICE_MAX:
            return max(self.value, other.value)
        elif self.op_type == OpType.SEMILATTICE_MIN:
            return min(self.value, other.value)
        elif self.op_type == OpType.SEMILATTICE_UNION:
            return self.value | other.value  # Set union
        elif self.op_type == OpType.SEMILATTICE_INTERSECT:
            return self.value & other.value  # Set intersection
        elif self.op_type == OpType.ABELIAN_ADD:
            return self.value + other.value
        elif self.op_type == OpType.ABELIAN_MULTIPLY:
            return self.value * other.value
        else:
            raise ValueError(f"No merge rule for {self.op_type}")


class AlgebraicClassifier:
    """
    Classifies operations based on their algebraic properties.

    Can automatically detect some patterns from SQL-like operations,
    or accept explicit classification.
    """

    def __init__(self):
        # Column-level type hints (user-provided)
        self.column_types: Dict[Tuple[str, str], OpType] = {}

        # Statistics for auto-classification
        self.operation_history: List[AlgebraicOperation] = []
        self.merge_successes: int = 0
        self.merge_failures: int = 0

    def register_column(self, table: str, column: str, op_type: OpType):
        """Register a column's algebraic type."""
        self.column_types[(table, column)] = op_type

    def classify(
        self,
        table: str,
        row: str,
        column: str,
        operation: str,
        value: Any,
    ) -> AlgebraicOperation:
        """
        Classify an operation based on hints and patterns.

        Args:
            table: Target table
            row: Target row ID
            column: Target column
            operation: SQL-like operation (SET, INCREMENT, APPEND, etc.)
            value: The value being applied

        Returns:
            Classified AlgebraicOperation
        """
        # Check for explicit column type
        if (table, column) in self.column_types:
            op_type = self.column_types[(table, column)]
        else:
            # Try to infer from operation
            op_type = self._infer_type(operation, value)

        return AlgebraicOperation(
            table=table,
            row=row,
            column=column,
            op_type=op_type,
            value=value,
        )

    def _infer_type(self, operation: str, value: Any) -> OpType:
        """Infer operation type from operation string."""
        op_lower = operation.lower()

        if op_lower in ('increment', 'decrement', 'add', 'subtract', '+=', '-='):
            return OpType.ABELIAN_ADD
        elif op_lower in ('max', 'greatest'):
            return OpType.SEMILATTICE_MAX
        elif op_lower in ('min', 'least'):
            return OpType.SEMILATTICE_MIN
        elif op_lower in ('append', 'union', 'add_to_set'):
            return OpType.SEMILATTICE_UNION
        elif op_lower in ('multiply', 'scale', '*='):
            return OpType.ABELIAN_MULTIPLY
        elif op_lower in ('set', 'overwrite', '='):
            return OpType.GENERIC_OVERWRITE
        else:
            return OpType.UNKNOWN

    def can_auto_merge(self, ops1: List[AlgebraicOperation], ops2: List[AlgebraicOperation]) -> bool:
        """
        Check if two operation lists can be automatically merged.

        Returns True if all conflicting operations are algebraically compatible.
        """
        # Group by target
        targets1: Dict[Tuple[str, str, str], AlgebraicOperation] = {}
        targets2: Dict[Tuple[str, str, str], AlgebraicOperation] = {}

        for op in ops1:
            key = (op.table, op.row, op.column)
            if key in targets1:
                # Multiple ops to same target in same list - try to merge
                if targets1[key].can_merge_with(op):
                    targets1[key] = targets1[key].merge(op)
                else:
                    return False
            else:
                targets1[key] = op

        for op in ops2:
            key = (op.table, op.row, op.column)
            if key in targets2:
                if targets2[key].can_merge_with(op):
                    targets2[key] = targets2[key].merge(op)
                else:
                    return False
            else:
                targets2[key] = op

        # Check conflicts between lists
        common_targets = set(targets1.keys()) & set(targets2.keys())
        for target in common_targets:
            if not targets1[target].can_merge_with(targets2[target]):
                return False

        return True

    def merge_operations(
        self,
        ops1: List[AlgebraicOperation],
        ops2: List[AlgebraicOperation],
    ) -> List[AlgebraicOperation]:
        """
        Merge two operation lists.

        Raises ValueError if merge is not possible.
        """
        if not self.can_auto_merge(ops1, ops2):
            self.merge_failures += 1
            raise ValueError("Cannot auto-merge: incompatible operations")

        # Merge by target
        merged: Dict[Tuple[str, str, str], AlgebraicOperation] = {}

        for op in ops1 + ops2:
            key = (op.table, op.row, op.column)
            if key in merged:
                merged[key] = merged[key].merge(op)
            else:
                merged[key] = op

        self.merge_successes += 1
        return list(merged.values())

    def get_stats(self) -> dict:
        """Get classification and merge statistics."""
        return {
            'registered_columns': len(self.column_types),
            'merge_successes': self.merge_successes,
            'merge_failures': self.merge_failures,
            'merge_success_rate': (
                self.merge_successes / (self.merge_successes + self.merge_failures)
                if (self.merge_successes + self.merge_failures) > 0 else 0
            ),
        }


# Convenience functions for common patterns

def counter_delta(table: str, row: str, column: str, delta: int) -> AlgebraicOperation:
    """Create a counter delta operation (always mergeable via addition)."""
    return AlgebraicOperation(
        table=table,
        row=row,
        column=column,
        op_type=OpType.ABELIAN_ADD,
        value=delta,
    )


def set_max(table: str, row: str, column: str, value: Any) -> AlgebraicOperation:
    """Create a max-wins operation (always mergeable via max)."""
    return AlgebraicOperation(
        table=table,
        row=row,
        column=column,
        op_type=OpType.SEMILATTICE_MAX,
        value=value,
    )


def set_min(table: str, row: str, column: str, value: Any) -> AlgebraicOperation:
    """Create a min-wins operation (always mergeable via min)."""
    return AlgebraicOperation(
        table=table,
        row=row,
        column=column,
        op_type=OpType.SEMILATTICE_MIN,
        value=value,
    )


def add_to_set(table: str, row: str, column: str, elements: Set) -> AlgebraicOperation:
    """Create a set-add operation (always mergeable via union)."""
    return AlgebraicOperation(
        table=table,
        row=row,
        column=column,
        op_type=OpType.SEMILATTICE_UNION,
        value=elements,
    )


def demonstrate_algebraic_merge():
    """Demonstrate conflict-free merging via algebraic properties."""
    print("\nAlgebraic Merge Demonstration")
    print("=" * 50)

    classifier = AlgebraicClassifier()

    # Scenario 1: Counter updates (Abelian group)
    print("\n1. Counter Updates (Abelian Group - Addition)")
    branch_a = [
        counter_delta("inventory", "sku_123", "count", -5),  # Sold 5
        counter_delta("inventory", "sku_456", "count", -3),  # Sold 3
    ]
    branch_b = [
        counter_delta("inventory", "sku_123", "count", -2),  # Sold 2
        counter_delta("inventory", "sku_789", "count", +10),  # Restocked 10
    ]

    print(f"  Branch A: {[(op.row, op.value) for op in branch_a]}")
    print(f"  Branch B: {[(op.row, op.value) for op in branch_b]}")

    if classifier.can_auto_merge(branch_a, branch_b):
        merged = classifier.merge_operations(branch_a, branch_b)
        print(f"  Merged: {[(op.row, op.value) for op in merged]}")
        print("  Result: sku_123 total delta = -7 (automatically combined!)")
    else:
        print("  Cannot merge!")

    # Scenario 2: Last-update-timestamp (Semilattice - max)
    print("\n2. Last-Update Timestamps (Semilattice - Max)")
    branch_a = [set_max("users", "user_1", "last_seen", 1000)]
    branch_b = [set_max("users", "user_1", "last_seen", 1500)]

    print(f"  Branch A: last_seen = 1000")
    print(f"  Branch B: last_seen = 1500")

    if classifier.can_auto_merge(branch_a, branch_b):
        merged = classifier.merge_operations(branch_a, branch_b)
        print(f"  Merged: last_seen = {merged[0].value}")
        print("  Result: Most recent timestamp wins automatically!")

    # Scenario 3: Tags (Semilattice - union)
    print("\n3. Tags/Labels (Semilattice - Union)")
    branch_a = [add_to_set("items", "item_1", "tags", {"featured", "sale"})]
    branch_b = [add_to_set("items", "item_1", "tags", {"new", "sale"})]

    print(f"  Branch A: tags = {branch_a[0].value}")
    print(f"  Branch B: tags = {branch_b[0].value}")

    if classifier.can_auto_merge(branch_a, branch_b):
        merged = classifier.merge_operations(branch_a, branch_b)
        print(f"  Merged: tags = {merged[0].value}")
        print("  Result: All tags preserved via union!")

    # Scenario 4: Incompatible operations
    print("\n4. Incompatible Operations (Generic Overwrite)")
    branch_a = [
        AlgebraicOperation("users", "user_1", "name", OpType.GENERIC_OVERWRITE, "Alice"),
    ]
    branch_b = [
        AlgebraicOperation("users", "user_1", "name", OpType.GENERIC_OVERWRITE, "Bob"),
    ]

    print(f"  Branch A: name = 'Alice'")
    print(f"  Branch B: name = 'Bob'")
    print(f"  Can merge: {classifier.can_auto_merge(branch_a, branch_b)}")
    print("  Result: CONFLICT - requires manual resolution")

    print(f"\n  Stats: {classifier.get_stats()}")


if __name__ == '__main__':
    demonstrate_algebraic_merge()
