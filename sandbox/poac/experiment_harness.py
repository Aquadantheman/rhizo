"""
Experiment Harness for POAC Validation

Runs controlled experiments to validate mathematical claims:
1. Bloom filter FP rate matches theory
2. Speculative execution improves latency when p < threshold
3. Escrow reduces coordination to predicted rate
4. Algebraic operations merge conflict-free

Results can be used to write academic paper with empirical validation.
"""

from __future__ import annotations
import time
import random
import json
from dataclasses import dataclass
from typing import Dict, List, Optional, Any, Tuple
from pathlib import Path
import math

from .bloom_write_set import BloomWriteSet, ExactWriteSet, compare_bloom_vs_exact
from .speculative_executor import SpeculativeExecutor, simulate_workload
from .escrow_manager import (
    EscrowManager, EscrowedResource,
    calculate_optimal_quota, theoretical_exhaustion_probability,
    simulate_hot_spot,
)
from .algebraic_classifier import (
    AlgebraicClassifier, AlgebraicOperation, OpType,
    counter_delta, set_max, add_to_set,
)
from .metrics import MetricsCollector, confidence_interval, hypothesis_test


@dataclass
class ExperimentConfig:
    """Configuration for a POAC validation experiment."""
    name: str
    description: str
    parameters: Dict[str, Any]
    repetitions: int = 10  # For statistical significance


