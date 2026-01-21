"""
Algebraic Classification of SQL Operations

Maps common SQL/database operations to their algebraic signatures,
determining which can be coordination-free.

This is the bridge between theory and practice.
"""

from enum import Enum
from dataclasses import dataclass
from typing import Optional, List, Dict, Any


class AlgebraicSignature(Enum):
    """Algebraic classification of operations."""

    SEMILATTICE = "semilattice"  # Commutative, associative, idempotent
    ABELIAN = "abelian"          # Commutative, associative, has inverse
    GENERIC = "generic"          # Non-commutative, requires coordination

    @property
    def coordination_free(self) -> bool:
        """Can this operation type be coordination-free?"""
        return self in (AlgebraicSignature.SEMILATTICE, AlgebraicSignature.ABELIAN)

    @property
    def min_coordination_rounds(self) -> str:
        """Theoretical minimum coordination rounds."""
        if self.coordination_free:
            return "0"
        return "Omega(log N)"


@dataclass
class OperationClassification:
    """Classification result for an operation."""

    operation: str
    signature: AlgebraicSignature
    reason: str
    examples: List[str]
    caveats: Optional[str] = None


# =============================================================================
# CLASSIFICATION DATABASE
# =============================================================================

CLASSIFICATIONS: Dict[str, OperationClassification] = {

    # =========================================================================
    # SEMILATTICE OPERATIONS (coordination-free, idempotent)
    # =========================================================================

    "MAX": OperationClassification(
        operation="MAX",
        signature=AlgebraicSignature.SEMILATTICE,
        reason="max(max(a,b),c) = max(a,max(b,c)), max(a,b) = max(b,a), max(a,a) = a",
        examples=[
            "UPDATE metrics SET high_water_mark = GREATEST(high_water_mark, @new_value)",
            "UPDATE users SET last_seen = GREATEST(last_seen, @timestamp)",
        ],
    ),

    "MIN": OperationClassification(
        operation="MIN",
        signature=AlgebraicSignature.SEMILATTICE,
        reason="min(min(a,b),c) = min(a,min(b,c)), min(a,b) = min(b,a), min(a,a) = a",
        examples=[
            "UPDATE metrics SET low_water_mark = LEAST(low_water_mark, @new_value)",
            "UPDATE products SET min_price = LEAST(min_price, @price)",
        ],
    ),

    "UNION": OperationClassification(
        operation="UNION (Set Add)",
        signature=AlgebraicSignature.SEMILATTICE,
        reason="A U (B U C) = (A U B) U C, A U B = B U A, A U A = A",
        examples=[
            "UPDATE users SET tags = tags || @new_tag  -- if tags is a set",
            "INSERT INTO tag_assignments (user_id, tag) ... ON CONFLICT DO NOTHING",
        ],
    ),

    "OR": OperationClassification(
        operation="OR (Boolean)",
        signature=AlgebraicSignature.SEMILATTICE,
        reason="(a OR b) OR c = a OR (b OR c), a OR b = b OR a, a OR a = a",
        examples=[
            "UPDATE flags SET is_active = is_active OR @new_flag",
            "UPDATE users SET email_verified = email_verified OR TRUE",
        ],
    ),

    "AND": OperationClassification(
        operation="AND (Boolean)",
        signature=AlgebraicSignature.SEMILATTICE,
        reason="(a AND b) AND c = a AND (b AND c), a AND b = b AND a, a AND a = a",
        examples=[
            "UPDATE flags SET all_checks_passed = all_checks_passed AND @check_result",
        ],
    ),

    # =========================================================================
    # ABELIAN GROUP OPERATIONS (coordination-free, has inverse)
    # =========================================================================

    "ADD": OperationClassification(
        operation="ADD (Increment)",
        signature=AlgebraicSignature.ABELIAN,
        reason="(a+b)+c = a+(b+c), a+b = b+a, identity=0, inverse=-a",
        examples=[
            "UPDATE counters SET page_views = page_views + 1",
            "UPDATE accounts SET balance = balance + @delta",
            "UPDATE metrics SET total = total + @increment",
        ],
        caveats="Loses algebraic property if bounded (overflow check) or non-negative constraint",
    ),

    "MULTIPLY": OperationClassification(
        operation="MULTIPLY (Scale)",
        signature=AlgebraicSignature.ABELIAN,
        reason="(a*b)*c = a*(b*c), a*b = b*a, identity=1, inverse=1/a (if a!=0)",
        examples=[
            "UPDATE metrics SET scale_factor = scale_factor * @multiplier",
            "UPDATE prices SET price = price * @discount_rate",
        ],
        caveats="Loses algebraic property if multiplier can be 0 (no inverse)",
    ),

    # =========================================================================
    # GENERIC OPERATIONS (require coordination)
    # =========================================================================

    "OVERWRITE": OperationClassification(
        operation="OVERWRITE (Set to value)",
        signature=AlgebraicSignature.GENERIC,
        reason="SET(a) then SET(b) != SET(b) then SET(a) in general",
        examples=[
            "UPDATE users SET name = @new_name",
            "UPDATE config SET value = @new_value",
            "INSERT ... ON CONFLICT DO UPDATE SET ...",
        ],
    ),

    "CAS": OperationClassification(
        operation="CAS (Compare-and-swap)",
        signature=AlgebraicSignature.GENERIC,
        reason="CAS depends on current value, violates commutativity",
        examples=[
            "UPDATE accounts SET balance = @new WHERE balance = @expected",
            "Optimistic locking patterns",
        ],
    ),

    "APPEND_ORDERED": OperationClassification(
        operation="APPEND (Ordered list)",
        signature=AlgebraicSignature.GENERIC,
        reason="append(a) then append(b) != append(b) then append(a) for ordered lists",
        examples=[
            "INSERT INTO events (seq, data) VALUES (nextval('seq'), @data)",
            "Audit logs with ordering requirements",
        ],
        caveats="Unordered append (bag/multiset) IS commutative",
    ),

    "DELETE_CONDITIONAL": OperationClassification(
        operation="DELETE (Conditional)",
        signature=AlgebraicSignature.GENERIC,
        reason="DELETE WHERE condition depends on current state",
        examples=[
            "DELETE FROM cart WHERE user_id = @user AND product_id = @product",
        ],
        caveats="Tombstone-based delete (mark deleted) can be semilattice",
    ),

    "FOREIGN_KEY": OperationClassification(
        operation="INSERT with FK check",
        signature=AlgebraicSignature.GENERIC,
        reason="FK constraint requires checking other table's current state",
        examples=[
            "INSERT INTO orders (user_id, ...) -- requires users.id exists",
        ],
    ),

    "UNIQUE_CONSTRAINT": OperationClassification(
        operation="INSERT with UNIQUE check",
        signature=AlgebraicSignature.GENERIC,
        reason="Uniqueness requires checking no concurrent insert of same value",
        examples=[
            "INSERT INTO users (email, ...) -- email must be unique",
        ],
    ),
}


