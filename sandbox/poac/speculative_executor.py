"""
Speculative Execution with Adaptive Consistency

Mathematical Foundation:
- Eager: T_commit = T_local + T_consensus (always pays consensus tax)
- Speculative: E[T_commit] = T_local + p*(T_rollback + T_retry)

Break-even: Speculative wins when p < T_consensus / (T_rollback + T_retry)
For typical values: p < 50% means speculative wins

Key insight: Most transactions don't conflict, so why pay upfront?
"""

from __future__ import annotations
import random
import time
import threading
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Callable, Any
from enum import Enum
from collections import deque
import math


class TxnState(Enum):
    PENDING = "pending"
    COMMITTED_LOCAL = "committed_local"
    CONFIRMED_GLOBAL = "confirmed_global"
    REVERTED = "reverted"
    ABORTED = "aborted"


@dataclass
class Transaction:
    """Represents a transaction in the speculative executor."""
    txn_id: int
    tables: List[str]
    operations: List[Any]
    state: TxnState = TxnState.PENDING
    start_time: float = field(default_factory=time.perf_counter)
    commit_time: Optional[float] = None
    confirm_time: Optional[float] = None
    was_speculative: bool = False


@dataclass
class SpecMetrics:
    """Metrics for speculative execution analysis."""
    total_transactions: int = 0
    speculative_commits: int = 0
    eager_commits: int = 0
    successful_speculations: int = 0
    reverted_speculations: int = 0

    # Latency tracking
    speculative_latencies_ms: List[float] = field(default_factory=list)
    eager_latencies_ms: List[float] = field(default_factory=list)
    revert_latencies_ms: List[float] = field(default_factory=list)

    # Conflict tracking per table
    conflicts_per_table: Dict[str, int] = field(default_factory=dict)
    transactions_per_table: Dict[str, int] = field(default_factory=dict)

    @property
    def speculation_rate(self) -> float:
        if self.total_transactions == 0:
            return 0.0
        return self.speculative_commits / self.total_transactions

    @property
    def revert_rate(self) -> float:
        if self.speculative_commits == 0:
            return 0.0
        return self.reverted_speculations / self.speculative_commits

    @property
    def avg_speculative_latency_ms(self) -> float:
        if not self.speculative_latencies_ms:
            return 0.0
        return sum(self.speculative_latencies_ms) / len(self.speculative_latencies_ms)

    @property
    def avg_eager_latency_ms(self) -> float:
        if not self.eager_latencies_ms:
            return 0.0
        return sum(self.eager_latencies_ms) / len(self.eager_latencies_ms)

    def conflict_rate(self, table: str) -> float:
        if table not in self.transactions_per_table:
            return 0.0
        if self.transactions_per_table[table] == 0:
            return 0.0
        return self.conflicts_per_table.get(table, 0) / self.transactions_per_table[table]


class ConflictEstimator:
    """
    Estimates conflict probability using exponential moving average.

    Uses Bayesian-inspired approach with decay for non-stationarity.
    """

    def __init__(self, alpha: float = 0.1, prior: float = 0.01):
        """
        Args:
            alpha: Learning rate for exponential moving average
            prior: Initial conflict probability estimate
        """
        self.alpha = alpha
        self.estimates: Dict[str, float] = {}  # Per-table estimates
        self.global_estimate = prior
        self.prior = prior

    def estimate(self, tables: List[str]) -> float:
        """
        Estimate conflict probability for a transaction touching given tables.

        P(conflict) = 1 - Product((1 - p_table) for table in tables)
        """
        if not tables:
            return self.global_estimate

        # Probability of NO conflict across all tables
        p_no_conflict = 1.0
        for table in tables:
            p_table = self.estimates.get(table, self.prior)
            p_no_conflict *= (1 - p_table)

        return 1 - p_no_conflict

    def update(self, tables: List[str], conflicted: bool):
        """Update estimates based on observed outcome."""
        outcome = 1.0 if conflicted else 0.0

        for table in tables:
            current = self.estimates.get(table, self.prior)
            # Exponential moving average
            self.estimates[table] = current + self.alpha * (outcome - current)

        # Update global estimate
        self.global_estimate = self.global_estimate + self.alpha * (outcome - self.global_estimate)


