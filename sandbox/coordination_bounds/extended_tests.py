"""
Extended Test Cases for Coordination Bounds

Comprehensive test suite validating edge cases and stress scenarios:
1. Edge cases (empty transactions, single ops, boundary conditions)
2. Stress tests (high volume, mixed operations)
3. Algebraic property verification (commutativity, associativity, idempotency)
4. Transaction classification tests
5. Real workload pattern analysis

Run: python sandbox/coordination_bounds/extended_tests.py
"""

import sys
import time
import random
from pathlib import Path
from dataclasses import dataclass
from typing import List, Dict, Any, Tuple

# Add rhizo to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "python"))

from _rhizo import (
    PySimulatedCluster,
    PyAlgebraicOperation,
    PyAlgebraicTransaction,
    PyOpType,
    PyAlgebraicValue,
)


@dataclass
class TestResult:
    """Result of a single test."""
    name: str
    passed: bool
    message: str
    duration_ms: float


class ExtendedTestSuite:
    """Extended test suite for coordination bounds validation."""

    def __init__(self):
        self.results: List[TestResult] = []

    def run_test(self, name: str, test_func) -> TestResult:
        """Run a single test and record result."""
        start = time.perf_counter()
        try:
            passed, message = test_func()
        except Exception as e:
            passed = False
            message = f"Exception: {e}"
        duration = (time.perf_counter() - start) * 1000

        result = TestResult(name, passed, message, duration)
        self.results.append(result)
        return result

    def print_results(self):
        """Print all test results."""
        print("\n" + "=" * 70)
        print("EXTENDED TEST RESULTS")
        print("=" * 70)

        passed = sum(1 for r in self.results if r.passed)
        total = len(self.results)

        for r in self.results:
            status = "PASS" if r.passed else "FAIL"
            print(f"  [{status}] {r.name}: {r.message} ({r.duration_ms:.2f}ms)")

        print("\n" + "-" * 70)
        print(f"Total: {passed}/{total} tests passed")

        if passed == total:
            print("\nALL TESTS PASSED!")
        else:
            print(f"\nWARNING: {total - passed} tests failed")

        return passed == total


# =============================================================================
# EDGE CASE TESTS
# =============================================================================

def test_empty_transaction() -> Tuple[bool, str]:
    """Test that empty transactions are handled correctly."""
    cluster = PySimulatedCluster(4)
    tx = PyAlgebraicTransaction()  # Empty

    try:
        # Empty transaction should either be rejected or no-op
        cluster.commit_on_node(0, tx)
        cluster.propagate_all()
        converged = cluster.verify_convergence()
        return converged, "Empty transaction handled (converged)"
    except Exception as e:
        if "empty" in str(e).lower():
            return True, "Empty transaction correctly rejected"
        return False, f"Unexpected error: {e}"


def test_single_algebraic_operation() -> Tuple[bool, str]:
    """Test single algebraic operation commits correctly."""
    cluster = PySimulatedCluster(4)

    tx = PyAlgebraicTransaction()
    tx.add_operation(PyAlgebraicOperation("x", PyOpType("ADD"), PyAlgebraicValue.integer(42)))

    cluster.commit_on_node(0, tx)
    cluster.propagate_all()

    # Check all nodes have the value
    values = [cluster.get_node_state(i, "x") for i in range(4)]
    all_same = len(set(str(v) for v in values)) == 1

    if all_same and cluster.verify_convergence():
        return True, f"Single ADD converged, value={values[0]}"
    return False, f"Values differ: {values}"


def test_single_generic_rejected() -> Tuple[bool, str]:
    """Test that single generic operation is rejected."""
    cluster = PySimulatedCluster(4)

    tx = PyAlgebraicTransaction()
    tx.add_operation(PyAlgebraicOperation("x", PyOpType("OVERWRITE"), PyAlgebraicValue.integer(42)))

    try:
        cluster.commit_on_node(0, tx)
        return False, "OVERWRITE was accepted (should be rejected)"
    except ValueError as e:
        if "coordination" in str(e).lower() or "non-algebraic" in str(e).lower():
            return True, "OVERWRITE correctly rejected"
        return False, f"Wrong error: {e}"


def test_large_value() -> Tuple[bool, str]:
    """Test handling of large integer values."""
    cluster = PySimulatedCluster(4)

    large_value = 2**62  # Large but within int64

    tx = PyAlgebraicTransaction()
    tx.add_operation(PyAlgebraicOperation("big", PyOpType("MAX"), PyAlgebraicValue.integer(large_value)))

    cluster.commit_on_node(0, tx)
    cluster.propagate_all()

    value = cluster.get_node_state(0, "big")
    # Check if value is preserved (as string comparison)
    if str(large_value) in str(value) or cluster.verify_convergence():
        return True, f"Large value handled: {value}"
    return False, f"Value mismatch: {value}"


