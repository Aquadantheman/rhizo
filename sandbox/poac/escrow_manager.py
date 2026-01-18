"""
Escrow Transactions for Hot-Spot Mitigation

Mathematical Foundation:
- Without escrow: All operations on hot row serialize
  Throughput = 1 / T_transaction

- With escrow: Operations are local until quota exhausted
  Throughput = n * (1 / T_local) where n = nodes

  P(need_coordination) = P(local_requests > quota)
                       = P(Poisson(λT/n) > q)

For λ=1000/s, n=3, T=1s, q=500:
  E[requests/node/second] = 333
  P(exhaust) ≈ 0.0001 (negligible!)
"""

from __future__ import annotations
import random
import time
import threading
import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from enum import Enum

# Try to import scipy, fall back to manual implementation
try:
    from scipy import stats as scipy_stats
    HAS_SCIPY = True
except ImportError:
    HAS_SCIPY = False


class EscrowResult(Enum):
    SUCCESS_LOCAL = "success_local"  # Used local quota, no coordination
    SUCCESS_COORDINATED = "success_coordinated"  # Needed to coordinate
    FAILED_INSUFFICIENT = "failed_insufficient"  # No quota available anywhere


@dataclass
class EscrowMetrics:
    """Metrics for escrow performance analysis."""
    total_operations: int = 0
    local_operations: int = 0
    coordinated_operations: int = 0
    failed_operations: int = 0

    rebalance_count: int = 0
    quota_exhaustions: int = 0

    # Latency tracking
    local_latencies_ms: List[float] = field(default_factory=list)
    coordinated_latencies_ms: List[float] = field(default_factory=list)

    @property
    def local_rate(self) -> float:
        if self.total_operations == 0:
            return 0.0
        return self.local_operations / self.total_operations

    @property
    def coordination_rate(self) -> float:
        if self.total_operations == 0:
            return 0.0
        return self.coordinated_operations / self.total_operations

    @property
    def avg_local_latency_ms(self) -> float:
        if not self.local_latencies_ms:
            return 0.0
        return sum(self.local_latencies_ms) / len(self.local_latencies_ms)

    @property
    def avg_coordinated_latency_ms(self) -> float:
        if not self.coordinated_latencies_ms:
            return 0.0
        return sum(self.coordinated_latencies_ms) / len(self.coordinated_latencies_ms)


@dataclass
class EscrowedResource:
    """A resource managed via escrow (e.g., inventory count)."""
    name: str
    total_value: int
    min_value: int = 0  # Can't go below this (e.g., inventory can't be negative)


class EscrowNode:
    """Represents a node with local escrow quota."""

    def __init__(self, node_id: str, quota: int):
        self.node_id = node_id
        self.quota = quota
        self.initial_quota = quota
        self.used = 0
        self._lock = threading.Lock()

    @property
    def available(self) -> int:
        return self.quota - self.used

    def try_consume(self, amount: int) -> bool:
        """Try to consume from local quota without coordination."""
        with self._lock:
            if self.available >= amount:
                self.used += amount
                return True
            return False

    def return_quota(self, amount: int):
        """Return quota (e.g., after rollback)."""
        with self._lock:
            self.used = max(0, self.used - amount)

    def add_quota(self, amount: int):
        """Add quota from rebalancing."""
        with self._lock:
            self.quota += amount

    def __repr__(self) -> str:
        return f"EscrowNode({self.node_id}, quota={self.quota}, used={self.used})"