class SpeculativeExecutor:
    """
    Adaptive speculative execution engine.

    Decides whether to speculate based on:
    1. Estimated conflict probability
    2. Relative costs of speculation vs eager consensus
    3. Historical performance

    Mathematical model:
        E[T_speculative] = T_local + p * (T_rollback + T_retry)
        E[T_eager] = T_local + T_consensus

        Speculate when E[T_speculative] < E[T_eager]
        i.e., when p < T_consensus / (T_rollback + T_retry)
    """

    def __init__(
        self,
        t_local_ms: float = 0.1,
        t_consensus_ms: float = 5.0,
        t_rollback_ms: float = 1.0,
        t_retry_ms: float = 5.0,
        threshold_override: Optional[float] = None,
    ):
        """
        Args:
            t_local_ms: Time for local commit
            t_consensus_ms: Time for distributed consensus
            t_rollback_ms: Time to rollback a speculative commit
            t_retry_ms: Time to retry after rollback
            threshold_override: Override calculated threshold (for experiments)
        """
        self.t_local = t_local_ms
        self.t_consensus = t_consensus_ms
        self.t_rollback = t_rollback_ms
        self.t_retry = t_retry_ms

        # Calculate break-even threshold
        if threshold_override is not None:
            self.speculation_threshold = threshold_override
        else:
            self.speculation_threshold = t_consensus_ms / (t_rollback_ms + t_retry_ms)

        self.estimator = ConflictEstimator()
        self.metrics = SpecMetrics()

        # State tracking
        self._txn_counter = 0
        self._pending_confirmations: Dict[int, Transaction] = {}
        self._committed_writes: Dict[str, List[int]] = {}  # table -> [txn_ids]
        self._lock = threading.Lock()

    def should_speculate(self, tables: List[str]) -> bool:
        """
        Decide whether to use speculative execution.

        Returns True if expected speculative cost < expected eager cost.
        """
        p_conflict = self.estimator.estimate(tables)
        return p_conflict < self.speculation_threshold

    def expected_latency(self, tables: List[str], speculative: bool) -> float:
        """Calculate expected latency for a commit strategy."""
        p = self.estimator.estimate(tables)
        if speculative:
            return self.t_local + p * (self.t_rollback + self.t_retry)
        else:
            return self.t_local + self.t_consensus

    def begin_transaction(self, tables: List[str]) -> Transaction:
        """Begin a new transaction."""
        with self._lock:
            self._txn_counter += 1
            txn = Transaction(
                txn_id=self._txn_counter,
                tables=tables,
                operations=[],
            )
            return txn

    def commit(
        self,
        txn: Transaction,
        force_speculative: Optional[bool] = None,
        actual_conflict_checker: Optional[Callable[[Transaction], bool]] = None,
    ) -> bool:
        """
        Commit a transaction, choosing speculation adaptively.

        Args:
            txn: The transaction to commit
            force_speculative: Override adaptive decision (for experiments)
            actual_conflict_checker: Function to check for actual conflicts

        Returns:
            True if committed successfully, False if aborted/reverted
        """
        # Decide strategy
        if force_speculative is not None:
            speculative = force_speculative
        else:
            speculative = self.should_speculate(txn.tables)

        txn.was_speculative = speculative

        # Track metrics
        self.metrics.total_transactions += 1
        for table in txn.tables:
            self.metrics.transactions_per_table[table] = \
                self.metrics.transactions_per_table.get(table, 0) + 1

        if speculative:
            return self._commit_speculative(txn, actual_conflict_checker)
        else:
            return self._commit_eager(txn, actual_conflict_checker)

    def _commit_speculative(
        self,
        txn: Transaction,
        conflict_checker: Optional[Callable[[Transaction], bool]],
    ) -> bool:
        """Commit speculatively: local first, confirm async."""
        start = time.perf_counter()

        # Simulate local commit (fast)
        time.sleep(self.t_local / 1000)
        txn.state = TxnState.COMMITTED_LOCAL
        txn.commit_time = time.perf_counter()

        local_latency = (txn.commit_time - start) * 1000
        self.metrics.speculative_commits += 1

        # Check for conflicts (simulating async confirmation)
        has_conflict = False
        if conflict_checker:
            has_conflict = conflict_checker(txn)

        if has_conflict:
            # Rollback needed
            time.sleep(self.t_rollback / 1000)
            txn.state = TxnState.REVERTED

            self.metrics.reverted_speculations += 1
            for table in txn.tables:
                self.metrics.conflicts_per_table[table] = \
                    self.metrics.conflicts_per_table.get(table, 0) + 1

            total_latency = (time.perf_counter() - start) * 1000
            self.metrics.revert_latencies_ms.append(total_latency)

            # Update estimator
            self.estimator.update(txn.tables, conflicted=True)
            return False
        else:
            # Confirm (simulate async consensus)
            time.sleep(self.t_consensus / 1000)  # Background confirmation
            txn.state = TxnState.CONFIRMED_GLOBAL
            txn.confirm_time = time.perf_counter()

            self.metrics.successful_speculations += 1
            self.metrics.speculative_latencies_ms.append(local_latency)

            # Update estimator
            self.estimator.update(txn.tables, conflicted=False)
            return True

    def _commit_eager(
        self,
        txn: Transaction,
        conflict_checker: Optional[Callable[[Transaction], bool]],
    ) -> bool:
        """Commit eagerly: full consensus before returning."""
        start = time.perf_counter()

        # Simulate local + consensus
        time.sleep((self.t_local + self.t_consensus) / 1000)

        # Check for conflicts
        has_conflict = False
        if conflict_checker:
            has_conflict = conflict_checker(txn)

        if has_conflict:
            txn.state = TxnState.ABORTED
            for table in txn.tables:
                self.metrics.conflicts_per_table[table] = \
                    self.metrics.conflicts_per_table.get(table, 0) + 1
            self.estimator.update(txn.tables, conflicted=True)
            return False
        else:
            txn.state = TxnState.CONFIRMED_GLOBAL
            txn.commit_time = time.perf_counter()
            txn.confirm_time = txn.commit_time

            latency = (txn.commit_time - start) * 1000
            self.metrics.eager_commits += 1
            self.metrics.eager_latencies_ms.append(latency)

            self.estimator.update(txn.tables, conflicted=False)
            return True

    def get_analysis(self) -> dict:
        """Get detailed analysis of speculation performance."""
        m = self.metrics

        # Calculate what-if scenarios
        all_latencies = m.speculative_latencies_ms + m.eager_latencies_ms + m.revert_latencies_ms

        # If all were eager
        eager_only_latency = self.t_local + self.t_consensus
        total_eager_time = m.total_transactions * eager_only_latency

        # Actual total time
        actual_total = (
            sum(m.speculative_latencies_ms) +
            sum(m.eager_latencies_ms) +
            sum(m.revert_latencies_ms)
        )

        return {
            'total_transactions': m.total_transactions,
            'speculation_rate': m.speculation_rate,
            'revert_rate': m.revert_rate,
            'avg_speculative_latency_ms': m.avg_speculative_latency_ms,
            'avg_eager_latency_ms': m.avg_eager_latency_ms,
            'speculation_threshold': self.speculation_threshold,
            'estimated_conflict_rates': dict(self.estimator.estimates),
            'time_saved_vs_all_eager_ms': total_eager_time - actual_total,
            'speedup_vs_eager': total_eager_time / actual_total if actual_total > 0 else 0,
        }