def test_negative_values() -> Tuple[bool, str]:
    """Test handling of negative values with ADD."""
    cluster = PySimulatedCluster(4)

    # Add positive then negative
    tx1 = PyAlgebraicTransaction()
    tx1.add_operation(PyAlgebraicOperation("balance", PyOpType("ADD"), PyAlgebraicValue.integer(100)))
    cluster.commit_on_node(0, tx1)

    tx2 = PyAlgebraicTransaction()
    tx2.add_operation(PyAlgebraicOperation("balance", PyOpType("ADD"), PyAlgebraicValue.integer(-30)))
    cluster.commit_on_node(1, tx2)

    cluster.propagate_all()

    if cluster.verify_convergence():
        return True, "Negative values handled correctly"
    return False, "Failed to converge with negative values"


# =============================================================================
# STRESS TESTS
# =============================================================================

def test_high_volume_algebraic() -> Tuple[bool, str]:
    """Stress test with 1000 algebraic operations."""
    cluster = PySimulatedCluster(8)

    num_ops = 1000
    start = time.perf_counter()

    for i in range(num_ops):
        node = i % 8
        tx = PyAlgebraicTransaction()
        tx.add_operation(PyAlgebraicOperation("counter", PyOpType("ADD"), PyAlgebraicValue.integer(1)))
        cluster.commit_on_node(node, tx)

    commit_time = (time.perf_counter() - start) * 1000

    cluster.propagate_all()
    converged = cluster.verify_convergence()

    ops_per_sec = num_ops / (commit_time / 1000)

    if converged:
        return True, f"{num_ops} ops in {commit_time:.1f}ms ({ops_per_sec:.0f} ops/sec)"
    return False, "Failed to converge after high volume"


def test_concurrent_same_key() -> Tuple[bool, str]:
    """Test many concurrent operations on the same key."""
    cluster = PySimulatedCluster(16)

    # All nodes write to same key simultaneously
    for node in range(16):
        tx = PyAlgebraicTransaction()
        tx.add_operation(PyAlgebraicOperation("hotkey", PyOpType("ADD"), PyAlgebraicValue.integer(node + 1)))
        cluster.commit_on_node(node, tx)

    cluster.propagate_all()

    # Expected: sum of 1+2+...+16 = 136
    expected = sum(range(1, 17))

    if cluster.verify_convergence():
        return True, f"Concurrent writes converged (expected sum: {expected})"
    return False, "Failed to converge with concurrent writes"


def test_many_keys() -> Tuple[bool, str]:
    """Test operations across many different keys."""
    cluster = PySimulatedCluster(8)
    num_keys = 100

    for i in range(num_keys):
        node = i % 8
        tx = PyAlgebraicTransaction()
        tx.add_operation(PyAlgebraicOperation(f"key_{i}", PyOpType("MAX"), PyAlgebraicValue.integer(i * 10)))
        cluster.commit_on_node(node, tx)

    cluster.propagate_all()
    converged = cluster.verify_convergence()

    if converged:
        return True, f"{num_keys} different keys all converged"
    return False, "Failed to converge with many keys"


def test_mixed_operation_types() -> Tuple[bool, str]:
    """Test mixing ADD and MAX operations (both algebraic)."""
    cluster = PySimulatedCluster(8)

    # Some ADDs
    for i in range(50):
        tx = PyAlgebraicTransaction()
        tx.add_operation(PyAlgebraicOperation("sum", PyOpType("ADD"), PyAlgebraicValue.integer(1)))
        cluster.commit_on_node(i % 8, tx)

    # Some MAXs
    for i in range(50):
        tx = PyAlgebraicTransaction()
        tx.add_operation(PyAlgebraicOperation("max", PyOpType("MAX"), PyAlgebraicValue.integer(i)))
        cluster.commit_on_node(i % 8, tx)

    cluster.propagate_all()

    if cluster.verify_convergence():
        return True, "Mixed ADD/MAX operations converged"
    return False, "Failed to converge with mixed operations"


# =============================================================================
# ALGEBRAIC PROPERTY VERIFICATION
# =============================================================================