# =============================================================================
# CLASSIFICATION FUNCTIONS
# =============================================================================

def classify_operation(op_name: str) -> Optional[OperationClassification]:
    """Look up classification for a named operation."""
    return CLASSIFICATIONS.get(op_name.upper())


def classify_sql_pattern(sql: str) -> AlgebraicSignature:
    """
    Attempt to classify a SQL statement by pattern matching.

    This is a heuristic - real implementation would use AST analysis.
    """
    sql_upper = sql.upper()

    # Semilattice patterns
    if "GREATEST(" in sql_upper or "= GREATEST(" in sql_upper:
        return AlgebraicSignature.SEMILATTICE
    if "LEAST(" in sql_upper or "= LEAST(" in sql_upper:
        return AlgebraicSignature.SEMILATTICE
    if " OR TRUE" in sql_upper or " OR @" in sql_upper:
        return AlgebraicSignature.SEMILATTICE

    # Abelian patterns
    if "+ 1" in sql or "+ @" in sql or "+= " in sql:
        return AlgebraicSignature.ABELIAN
    if "- 1" in sql or "- @" in sql or "-= " in sql:
        return AlgebraicSignature.ABELIAN

    # Generic patterns (default)
    if "= @" in sql or "= '" in sql:  # Direct assignment
        return AlgebraicSignature.GENERIC

    # Conservative default
    return AlgebraicSignature.GENERIC


