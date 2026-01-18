"""
Metrics Collection for POAC Experiments

Provides unified metrics collection, statistical analysis,
and visualization for validating POAC mathematical claims.
"""

from __future__ import annotations
import time
import statistics
import json
import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any, Tuple
from collections import defaultdict

# Try to import scipy for statistical tests
try:
    from scipy import stats as scipy_stats
    HAS_SCIPY = True
except ImportError:
    HAS_SCIPY = False


@dataclass
class LatencyMetrics:
    """Latency statistics for an operation type."""
    samples: List[float] = field(default_factory=list)

    def add(self, latency_ms: float):
        self.samples.append(latency_ms)

    @property
    def count(self) -> int:
        return len(self.samples)

    @property
    def mean(self) -> float:
        return statistics.mean(self.samples) if self.samples else 0.0

    @property
    def median(self) -> float:
        return statistics.median(self.samples) if self.samples else 0.0

    @property
    def stddev(self) -> float:
        return statistics.stdev(self.samples) if len(self.samples) > 1 else 0.0

    @property
    def p50(self) -> float:
        return self._percentile(50)

    @property
    def p90(self) -> float:
        return self._percentile(90)

    @property
    def p99(self) -> float:
        return self._percentile(99)

    @property
    def min(self) -> float:
        return min(self.samples) if self.samples else 0.0

    @property
    def max(self) -> float:
        return max(self.samples) if self.samples else 0.0

    def _percentile(self, p: float) -> float:
        if not self.samples:
            return 0.0
        sorted_samples = sorted(self.samples)
        idx = int(len(sorted_samples) * p / 100)
        return sorted_samples[min(idx, len(sorted_samples) - 1)]

    def to_dict(self) -> dict:
        return {
            'count': self.count,
            'mean': self.mean,
            'median': self.median,
            'stddev': self.stddev,
            'p50': self.p50,
            'p90': self.p90,
            'p99': self.p99,
            'min': self.min,
            'max': self.max,
        }


@dataclass
class RateMetrics:
    """Success/failure rate tracking."""
    successes: int = 0
    failures: int = 0

    def record_success(self):
        self.successes += 1

    def record_failure(self):
        self.failures += 1

    @property
    def total(self) -> int:
        return self.successes + self.failures

    @property
    def success_rate(self) -> float:
        return self.successes / self.total if self.total > 0 else 0.0

    @property
    def failure_rate(self) -> float:
        return self.failures / self.total if self.total > 0 else 0.0

    def to_dict(self) -> dict:
        return {
            'successes': self.successes,
            'failures': self.failures,
            'total': self.total,
            'success_rate': self.success_rate,
            'failure_rate': self.failure_rate,
        }


@dataclass
class MemoryMetrics:
    """Memory usage tracking."""
    samples: List[int] = field(default_factory=list)  # bytes

    def record(self, bytes_used: int):
        self.samples.append(bytes_used)

    @property
    def current(self) -> int:
        return self.samples[-1] if self.samples else 0

    @property
    def peak(self) -> int:
        return max(self.samples) if self.samples else 0

    @property
    def mean(self) -> float:
        return statistics.mean(self.samples) if self.samples else 0.0

    def to_dict(self) -> dict:
        return {
            'current_bytes': self.current,
            'peak_bytes': self.peak,
            'mean_bytes': self.mean,
            'current_kb': self.current / 1024,
            'peak_kb': self.peak / 1024,
        }