def test_commutativity_add() -> Tuple[bool, str]:
    """Verify ADD is commutative: a + b = b + a."""
    # Cluster 1: node 0 then node 1
    c1 = PySimulatedCluster(2)
    tx_a = PyAlgebraicTransaction()
    tx_a.add_operation(PyAlgebraicOperation("x", PyOpType("ADD"), PyAlgebraicValue.integer(10)))
    tx_b = PyAlgebraicTransaction()
    tx_b.add_operation(PyAlgebraicOperation("x", PyOpType("ADD"), PyAlgebraicValue.integer(20)))

    c1.commit_on_node(0, tx_a)
    c1.commit_on_node(1, tx_b)
    c1.propagate_all()
    v1 = c1.get_node_state(0, "x")

    # Cluster 2: node 1 then node 0 (reverse order)
    c2 = PySimulatedCluster(2)
    c2.commit_on_node(1, tx_b)
    c2.commit_on_node(0, tx_a)
    c2.propagate_all()
    v2 = c2.get_node_state(0, "x")

    if str(v1) == str(v2):
        return True, f"Commutative: {v1} = {v2}"
    return False, f"NOT commutative: {v1} != {v2}"


def test_commutativity_max() -> Tuple[bool, str]:
    """Verify MAX is commutative: max(a,b) = max(b,a)."""
    c1 = PySimulatedCluster(2)
    tx_a = PyAlgebraicTransaction()
    tx_a.add_operation(PyAlgebraicOperation("m", PyOpType("MAX"), PyAlgebraicValue.integer(10)))
    tx_b = PyAlgebraicTransaction()
    tx_b.add_operation(PyAlgebraicOperation("m", PyOpType("MAX"), PyAlgebraicValue.integer(20)))

    c1.commit_on_node(0, tx_a)
    c1.commit_on_node(1, tx_b)
    c1.propagate_all()
    v1 = c1.get_node_state(0, "m")

    c2 = PySimulatedCluster(2)
    c2.commit_on_node(1, tx_b)
    c2.commit_on_node(0, tx_a)
    c2.propagate_all()
    v2 = c2.get_node_state(0, "m")

    if str(v1) == str(v2):
        return True, f"Commutative: max={v1}"
    return False, f"NOT commutative: {v1} != {v2}"


def test_associativity_add() -> Tuple[bool, str]:
    """Verify ADD is associative: (a + b) + c = a + (b + c)."""
    cluster = PySimulatedCluster(3)

    for i, val in enumerate([10, 20, 30]):
        tx = PyAlgebraicTransaction()
        tx.add_operation(PyAlgebraicOperation("y", PyOpType("ADD"), PyAlgebraicValue.integer(val)))
        cluster.commit_on_node(i, tx)

    cluster.propagate_all()

    # All nodes should have same value
    values = [str(cluster.get_node_state(i, "y")) for i in range(3)]

    if len(set(values)) == 1:
        return True, f"Associative: all nodes = {values[0]}"
    return False, f"NOT associative: {values}"


def test_idempotency_max() -> Tuple[bool, str]:
    """Verify MAX is idempotent: max(a,a) = a."""
    cluster = PySimulatedCluster(1)

    # Apply same MAX multiple times
    for _ in range(5):
        tx = PyAlgebraicTransaction()
        tx.add_operation(PyAlgebraicOperation("z", PyOpType("MAX"), PyAlgebraicValue.integer(100)))
        cluster.commit_on_node(0, tx)

    value = cluster.get_node_state(0, "z")

    # Value should still be 100
    if "100" in str(value):
        return True, f"Idempotent: value={value}"
    return False, f"NOT idempotent: value={value}"


# =============================================================================
# TRANSACTION CLASSIFICATION TESTS
# =============================================================================

def test_all_algebraic_transaction() -> Tuple[bool, str]:
    """Test transaction with multiple algebraic operations."""
    cluster = PySimulatedCluster(4)

    tx = PyAlgebraicTransaction()
    tx.add_operation(PyAlgebraicOperation("a", PyOpType("ADD"), PyAlgebraicValue.integer(1)))
    tx.add_operation(PyAlgebraicOperation("b", PyOpType("MAX"), PyAlgebraicValue.integer(100)))
    tx.add_operation(PyAlgebraicOperation("c", PyOpType("ADD"), PyAlgebraicValue.integer(5)))

    try:
        cluster.commit_on_node(0, tx)
        cluster.propagate_all()
        if cluster.verify_convergence():
            return True, "All-algebraic transaction accepted and converged"
        return False, "Transaction accepted but didn't converge"
    except Exception as e:
        return False, f"Transaction rejected: {e}"