def simulate_workload(
    executor: SpeculativeExecutor,
    num_transactions: int,
    tables: List[str],
    actual_conflict_rate: float,
    txn_per_table_dist: Optional[Dict[str, float]] = None,
) -> dict:
    """
    Simulate a workload to validate speculative execution.

    Args:
        executor: The speculative executor
        num_transactions: Number of transactions to run
        tables: Available tables
        actual_conflict_rate: True underlying conflict rate
        txn_per_table_dist: Distribution of transactions across tables

    Returns:
        Analysis of simulation results
    """
    import random

    # Track which transactions committed to detect conflicts
    committed_txns: List[Transaction] = []

    def conflict_checker(txn: Transaction) -> bool:
        """Check for conflicts based on actual_conflict_rate."""
        # Simulate conflict based on rate
        return random.random() < actual_conflict_rate

    for i in range(num_transactions):
        # Pick tables for this transaction
        if txn_per_table_dist:
            selected_tables = [
                t for t in tables
                if random.random() < txn_per_table_dist.get(t, 0.5)
            ]
            if not selected_tables:
                selected_tables = [random.choice(tables)]
        else:
            num_tables = random.randint(1, min(3, len(tables)))
            selected_tables = random.sample(tables, num_tables)

        txn = executor.begin_transaction(selected_tables)
        executor.commit(txn, actual_conflict_checker=conflict_checker)

    return executor.get_analysis()


if __name__ == '__main__':
    print("Speculative Execution Simulation")
    print("=" * 50)

    for conflict_rate in [0.001, 0.01, 0.05, 0.1, 0.2, 0.5]:
        executor = SpeculativeExecutor(
            t_local_ms=0.1,
            t_consensus_ms=5.0,
            t_rollback_ms=1.0,
            t_retry_ms=5.0,
        )

        tables = ["orders", "inventory", "customers", "audit"]
        results = simulate_workload(
            executor,
            num_transactions=1000,
            tables=tables,
            actual_conflict_rate=conflict_rate,
        )

        print(f"\nActual conflict rate: {conflict_rate:.1%}")
        print(f"  Threshold: {results['speculation_threshold']:.1%}")
        print(f"  Speculation rate: {results['speculation_rate']:.1%}")
        print(f"  Revert rate: {results['revert_rate']:.1%}")
        print(f"  Speedup vs eager: {results['speedup_vs_eager']:.2f}x")