def can_be_coordination_free(operations: List[str]) -> bool:
    """
    Check if a set of operations can be coordination-free.

    A transaction is coordination-free iff ALL operations are algebraic.
    """
    for op in operations:
        classification = classify_operation(op)
        if classification is None:
            return False  # Unknown operation, be conservative
        if not classification.signature.coordination_free:
            return False
    return True


# =============================================================================
# ANALYSIS FUNCTIONS
# =============================================================================

def analyze_workload(operations: List[str]) -> Dict[str, Any]:
    """
    Analyze a workload to determine coordination requirements.

    Returns breakdown of operation types and overall coordination need.
    """
    semilattice_count = 0
    abelian_count = 0
    generic_count = 0
    unknown_count = 0

    for op in operations:
        classification = classify_operation(op)
        if classification is None:
            unknown_count += 1
        elif classification.signature == AlgebraicSignature.SEMILATTICE:
            semilattice_count += 1
        elif classification.signature == AlgebraicSignature.ABELIAN:
            abelian_count += 1
        else:
            generic_count += 1

    total = len(operations)
    coordination_free_count = semilattice_count + abelian_count

    return {
        "total_operations": total,
        "semilattice": semilattice_count,
        "abelian": abelian_count,
        "generic": generic_count,
        "unknown": unknown_count,
        "coordination_free_ratio": coordination_free_count / total if total > 0 else 0,
        "requires_coordination": generic_count > 0 or unknown_count > 0,
        "theoretical_speedup": "Inf" if generic_count == 0 else f"O(log N) vs O(1)",
    }


# =============================================================================
# DEMONSTRATION
# =============================================================================

if __name__ == "__main__":
    print("=" * 70)
    print("ALGEBRAIC CLASSIFICATION OF DATABASE OPERATIONS")
    print("=" * 70)

    print("\n## Classification Table\n")
    print(f"{'Operation':<25} {'Signature':<15} {'Coord-Free?':<12} {'Min Rounds'}")
    print("-" * 70)

    for name, classification in CLASSIFICATIONS.items():
        print(f"{classification.operation:<25} "
              f"{classification.signature.value:<15} "
              f"{'Yes' if classification.signature.coordination_free else 'No':<12} "
              f"{classification.signature.min_coordination_rounds}")

    print("\n" + "=" * 70)
    print("EXAMPLE WORKLOAD ANALYSIS")
    print("=" * 70)

    # Example: Analytics workload (mostly coordination-free)
    analytics_workload = ["ADD", "ADD", "MAX", "MAX", "UNION", "ADD"]
    result = analyze_workload(analytics_workload)
    print(f"\nAnalytics Workload: {analytics_workload}")
    print(f"  Coordination-free ratio: {result['coordination_free_ratio']:.0%}")
    print(f"  Requires coordination: {result['requires_coordination']}")

    # Example: CRUD workload (requires coordination)
    crud_workload = ["OVERWRITE", "OVERWRITE", "ADD", "DELETE_CONDITIONAL"]
    result = analyze_workload(crud_workload)
    print(f"\nCRUD Workload: {crud_workload}")
    print(f"  Coordination-free ratio: {result['coordination_free_ratio']:.0%}")
    print(f"  Requires coordination: {result['requires_coordination']}")

    # Example: Counter workload (fully coordination-free)
    counter_workload = ["ADD"] * 10
    result = analyze_workload(counter_workload)
    print(f"\nCounter Workload: {counter_workload}")
    print(f"  Coordination-free ratio: {result['coordination_free_ratio']:.0%}")
    print(f"  Requires coordination: {result['requires_coordination']}")

    print("\n" + "=" * 70)
    print("THE KEY INSIGHT")
    print("=" * 70)
    print("""
    Coordination cost is determined by algebraic properties:

    - Semilattice (MAX, MIN, UNION, OR): C = 0  [proven optimal]
    - Abelian (ADD, MULTIPLY):           C = 0  [proven optimal]
    - Generic (OVERWRITE, CAS):          C = Omega(log N)  [proven necessary]

    Rhizo achieves these bounds exactly.
    """)