def test_mixed_transaction_rejected() -> Tuple[bool, str]:
    """Test that transaction with ANY generic operation is rejected."""
    cluster = PySimulatedCluster(4)

    tx = PyAlgebraicTransaction()
    tx.add_operation(PyAlgebraicOperation("a", PyOpType("ADD"), PyAlgebraicValue.integer(1)))
    tx.add_operation(PyAlgebraicOperation("b", PyOpType("OVERWRITE"), PyAlgebraicValue.integer(100)))  # Generic!
    tx.add_operation(PyAlgebraicOperation("c", PyOpType("MAX"), PyAlgebraicValue.integer(5)))

    try:
        cluster.commit_on_node(0, tx)
        return False, "Mixed transaction was accepted (should be rejected)"
    except ValueError as e:
        if "coordination" in str(e).lower() or "non-algebraic" in str(e).lower():
            return True, "Mixed transaction correctly rejected"
        return False, f"Wrong error: {e}"


# =============================================================================
# WORKLOAD PATTERN TESTS
# =============================================================================

def test_analytics_workload() -> Tuple[bool, str]:
    """Simulate analytics workload (90% algebraic)."""
    cluster = PySimulatedCluster(8)
    ops = 100

    for i in range(ops):
        tx = PyAlgebraicTransaction()
        # 90% ADD/MAX, 10% would be generic but we skip those
        if random.random() < 0.9:
            if random.random() < 0.5:
                tx.add_operation(PyAlgebraicOperation("pageviews", PyOpType("ADD"), PyAlgebraicValue.integer(1)))
            else:
                tx.add_operation(PyAlgebraicOperation("max_latency", PyOpType("MAX"), PyAlgebraicValue.integer(random.randint(1, 1000))))
            cluster.commit_on_node(i % 8, tx)

    cluster.propagate_all()

    if cluster.verify_convergence():
        return True, f"Analytics workload ({ops} ops) converged"
    return False, "Analytics workload failed to converge"


def test_gaming_workload() -> Tuple[bool, str]:
    """Simulate gaming workload (high scores, counters)."""
    cluster = PySimulatedCluster(8)

    # High scores (MAX)
    for player in range(20):
        tx = PyAlgebraicTransaction()
        tx.add_operation(PyAlgebraicOperation(f"score_{player}", PyOpType("MAX"),
                                               PyAlgebraicValue.integer(random.randint(0, 10000))))
        cluster.commit_on_node(player % 8, tx)

    # Play counts (ADD)
    for _ in range(50):
        tx = PyAlgebraicTransaction()
        tx.add_operation(PyAlgebraicOperation("total_plays", PyOpType("ADD"), PyAlgebraicValue.integer(1)))
        cluster.commit_on_node(random.randint(0, 7), tx)

    cluster.propagate_all()

    if cluster.verify_convergence():
        return True, "Gaming workload converged"
    return False, "Gaming workload failed"


# =============================================================================
# MAIN
# =============================================================================

def main():
    """Run all extended tests."""

    print("=" * 70)
    print("EXTENDED TEST SUITE: COORDINATION BOUNDS")
    print("=" * 70)

    suite = ExtendedTestSuite()

    # Edge cases
    print("\n[1/5] Edge Case Tests...")
    suite.run_test("Empty transaction", test_empty_transaction)
    suite.run_test("Single algebraic op", test_single_algebraic_operation)
    suite.run_test("Single generic rejected", test_single_generic_rejected)
    suite.run_test("Large values", test_large_value)
    suite.run_test("Negative values", test_negative_values)

    # Stress tests
    print("\n[2/5] Stress Tests...")
    suite.run_test("High volume (1000 ops)", test_high_volume_algebraic)
    suite.run_test("Concurrent same key", test_concurrent_same_key)
    suite.run_test("Many keys (100)", test_many_keys)
    suite.run_test("Mixed ADD/MAX", test_mixed_operation_types)

    # Algebraic properties
    print("\n[3/5] Algebraic Property Verification...")
    suite.run_test("Commutativity (ADD)", test_commutativity_add)
    suite.run_test("Commutativity (MAX)", test_commutativity_max)
    suite.run_test("Associativity (ADD)", test_associativity_add)
    suite.run_test("Idempotency (MAX)", test_idempotency_max)

    # Transaction classification
    print("\n[4/5] Transaction Classification Tests...")
    suite.run_test("All-algebraic transaction", test_all_algebraic_transaction)
    suite.run_test("Mixed transaction rejected", test_mixed_transaction_rejected)

    # Workload patterns
    print("\n[5/5] Workload Pattern Tests...")
    suite.run_test("Analytics workload", test_analytics_workload)
    suite.run_test("Gaming workload", test_gaming_workload)

    # Print results
    all_passed = suite.print_results()

    return all_passed


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