class EscrowManager:
    """
    Manages escrowed resources across multiple nodes.

    Key insight: Pre-allocate quota so nodes can operate independently.
    Only coordinate when quota exhausted (rare event).

    Mathematical model:
        λ = total request rate
        n = number of nodes
        q = quota per node
        T = rebalance interval

        P(exhaust) = P(Poisson(λT/n) > q)

        With proper q sizing, P(exhaust) < 0.01%
    """

    def __init__(
        self,
        resource: EscrowedResource,
        num_nodes: int,
        reserve_fraction: float = 0.1,
        t_local_ms: float = 0.1,
        t_coordination_ms: float = 5.0,
    ):
        """
        Initialize escrow manager.

        Args:
            resource: The resource to manage
            num_nodes: Number of nodes to distribute quota to
            reserve_fraction: Fraction of total to keep in reserve
            t_local_ms: Latency for local operations
            t_coordination_ms: Latency for coordinated operations
        """
        self.resource = resource
        self.t_local = t_local_ms
        self.t_coordination = t_coordination_ms

        # Calculate initial quota distribution
        available = resource.total_value - resource.min_value
        reserve = int(available * reserve_fraction)
        per_node = (available - reserve) // num_nodes

        self.reserve = reserve
        self.nodes: Dict[str, EscrowNode] = {
            f"node_{i}": EscrowNode(f"node_{i}", per_node)
            for i in range(num_nodes)
        }

        self.metrics = EscrowMetrics()
        self._lock = threading.Lock()

    def consume(
        self,
        node_id: str,
        amount: int,
        allow_coordination: bool = True,
    ) -> Tuple[EscrowResult, float]:
        """
        Consume resource from a node.

        Args:
            node_id: The node making the request
            amount: Amount to consume
            allow_coordination: Whether to try coordination if local fails

        Returns:
            (result, latency_ms)
        """
        start = time.perf_counter()
        self.metrics.total_operations += 1

        node = self.nodes.get(node_id)
        if node is None:
            return EscrowResult.FAILED_INSUFFICIENT, 0.0

        # Try local first
        if node.try_consume(amount):
            time.sleep(self.t_local / 1000)
            latency = (time.perf_counter() - start) * 1000
            self.metrics.local_operations += 1
            self.metrics.local_latencies_ms.append(latency)
            return EscrowResult.SUCCESS_LOCAL, latency

        # Local quota exhausted
        self.metrics.quota_exhaustions += 1

        if not allow_coordination:
            self.metrics.failed_operations += 1
            return EscrowResult.FAILED_INSUFFICIENT, 0.0

        # Try to get from reserve or other nodes
        with self._lock:
            time.sleep(self.t_coordination / 1000)  # Coordination latency

            # Try reserve first
            if self.reserve >= amount:
                self.reserve -= amount
                node.add_quota(amount)
                node.try_consume(amount)
                self.metrics.rebalance_count += 1
                latency = (time.perf_counter() - start) * 1000
                self.metrics.coordinated_operations += 1
                self.metrics.coordinated_latencies_ms.append(latency)
                return EscrowResult.SUCCESS_COORDINATED, latency

            # Try stealing from other nodes
            for other_id, other_node in self.nodes.items():
                if other_id != node_id and other_node.available >= amount * 2:
                    # Steal half of other's available
                    steal = other_node.available // 2
                    other_node.quota -= steal
                    node.add_quota(steal)
                    if node.try_consume(amount):
                        self.metrics.rebalance_count += 1
                        latency = (time.perf_counter() - start) * 1000
                        self.metrics.coordinated_operations += 1
                        self.metrics.coordinated_latencies_ms.append(latency)
                        return EscrowResult.SUCCESS_COORDINATED, latency

        self.metrics.failed_operations += 1
        return EscrowResult.FAILED_INSUFFICIENT, (time.perf_counter() - start) * 1000

    def get_status(self) -> dict:
        """Get current escrow status."""
        return {
            'resource': self.resource.name,
            'total': self.resource.total_value,
            'reserve': self.reserve,
            'nodes': {
                node_id: {
                    'quota': node.quota,
                    'used': node.used,
                    'available': node.available,
                }
                for node_id, node in self.nodes.items()
            },
            'total_distributed': sum(n.quota for n in self.nodes.values()),
            'total_used': sum(n.used for n in self.nodes.values()),
        }

    def get_analysis(self) -> dict:
        """Get performance analysis."""
        m = self.metrics

        # Calculate what-if all operations were coordinated
        all_coordinated_time = m.total_operations * self.t_coordination
        actual_time = (
            m.local_operations * self.t_local +
            m.coordinated_operations * self.t_coordination
        )

        return {
            'total_operations': m.total_operations,
            'local_rate': m.local_rate,
            'coordination_rate': m.coordination_rate,
            'avg_local_latency_ms': m.avg_local_latency_ms,
            'avg_coordinated_latency_ms': m.avg_coordinated_latency_ms,
            'rebalance_count': m.rebalance_count,
            'quota_exhaustions': m.quota_exhaustions,
            'time_saved_vs_all_coordinated_ms': all_coordinated_time - actual_time,
            'speedup_vs_coordinated': all_coordinated_time / actual_time if actual_time > 0 else 0,
        }


def _poisson_cdf(k: int, lam: float) -> float:
    """Calculate Poisson CDF manually: P(X <= k)."""
    if lam <= 0:
        return 1.0 if k >= 0 else 0.0
    total = 0.0
    term = math.exp(-lam)
    for i in range(k + 1):
        total += term
        term *= lam / (i + 1)
    return min(1.0, total)


def _poisson_ppf(p: float, lam: float) -> int:
    """Calculate Poisson PPF (inverse CDF) manually."""
    if p <= 0:
        return 0
    if p >= 1:
        return int(lam * 10)  # Reasonable upper bound

    k = 0
    while _poisson_cdf(k, lam) < p:
        k += 1
    return k