class MetricsCollector:
    """
    Unified metrics collection for POAC experiments.

    Collects:
    - Latency distributions
    - Success/failure rates
    - Memory usage
    - Custom counters
    - Theoretical vs actual comparisons
    """

    def __init__(self, experiment_name: str):
        self.experiment_name = experiment_name
        self.start_time = time.perf_counter()

        self.latencies: Dict[str, LatencyMetrics] = defaultdict(LatencyMetrics)
        self.rates: Dict[str, RateMetrics] = defaultdict(RateMetrics)
        self.memory: Dict[str, MemoryMetrics] = defaultdict(MemoryMetrics)
        self.counters: Dict[str, int] = defaultdict(int)
        self.gauges: Dict[str, float] = {}

        # For theoretical comparison
        self.theoretical_predictions: Dict[str, float] = {}
        self.actual_measurements: Dict[str, float] = {}

    def record_latency(self, operation: str, latency_ms: float):
        """Record a latency sample."""
        self.latencies[operation].add(latency_ms)

    def record_success(self, operation: str):
        """Record a successful operation."""
        self.rates[operation].record_success()

    def record_failure(self, operation: str):
        """Record a failed operation."""
        self.rates[operation].record_failure()

    def record_memory(self, component: str, bytes_used: int):
        """Record memory usage."""
        self.memory[component].record(bytes_used)

    def increment(self, counter: str, amount: int = 1):
        """Increment a counter."""
        self.counters[counter] += amount

    def set_gauge(self, name: str, value: float):
        """Set a gauge value."""
        self.gauges[name] = value

    def set_theoretical(self, metric: str, value: float):
        """Record theoretical prediction for later comparison."""
        self.theoretical_predictions[metric] = value

    def set_actual(self, metric: str, value: float):
        """Record actual measurement for comparison."""
        self.actual_measurements[metric] = value

    def get_comparison(self, metric: str) -> Optional[dict]:
        """Get theoretical vs actual comparison for a metric."""
        if metric not in self.theoretical_predictions:
            return None
        if metric not in self.actual_measurements:
            return None

        theoretical = self.theoretical_predictions[metric]
        actual = self.actual_measurements[metric]
        error = abs(actual - theoretical)
        relative_error = error / theoretical if theoretical != 0 else float('inf')

        return {
            'metric': metric,
            'theoretical': theoretical,
            'actual': actual,
            'absolute_error': error,
            'relative_error': relative_error,
            'within_10_percent': relative_error < 0.1,
            'within_20_percent': relative_error < 0.2,
        }

    def elapsed_seconds(self) -> float:
        """Get elapsed time since collector creation."""
        return time.perf_counter() - self.start_time

    def summary(self) -> dict:
        """Generate comprehensive summary."""
        return {
            'experiment': self.experiment_name,
            'elapsed_seconds': self.elapsed_seconds(),
            'latencies': {k: v.to_dict() for k, v in self.latencies.items()},
            'rates': {k: v.to_dict() for k, v in self.rates.items()},
            'memory': {k: v.to_dict() for k, v in self.memory.items()},
            'counters': dict(self.counters),
            'gauges': dict(self.gauges),
            'comparisons': [
                self.get_comparison(m)
                for m in self.theoretical_predictions.keys()
                if self.get_comparison(m) is not None
            ],
        }

    def print_summary(self):
        """Print formatted summary."""
        s = self.summary()

        print(f"\n{'='*60}")
        print(f"Experiment: {s['experiment']}")
        print(f"Duration: {s['elapsed_seconds']:.2f}s")
        print(f"{'='*60}")

        if s['latencies']:
            print("\nLatencies (ms):")
            for name, metrics in s['latencies'].items():
                print(f"  {name}:")
                print(f"    mean={metrics['mean']:.3f}, p50={metrics['p50']:.3f}, "
                      f"p99={metrics['p99']:.3f}, n={metrics['count']}")

        if s['rates']:
            print("\nRates:")
            for name, metrics in s['rates'].items():
                print(f"  {name}: {metrics['success_rate']:.1%} success "
                      f"({metrics['successes']}/{metrics['total']})")

        if s['memory']:
            print("\nMemory:")
            for name, metrics in s['memory'].items():
                print(f"  {name}: current={metrics['current_kb']:.1f}KB, "
                      f"peak={metrics['peak_kb']:.1f}KB")

        if s['counters']:
            print("\nCounters:")
            for name, value in s['counters'].items():
                print(f"  {name}: {value}")

        if s['comparisons']:
            print("\nTheoretical vs Actual:")
            for comp in s['comparisons']:
                status = "✓" if comp['within_10_percent'] else "✗"
                print(f"  {status} {comp['metric']}: "
                      f"predicted={comp['theoretical']:.4f}, "
                      f"actual={comp['actual']:.4f}, "
                      f"error={comp['relative_error']:.1%}")

    def to_json(self) -> str:
        """Export summary as JSON."""
        return json.dumps(self.summary(), indent=2)


def confidence_interval(samples: List[float], confidence: float = 0.95) -> Tuple[float, float]:
    """
    Calculate confidence interval for sample mean.

    Returns (lower, upper) bounds.
    """
    if len(samples) < 2:
        return (0.0, 0.0)

    n = len(samples)
    mean = statistics.mean(samples)
    stderr = statistics.stdev(samples) / math.sqrt(n)

    if HAS_SCIPY:
        # Use t-distribution for small samples
        t_value = scipy_stats.t.ppf((1 + confidence) / 2, n - 1)
    else:
        # Approximate t-value for 95% confidence
        # This is a rough approximation for common cases
        if n > 30:
            t_value = 1.96  # Approximate normal
        elif n > 10:
            t_value = 2.1
        else:
            t_value = 2.5

    margin = t_value * stderr
    return (mean - margin, mean + margin)


def hypothesis_test(
    sample1: List[float],
    sample2: List[float],
    alpha: float = 0.05,
) -> dict:
    """
    Perform two-sample t-test to compare means.

    Tests H0: mean1 = mean2 vs H1: mean1 ≠ mean2
    """
    if len(sample1) < 2 or len(sample2) < 2:
        return {'error': 'Insufficient samples'}

    if HAS_SCIPY:
        t_stat, p_value = scipy_stats.ttest_ind(sample1, sample2)
    else:
        # Simple approximation without scipy
        mean1 = statistics.mean(sample1)
        mean2 = statistics.mean(sample2)
        var1 = statistics.variance(sample1)
        var2 = statistics.variance(sample2)
        n1, n2 = len(sample1), len(sample2)

        # Welch's t-test approximation
        se = math.sqrt(var1 / n1 + var2 / n2)
        t_stat = (mean1 - mean2) / se if se > 0 else 0

        # Very rough p-value approximation
        p_value = 0.05 if abs(t_stat) > 2 else 0.5

    return {
        't_statistic': t_stat,
        'p_value': p_value,
        'reject_null': p_value < alpha,
        'mean1': statistics.mean(sample1),
        'mean2': statistics.mean(sample2),
        'interpretation': (
            f"Means are {'significantly different' if p_value < alpha else 'not significantly different'} "
            f"at α={alpha}"
        ),
    }