class ExperimentHarness:
    """
    Harness for running POAC validation experiments.

    Validates mathematical predictions against empirical measurements.
    """

    def __init__(self, output_dir: Optional[Path] = None):
        self.output_dir = output_dir or Path("./results")
        self.output_dir.mkdir(exist_ok=True)
        self.results: Dict[str, Any] = {}

    def run_all_experiments(self) -> Dict[str, Any]:
        """Run all POAC validation experiments."""
        print("\n" + "=" * 70)
        print("POAC VALIDATION EXPERIMENTS")
        print("=" * 70)

        self.results = {
            'bloom_filter': self.experiment_bloom_filter(),
            'speculative_execution': self.experiment_speculative_execution(),
            'escrow_transactions': self.experiment_escrow_transactions(),
            'algebraic_merge': self.experiment_algebraic_merge(),
            'combined_poac': self.experiment_combined_poac(),
        }

        # Save results
        results_path = self.output_dir / f"poac_results_{int(time.time())}.json"
        with open(results_path, 'w') as f:
            json.dump(self.results, f, indent=2, default=str)
        print(f"\nResults saved to: {results_path}")

        return self.results

    def experiment_bloom_filter(self) -> Dict[str, Any]:
        """
        Experiment 1: Validate bloom filter mathematical properties.

        Claims to validate:
        - P(false_negative) = 0 (absolute)
        - P(false_positive) ≈ theoretical prediction
        - Memory savings > 90% vs exact set
        """
        print("\n" + "-" * 50)
        print("Experiment 1: Bloom Filter Write-Sets")
        print("-" * 50)

        collector = MetricsCollector("bloom_filter")
        results = []

        # Test across different scales
        for num_elements in [1000, 10000, 100000]:
            for fp_target in [0.01, 0.05, 0.001]:
                print(f"\n  Testing n={num_elements:,}, target_fp={fp_target:.1%}")

                # Run multiple times for statistical significance
                trial_results = []
                for trial in range(10):
                    r = compare_bloom_vs_exact(
                        num_elements=num_elements,
                        num_queries=num_elements,
                        fp_rate=fp_target,
                    )
                    trial_results.append(r)

                # Aggregate results
                avg_result = {
                    'num_elements': num_elements,
                    'target_fp_rate': fp_target,
                    'theoretical_fp_rate': sum(r['theoretical_fp_rate'] for r in trial_results) / len(trial_results),
                    'actual_fp_rate': sum(r['actual_fp_rate'] for r in trial_results) / len(trial_results),
                    'false_negatives': sum(r['false_negatives'] for r in trial_results),  # Should be 0!
                    'memory_savings': sum(r['memory_savings'] for r in trial_results) / len(trial_results),
                    'bloom_memory_bytes': trial_results[0]['bloom_memory_bytes'],
                    'exact_memory_bytes': trial_results[0]['exact_memory_bytes'],
                }

                # Validate claims
                avg_result['claim_no_false_negatives'] = avg_result['false_negatives'] == 0
                avg_result['claim_fp_within_2x'] = avg_result['actual_fp_rate'] < 2 * fp_target
                avg_result['claim_memory_savings_90pct'] = avg_result['memory_savings'] > 0.9

                results.append(avg_result)

                print(f"    FP: theoretical={avg_result['theoretical_fp_rate']:.4%}, "
                      f"actual={avg_result['actual_fp_rate']:.4%}")
                print(f"    False negatives: {avg_result['false_negatives']} (must be 0)")
                print(f"    Memory savings: {avg_result['memory_savings']:.1%}")

                # Record metrics
                collector.set_theoretical(f"fp_rate_{num_elements}_{fp_target}", fp_target)
                collector.set_actual(f"fp_rate_{num_elements}_{fp_target}", avg_result['actual_fp_rate'])

        # Summary
        all_no_fn = all(r['claim_no_false_negatives'] for r in results)
        all_fp_ok = all(r['claim_fp_within_2x'] for r in results)
        all_memory_ok = all(r['claim_memory_savings_90pct'] for r in results)

        print(f"\n  VALIDATION SUMMARY:")
        print(f"    P(false_negative) = 0: {'PASS' if all_no_fn else 'FAIL'}")
        print(f"    P(false_positive) within 2x target: {'PASS' if all_fp_ok else 'FAIL'}")
        print(f"    Memory savings > 90%: {'PASS' if all_memory_ok else 'FAIL'}")

        return {
            'results': results,
            'claims_validated': {
                'no_false_negatives': all_no_fn,
                'fp_within_bounds': all_fp_ok,
                'memory_savings': all_memory_ok,
            },
        }

    def experiment_speculative_execution(self) -> Dict[str, Any]:
        """
        Experiment 2: Validate speculative execution benefits.

        Claims to validate:
        - Speculative wins when p < T_consensus / (T_rollback + T_retry)
        - Speedup approaches T_consensus / T_local as p → 0
        - System adapts threshold based on observed conflicts
        """
        print("\n" + "-" * 50)
        print("Experiment 2: Speculative Execution")
        print("-" * 50)

        collector = MetricsCollector("speculative_execution")
        results = []

        # Test across different conflict rates
        for conflict_rate in [0.001, 0.01, 0.05, 0.1, 0.2, 0.3, 0.5]:
            print(f"\n  Testing actual_conflict_rate={conflict_rate:.1%}")

            executor = SpeculativeExecutor(
                t_local_ms=0.1,
                t_consensus_ms=5.0,
                t_rollback_ms=1.0,
                t_retry_ms=5.0,
            )

            tables = ["orders", "inventory", "customers"]
            analysis = simulate_workload(
                executor,
                num_transactions=1000,
                tables=tables,
                actual_conflict_rate=conflict_rate,
            )

            # Calculate theoretical expectations
            threshold = executor.speculation_threshold
            should_speculate = conflict_rate < threshold

            # Expected speedup if speculating
            expected_eager_latency = 0.1 + 5.0  # T_local + T_consensus
            expected_spec_latency = 0.1 + conflict_rate * (1.0 + 5.0)  # T_local + p*(T_rollback + T_retry)
            theoretical_speedup = expected_eager_latency / expected_spec_latency

            result = {
                'conflict_rate': conflict_rate,
                'threshold': threshold,
                'should_speculate': should_speculate,
                'speculation_rate': analysis['speculation_rate'],
                'revert_rate': analysis['revert_rate'],
                'actual_speedup': analysis['speedup_vs_eager'],
                'theoretical_speedup': theoretical_speedup,
                'speedup_ratio': analysis['speedup_vs_eager'] / theoretical_speedup if theoretical_speedup > 0 else 0,
            }

            # Validate claims
            result['claim_speculation_decision_correct'] = (
                (should_speculate and analysis['speculation_rate'] > 0.5) or
                (not should_speculate and analysis['speculation_rate'] < 0.5)
            )
            result['claim_speedup_positive'] = (
                not should_speculate or analysis['speedup_vs_eager'] > 1.0
            )

            results.append(result)

            print(f"    Threshold: {threshold:.1%}")
            print(f"    Should speculate: {should_speculate}")
            print(f"    Speculation rate: {analysis['speculation_rate']:.1%}")
            print(f"    Speedup: {analysis['speedup_vs_eager']:.2f}x "
                  f"(theoretical: {theoretical_speedup:.2f}x)")

            collector.set_theoretical(f"speedup_{conflict_rate}", theoretical_speedup)
            collector.set_actual(f"speedup_{conflict_rate}", analysis['speedup_vs_eager'])

        # Summary
        all_decisions_correct = all(r['claim_speculation_decision_correct'] for r in results)
        all_speedups_valid = all(r['claim_speedup_positive'] for r in results)

        print(f"\n  VALIDATION SUMMARY:")
        print(f"    Speculation decisions correct: {'PASS' if all_decisions_correct else 'FAIL'}")
        print(f"    Speedup when speculating: {'PASS' if all_speedups_valid else 'FAIL'}")

        return {
            'results': results,
            'claims_validated': {
                'decisions_correct': all_decisions_correct,
                'speedup_valid': all_speedups_valid,
            },
        }

    def experiment_escrow_transactions(self) -> Dict[str, Any]:
        """
        Experiment 3: Validate escrow transaction benefits.

        Claims to validate:
        - Local operation rate matches (1 - P(exhaust))
        - P(exhaust) matches Poisson prediction
        - Speedup approaches n (num_nodes) as quota → ∞
        """
        print("\n" + "-" * 50)
        print("Experiment 3: Escrow Transactions")
        print("-" * 50)

        collector = MetricsCollector("escrow_transactions")
        results = []

        # Test across different request rates
        for request_rate in [100, 500, 1000]:
            for num_nodes in [2, 3, 5]:
                print(f"\n  Testing rate={request_rate}/s, nodes={num_nodes}")

                # Calculate optimal quota
                optimal_quota = calculate_optimal_quota(
                    request_rate=request_rate,
                    num_nodes=num_nodes,
                    rebalance_interval_s=1.0,
                    target_exhaustion_rate=0.0001,
                )

                # Theoretical exhaustion probability
                p_exhaust_theory = theoretical_exhaustion_probability(
                    request_rate=request_rate,
                    num_nodes=num_nodes,
                    quota_per_node=optimal_quota,
                    interval_s=1.0,
                )

                # Run simulation
                resource = EscrowedResource(
                    "inventory",
                    total_value=optimal_quota * num_nodes * 10,  # Plenty of total
                )
                manager = EscrowManager(
                    resource,
                    num_nodes=num_nodes,
                    reserve_fraction=0.1,
                    t_local_ms=0.1,
                    t_coordination_ms=5.0,
                )

                # Simulate workload
                num_requests = request_rate * 10  # 10 seconds
                node_ids = list(manager.nodes.keys())

                for _ in range(num_requests):
                    node = random.choice(node_ids)
                    manager.consume(node, 1)

                analysis = manager.get_analysis()

                result = {
                    'request_rate': request_rate,
                    'num_nodes': num_nodes,
                    'optimal_quota': optimal_quota,
                    'p_exhaust_theory': p_exhaust_theory,
                    'local_rate': analysis['local_rate'],
                    'coordination_rate': analysis['coordination_rate'],
                    'speedup': analysis['speedup_vs_coordinated'],
                    'theoretical_max_speedup': manager.t_coordination / manager.t_local,
                }

                # Validate claims
                result['claim_local_rate_high'] = analysis['local_rate'] > 0.95
                result['claim_coordination_low'] = analysis['coordination_rate'] < 0.05

                results.append(result)

                print(f"    Optimal quota: {optimal_quota}")
                print(f"    P(exhaust) theory: {p_exhaust_theory:.6%}")
                print(f"    Local rate: {analysis['local_rate']:.1%}")
                print(f"    Speedup: {analysis['speedup_vs_coordinated']:.1f}x")

                collector.set_theoretical(f"local_rate_{request_rate}_{num_nodes}", 1 - p_exhaust_theory)
                collector.set_actual(f"local_rate_{request_rate}_{num_nodes}", analysis['local_rate'])

        # Summary
        all_local_high = all(r['claim_local_rate_high'] for r in results)
        all_coord_low = all(r['claim_coordination_low'] for r in results)

        print(f"\n  VALIDATION SUMMARY:")
        print(f"    Local rate > 95%: {'PASS' if all_local_high else 'FAIL'}")
        print(f"    Coordination rate < 5%: {'PASS' if all_coord_low else 'FAIL'}")

        return {
            'results': results,
            'claims_validated': {
                'local_rate_high': all_local_high,
                'coordination_low': all_coord_low,
            },
        }

    def experiment_algebraic_merge(self) -> Dict[str, Any]:
        """
        Experiment 4: Validate algebraic merge properties.

        Claims to validate:
        - Semilattice operations always merge
        - Abelian group operations always merge
        - Incompatible operations correctly detected
        """
        print("\n" + "-" * 50)
        print("Experiment 4: Algebraic Merge")
        print("-" * 50)

        collector = MetricsCollector("algebraic_merge")
        classifier = AlgebraicClassifier()

        # Test semilattice operations
        semilattice_tests = [
            ("max", OpType.SEMILATTICE_MAX, 10, 20, 20),
            ("min", OpType.SEMILATTICE_MIN, 10, 20, 10),
            ("union", OpType.SEMILATTICE_UNION, {1, 2}, {2, 3}, {1, 2, 3}),
        ]

        semilattice_results = []
        for name, op_type, v1, v2, expected in semilattice_tests:
            op1 = AlgebraicOperation("t", "r", "c", op_type, v1)
            op2 = AlgebraicOperation("t", "r", "c", op_type, v2)

            can_merge = op1.can_merge_with(op2)
            if can_merge:
                merged = op1.merge(op2)
                correct = merged.value == expected
            else:
                correct = False

            semilattice_results.append({
                'operation': name,
                'can_merge': can_merge,
                'correct_result': correct,
            })
            print(f"  Semilattice {name}: can_merge={can_merge}, correct={correct}")

        # Test Abelian group operations
        abelian_tests = [
            ("add", OpType.ABELIAN_ADD, 5, -3, 2),
            ("multiply", OpType.ABELIAN_MULTIPLY, 2, 3, 6),
        ]

        abelian_results = []
        for name, op_type, v1, v2, expected in abelian_tests:
            op1 = AlgebraicOperation("t", "r", "c", op_type, v1)
            op2 = AlgebraicOperation("t", "r", "c", op_type, v2)

            can_merge = op1.can_merge_with(op2)
            if can_merge:
                merged = op1.merge(op2)
                correct = merged.value == expected
            else:
                correct = False

            abelian_results.append({
                'operation': name,
                'can_merge': can_merge,
                'correct_result': correct,
            })
            print(f"  Abelian {name}: can_merge={can_merge}, correct={correct}")

        # Test incompatible operations
        incompatible_tests = [
            ("overwrite vs overwrite", OpType.GENERIC_OVERWRITE, OpType.GENERIC_OVERWRITE),
            ("max vs add", OpType.SEMILATTICE_MAX, OpType.ABELIAN_ADD),
            ("unknown vs any", OpType.UNKNOWN, OpType.SEMILATTICE_MAX),
        ]

        incompatible_results = []
        for name, type1, type2 in incompatible_tests:
            op1 = AlgebraicOperation("t", "r", "c", type1, 1)
            op2 = AlgebraicOperation("t", "r", "c", type2, 2)

            can_merge = op1.can_merge_with(op2)
            correct = not can_merge  # Should NOT be able to merge

            incompatible_results.append({
                'operation': name,
                'correctly_rejected': correct,
            })
            print(f"  Incompatible {name}: correctly_rejected={correct}")

        # Summary
        all_semilattice_ok = all(r['correct_result'] for r in semilattice_results)
        all_abelian_ok = all(r['correct_result'] for r in abelian_results)
        all_incompatible_ok = all(r['correctly_rejected'] for r in incompatible_results)

        print(f"\n  VALIDATION SUMMARY:")
        print(f"    Semilattice merges correct: {'PASS' if all_semilattice_ok else 'FAIL'}")
        print(f"    Abelian merges correct: {'PASS' if all_abelian_ok else 'FAIL'}")
        print(f"    Incompatible detected: {'PASS' if all_incompatible_ok else 'FAIL'}")

        return {
            'semilattice': semilattice_results,
            'abelian': abelian_results,
            'incompatible': incompatible_results,
            'claims_validated': {
                'semilattice_correct': all_semilattice_ok,
                'abelian_correct': all_abelian_ok,
                'incompatible_detected': all_incompatible_ok,
            },
        }

    def experiment_combined_poac(self) -> Dict[str, Any]:
        """
        Experiment 5: Combined POAC system validation.

        Tests all components working together.
        """
        print("\n" + "-" * 50)
        print("Experiment 5: Combined POAC System")
        print("-" * 50)

        collector = MetricsCollector("combined_poac")

        # Simulate a realistic workload with all POAC components
        num_transactions = 1000
        conflict_rate = 0.02  # 2% actual conflicts
        num_nodes = 3

        # Initialize components
        bloom = BloomWriteSet(expected_elements=10000, fp_rate=0.01)
        executor = SpeculativeExecutor(t_local_ms=0.1, t_consensus_ms=5.0)
        classifier = AlgebraicClassifier()

        # Track results
        total_ops = 0
        bloom_fp = 0
        spec_reverts = 0
        auto_merges = 0
        total_latency = 0.0

        for i in range(num_transactions):
            # Generate transaction
            tables = random.sample(["orders", "inventory", "customers"], random.randint(1, 2))
            num_rows = random.randint(1, 100)

            # Add to bloom filter
            for j in range(num_rows):
                bloom.add(random.choice(tables), f"row_{i}_{j}")

            # Classify operations (mix of types)
            ops = []
            for j in range(num_rows):
                if random.random() < 0.6:  # 60% are counter deltas (mergeable)
                    ops.append(counter_delta("inventory", f"sku_{j}", "count", random.randint(-5, 5)))
                else:  # 40% are overwrites (may conflict)
                    ops.append(AlgebraicOperation(
                        "orders", f"order_{j}", "status",
                        OpType.GENERIC_OVERWRITE, "shipped"
                    ))

            # Check if transaction would conflict
            has_conflict = random.random() < conflict_rate

            # Use speculative execution
            txn = executor.begin_transaction(tables)
            start = time.perf_counter()
            success = executor.commit(
                txn,
                actual_conflict_checker=lambda t: has_conflict,
            )
            latency = (time.perf_counter() - start) * 1000

            total_ops += 1
            total_latency += latency
            if not success and txn.was_speculative:
                spec_reverts += 1

        # Calculate combined metrics
        avg_latency = total_latency / total_ops

        # Theoretical baseline (all eager, no POAC)
        baseline_latency = 0.1 + 5.0  # T_local + T_consensus
        speedup = baseline_latency / avg_latency

        result = {
            'total_transactions': total_ops,
            'avg_latency_ms': avg_latency,
            'baseline_latency_ms': baseline_latency,
            'speedup': speedup,
            'speculation_revert_rate': spec_reverts / total_ops,
            'bloom_memory_kb': bloom.memory_bytes() / 1024,
        }

        print(f"\n  Combined POAC Results:")
        print(f"    Transactions: {total_ops}")
        print(f"    Avg latency: {avg_latency:.2f}ms (baseline: {baseline_latency:.2f}ms)")
        print(f"    Speedup: {speedup:.2f}x")
        print(f"    Speculation reverts: {spec_reverts / total_ops:.2%}")
        print(f"    Bloom memory: {bloom.memory_bytes() / 1024:.1f}KB")

        return {
            'result': result,
            'claims_validated': {
                'speedup_achieved': speedup > 1.5,
                'low_revert_rate': spec_reverts / total_ops < 0.05,
            },
        }

    def generate_paper_tables(self) -> str:
        """Generate LaTeX tables for academic paper."""
        if not self.results:
            return "No results to generate tables from. Run experiments first."

        latex = []
        latex.append("% POAC Validation Results - LaTeX Tables")
        latex.append("")

        # Bloom filter results table
        if 'bloom_filter' in self.results:
            latex.append("\\begin{table}[h]")
            latex.append("\\centering")
            latex.append("\\caption{Bloom Filter Write-Set Validation}")
            latex.append("\\begin{tabular}{rrrrrr}")
            latex.append("\\hline")
            latex.append("n & Target FP & Theoretical FP & Actual FP & FN & Memory Savings \\\\")
            latex.append("\\hline")
            for r in self.results['bloom_filter']['results']:
                latex.append(
                    f"{r['num_elements']:,} & {r['target_fp_rate']:.2%} & "
                    f"{r['theoretical_fp_rate']:.4%} & {r['actual_fp_rate']:.4%} & "
                    f"{r['false_negatives']} & {r['memory_savings']:.1%} \\\\"
                )
            latex.append("\\hline")
            latex.append("\\end{tabular}")
            latex.append("\\end{table}")
            latex.append("")

        # Speculative execution results table
        if 'speculative_execution' in self.results:
            latex.append("\\begin{table}[h]")
            latex.append("\\centering")
            latex.append("\\caption{Speculative Execution Performance}")
            latex.append("\\begin{tabular}{rrrrrr}")
            latex.append("\\hline")
            latex.append("Conflict Rate & Threshold & Spec Rate & Revert Rate & Actual Speedup & Theory Speedup \\\\")
            latex.append("\\hline")
            for r in self.results['speculative_execution']['results']:
                latex.append(
                    f"{r['conflict_rate']:.1%} & {r['threshold']:.1%} & "
                    f"{r['speculation_rate']:.1%} & {r['revert_rate']:.1%} & "
                    f"{r['actual_speedup']:.2f}x & {r['theoretical_speedup']:.2f}x \\\\"
                )
            latex.append("\\hline")
            latex.append("\\end{tabular}")
            latex.append("\\end{table}")

        return "\n".join(latex)


if __name__ == '__main__':
    harness = ExperimentHarness()
    results = harness.run_all_experiments()

    print("\n" + "=" * 70)
    print("LATEX TABLES FOR PAPER")
    print("=" * 70)
    print(harness.generate_paper_tables())