def calculate_optimal_quota(
    request_rate: float,
    num_nodes: int,
    rebalance_interval_s: float,
    target_exhaustion_rate: float = 0.0001,
) -> int:
    """
    Calculate optimal quota per node to achieve target exhaustion rate.

    Uses inverse Poisson CDF:
        P(X > q) = target_exhaustion_rate
        q = Poisson.ppf(1 - target_exhaustion_rate, λ*T/n)

    Args:
        request_rate: Total requests per second (λ)
        num_nodes: Number of nodes (n)
        rebalance_interval_s: Time between rebalances (T)
        target_exhaustion_rate: Desired P(exhaust quota)

    Returns:
        Optimal quota per node
    """
    # Expected requests per node per interval
    lambda_per_node = (request_rate * rebalance_interval_s) / num_nodes

    # Find q such that P(Poisson(λ) > q) = target_exhaustion_rate
    if HAS_SCIPY:
        q = scipy_stats.poisson.ppf(1 - target_exhaustion_rate, lambda_per_node)
    else:
        q = _poisson_ppf(1 - target_exhaustion_rate, lambda_per_node)

    # Add safety margin
    return int(q * 1.2)


def simulate_hot_spot(
    total_value: int,
    num_nodes: int,
    request_rate: float,
    duration_s: float,
    with_escrow: bool,
) -> dict:
    """
    Simulate a hot-spot workload with and without escrow.

    Returns performance comparison.
    """
    num_requests = int(request_rate * duration_s)

    if with_escrow:
        resource = EscrowedResource("inventory", total_value, min_value=0)
        manager = EscrowManager(
            resource,
            num_nodes=num_nodes,
            reserve_fraction=0.1,
            t_local_ms=0.1,
            t_coordination_ms=5.0,
        )

        # Distribute requests across nodes
        node_ids = list(manager.nodes.keys())
        latencies = []

        for _ in range(num_requests):
            node = random.choice(node_ids)
            result, latency = manager.consume(node, 1)
            latencies.append(latency)

        analysis = manager.get_analysis()
        analysis['latencies'] = latencies
        analysis['p50_latency_ms'] = sorted(latencies)[len(latencies) // 2]
        analysis['p99_latency_ms'] = sorted(latencies)[int(len(latencies) * 0.99)]
        return analysis

    else:
        # Without escrow: all operations coordinate
        t_coordination = 5.0
        latencies = []

        for _ in range(num_requests):
            time.sleep(t_coordination / 1000)
            latencies.append(t_coordination)

        return {
            'total_operations': num_requests,
            'local_rate': 0.0,
            'coordination_rate': 1.0,
            'avg_local_latency_ms': 0.0,
            'avg_coordinated_latency_ms': t_coordination,
            'latencies': latencies,
            'p50_latency_ms': t_coordination,
            'p99_latency_ms': t_coordination,
            'speedup_vs_coordinated': 1.0,
        }


def theoretical_exhaustion_probability(
    request_rate: float,
    num_nodes: int,
    quota_per_node: int,
    interval_s: float = 1.0,
) -> float:
    """
    Calculate theoretical probability of quota exhaustion.

    P(exhaust) = P(Poisson(λT/n) > q)
    """
    lambda_per_node = (request_rate * interval_s) / num_nodes
    if HAS_SCIPY:
        return 1 - scipy_stats.poisson.cdf(quota_per_node, lambda_per_node)
    else:
        return 1 - _poisson_cdf(quota_per_node, lambda_per_node)


if __name__ == '__main__':
    print("Escrow Transaction Simulation")
    print("=" * 50)

    # Test optimal quota calculation
    print("\n1. Optimal Quota Calculation")
    for rate in [100, 1000, 10000]:
        q = calculate_optimal_quota(
            request_rate=rate,
            num_nodes=3,
            rebalance_interval_s=1.0,
            target_exhaustion_rate=0.0001,
        )
        p_exhaust = theoretical_exhaustion_probability(rate, 3, q)
        print(f"  Rate={rate}/s, Optimal quota={q}, P(exhaust)={p_exhaust:.6%}")

    # Simulate with vs without escrow
    print("\n2. Hot-Spot Simulation (1000 requests)")

    with_escrow = simulate_hot_spot(
        total_value=10000,
        num_nodes=3,
        request_rate=100,
        duration_s=10,
        with_escrow=True,
    )

    print(f"  With Escrow:")
    print(f"    Local rate: {with_escrow['local_rate']:.1%}")
    print(f"    P50 latency: {with_escrow['p50_latency_ms']:.2f}ms")
    print(f"    P99 latency: {with_escrow['p99_latency_ms']:.2f}ms")
    print(f"    Speedup: {with_escrow['speedup_vs_coordinated']:.1f}x")

    print(f"\n  Without Escrow (all coordinated):")
    print(f"    All latencies: 5.0ms (coordination cost)")
