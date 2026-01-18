"""
Bloom Filter Write-Set Implementation

Mathematical Foundation:
- Optimal bits: m = -n*ln(p) / (ln(2))^2
- Optimal hash functions: k = (m/n) * ln(2)
- False positive rate: p = (1 - e^(-kn/m))^k

Key Properties:
- False negatives: IMPOSSIBLE (safety preserved)
- False positives: Bounded by p (tunable)
- Memory: O(1) fixed size, not O(rows)
"""

from __future__ import annotations
import math
import hashlib
from dataclasses import dataclass, field
from typing import Set, Tuple, List, Optional

# Try to import mmh3, fall back to hashlib-based implementation
try:
    import mmh3
    HAS_MMH3 = True
except ImportError:
    HAS_MMH3 = False


@dataclass
class BloomMetrics:
    """Metrics for bloom filter performance analysis."""
    insertions: int = 0
    queries: int = 0
    true_positives: int = 0
    false_positives: int = 0
    true_negatives: int = 0
    # Note: false_negatives should always be 0

    @property
    def actual_fp_rate(self) -> float:
        """Measured false positive rate."""
        total_negatives = self.false_positives + self.true_negatives
        if total_negatives == 0:
            return 0.0
        return self.false_positives / total_negatives

    @property
    def theoretical_fp_rate(self) -> float:
        """Will be set by the bloom filter based on parameters."""
        return 0.0


class BloomWriteSet:
    """
    Bloom filter for tracking write-sets with bounded false positive rate.

    Mathematical guarantees:
    - P(false_negative) = 0 (never misses a conflict)
    - P(false_positive) ≤ p (configurable, default 1%)
    - Memory = m bits (fixed, regardless of elements)

    Usage:
        ws = BloomWriteSet(expected_elements=10000, fp_rate=0.01)
        ws.add("table1", "row123")
        ws.add("table1", "row456")

        # Check for conflicts
        if ws.might_conflict_with(other_write_set):
            # Might conflict (could be false positive)
            ...
    """

    def __init__(
        self,
        expected_elements: int = 10000,
        fp_rate: float = 0.01,
        bits: Optional[int] = None,
        hash_count: Optional[int] = None,
    ):
        """
        Initialize bloom filter with optimal parameters.

        Args:
            expected_elements: Expected number of (table, row) pairs
            fp_rate: Target false positive rate (default 1%)
            bits: Override calculated bit count (for experiments)
            hash_count: Override calculated hash count (for experiments)
        """
        self.expected_elements = expected_elements
        self.target_fp_rate = fp_rate

        # Calculate optimal parameters
        if bits is None:
            # m = -n*ln(p) / (ln(2))^2
            self.num_bits = self._optimal_bits(expected_elements, fp_rate)
        else:
            self.num_bits = bits

        if hash_count is None:
            # k = (m/n) * ln(2)
            self.num_hashes = self._optimal_hashes(self.num_bits, expected_elements)
        else:
            self.num_hashes = hash_count

        # Initialize bit array (using bytearray for efficiency)
        self.num_bytes = (self.num_bits + 7) // 8
        self.bit_array = bytearray(self.num_bytes)

        # For validation: track actual elements (only in validation mode)
        self._validation_mode = False
        self._actual_elements: Set[Tuple[str, str]] = set()

        # Metrics
        self.metrics = BloomMetrics()

    @staticmethod
    def _optimal_bits(n: int, p: float) -> int:
        """Calculate optimal number of bits for given n elements and FP rate p."""
        if n <= 0 or p <= 0 or p >= 1:
            return 1024  # Sensible default
        m = -n * math.log(p) / (math.log(2) ** 2)
        return max(64, int(math.ceil(m)))

    @staticmethod
    def _optimal_hashes(m: int, n: int) -> int:
        """Calculate optimal number of hash functions."""
        if n <= 0:
            return 1
        k = (m / n) * math.log(2)
        return max(1, min(32, int(math.ceil(k))))  # Cap at 32 for performance

    def theoretical_fp_rate(self) -> float:
        """Calculate theoretical FP rate based on current parameters and fill."""
        # p = (1 - e^(-kn/m))^k
        n = self.metrics.insertions
        if n == 0:
            return 0.0
        k = self.num_hashes
        m = self.num_bits
        return (1 - math.exp(-k * n / m)) ** k

    def _hash_indices(self, table: str, row: str) -> List[int]:
        """Generate k hash indices for a (table, row) pair."""
        # Use double hashing: h(i) = h1 + i*h2
        key = f"{table}:{row}".encode('utf-8')

        if HAS_MMH3:
            h1 = mmh3.hash(key, seed=0) % self.num_bits
            h2 = mmh3.hash(key, seed=42) % self.num_bits
        else:
            # Fallback using hashlib
            h1 = int(hashlib.md5(key).hexdigest(), 16) % self.num_bits
            h2 = int(hashlib.sha1(key).hexdigest(), 16) % self.num_bits

        if h2 == 0:
            h2 = 1  # Avoid degenerate case

        indices = []
        for i in range(self.num_hashes):
            idx = (h1 + i * h2) % self.num_bits
            indices.append(idx)
        return indices

    def _set_bit(self, index: int):
        """Set a bit in the bit array."""
        byte_idx = index // 8
        bit_idx = index % 8
        self.bit_array[byte_idx] |= (1 << bit_idx)

    def _get_bit(self, index: int) -> bool:
        """Check if a bit is set."""
        byte_idx = index // 8
        bit_idx = index % 8
        return bool(self.bit_array[byte_idx] & (1 << bit_idx))

    def add(self, table: str, row: str):
        """Add a (table, row) pair to the write-set."""
        indices = self._hash_indices(table, row)
        for idx in indices:
            self._set_bit(idx)

        self.metrics.insertions += 1

        if self._validation_mode:
            self._actual_elements.add((table, row))

    def might_contain(self, table: str, row: str) -> bool:
        """
        Check if (table, row) might be in the set.

        Returns:
            True if possibly present (might be false positive)
            False if definitely not present (never false negative)
        """
        indices = self._hash_indices(table, row)
        result = all(self._get_bit(idx) for idx in indices)

        self.metrics.queries += 1

        if self._validation_mode:
            actually_present = (table, row) in self._actual_elements
            if result and actually_present:
                self.metrics.true_positives += 1
            elif result and not actually_present:
                self.metrics.false_positives += 1
            elif not result and not actually_present:
                self.metrics.true_negatives += 1
            # else: false negative - should never happen!

        return result

    def might_conflict_with(self, other: 'BloomWriteSet') -> bool:
        """
        Check if this write-set might conflict with another.

        Uses bitwise AND to check for potential overlap.
        """
        # If sizes differ, fall back to element-wise check
        if self.num_bytes != other.num_bytes:
            raise ValueError("Cannot compare bloom filters of different sizes")

        # Bitwise AND: if any bits overlap, might conflict
        for i in range(self.num_bytes):
            if self.bit_array[i] & other.bit_array[i]:
                return True
        return False

    def enable_validation_mode(self):
        """Enable tracking of actual elements for validation."""
        self._validation_mode = True
        self._actual_elements = set()

    def fill_ratio(self) -> float:
        """Return the fraction of bits that are set."""
        set_bits = sum(bin(byte).count('1') for byte in self.bit_array)
        return set_bits / self.num_bits

    def memory_bytes(self) -> int:
        """Return memory usage in bytes."""
        return self.num_bytes

    def __repr__(self) -> str:
        return (
            f"BloomWriteSet(elements={self.metrics.insertions}, "
            f"bits={self.num_bits}, hashes={self.num_hashes}, "
            f"fill={self.fill_ratio():.2%}, "
            f"theoretical_fp={self.theoretical_fp_rate():.4%})"
        )


class ExactWriteSet:
    """
    Exact write-set for comparison baseline.

    Uses O(n) memory where n = number of (table, row) pairs.
    Used to validate bloom filter correctness and measure memory savings.
    """

    def __init__(self):
        self.elements: Set[Tuple[str, str]] = set()
        self.metrics = BloomMetrics()

    def add(self, table: str, row: str):
        self.elements.add((table, row))
        self.metrics.insertions += 1

    def contains(self, table: str, row: str) -> bool:
        result = (table, row) in self.elements
        self.metrics.queries += 1
        if result:
            self.metrics.true_positives += 1
        else:
            self.metrics.true_negatives += 1
        return result

    def conflicts_with(self, other: 'ExactWriteSet') -> bool:
        return bool(self.elements & other.elements)

    def memory_bytes(self) -> int:
        """Estimate memory usage: ~80 bytes per element (Python overhead)."""
        return len(self.elements) * 80

    def __repr__(self) -> str:
        return f"ExactWriteSet(elements={len(self.elements)})"


def compare_bloom_vs_exact(
    num_elements: int,
    num_queries: int,
    fp_rate: float = 0.01,
    overlap_fraction: float = 0.1,
) -> dict:
    """
    Compare bloom filter against exact set for validation.

    Returns metrics comparing memory, accuracy, and performance.
    """
    import random
    import time

    # Create both types
    bloom = BloomWriteSet(expected_elements=num_elements, fp_rate=fp_rate)
    bloom.enable_validation_mode()
    exact = ExactWriteSet()

    # Generate elements
    elements = [(f"table_{i % 10}", f"row_{i}") for i in range(num_elements)]

    # Insert elements
    t0 = time.perf_counter()
    for table, row in elements:
        bloom.add(table, row)
    bloom_insert_time = time.perf_counter() - t0

    t0 = time.perf_counter()
    for table, row in elements:
        exact.add(table, row)
    exact_insert_time = time.perf_counter() - t0

    # Generate queries: mix of present and absent
    present_queries = random.sample(elements, min(num_queries // 2, len(elements)))
    absent_queries = [
        (f"table_{i % 10}", f"row_{num_elements + i}")
        for i in range(num_queries // 2)
    ]
    queries = present_queries + absent_queries
    random.shuffle(queries)

    # Run queries
    t0 = time.perf_counter()
    bloom_results = [bloom.might_contain(t, r) for t, r in queries]
    bloom_query_time = time.perf_counter() - t0

    t0 = time.perf_counter()
    exact_results = [exact.contains(t, r) for t, r in queries]
    exact_query_time = time.perf_counter() - t0

    # Validate: bloom should never have false negatives
    false_negatives = sum(
        1 for b, e in zip(bloom_results, exact_results)
        if e and not b
    )
    false_positives = sum(
        1 for b, e in zip(bloom_results, exact_results)
        if b and not e
    )

    return {
        'num_elements': num_elements,
        'num_queries': len(queries),
        'bloom_memory_bytes': bloom.memory_bytes(),
        'exact_memory_bytes': exact.memory_bytes(),
        'memory_savings': 1 - bloom.memory_bytes() / exact.memory_bytes(),
        'bloom_insert_time_ms': bloom_insert_time * 1000,
        'exact_insert_time_ms': exact_insert_time * 1000,
        'bloom_query_time_ms': bloom_query_time * 1000,
        'exact_query_time_ms': exact_query_time * 1000,
        'false_negatives': false_negatives,  # Should ALWAYS be 0
        'false_positives': false_positives,
        'actual_fp_rate': false_positives / (num_queries // 2) if num_queries > 0 else 0,
        'theoretical_fp_rate': bloom.theoretical_fp_rate(),
        'target_fp_rate': fp_rate,
        'fill_ratio': bloom.fill_ratio(),
    }


if __name__ == '__main__':
    # Quick validation test
    print("Bloom Filter Write-Set Validation")
    print("=" * 50)

    for n in [1000, 10000, 100000, 1000000]:
        results = compare_bloom_vs_exact(n, n // 10)
        print(f"\nElements: {n:,}")
        print(f"  Memory: Bloom={results['bloom_memory_bytes']:,}B, "
              f"Exact={results['exact_memory_bytes']:,}B "
              f"(savings={results['memory_savings']:.1%})")
        print(f"  FP Rate: actual={results['actual_fp_rate']:.4%}, "
              f"theoretical={results['theoretical_fp_rate']:.4%}, "
              f"target={results['target_fp_rate']:.4%}")
        print(f"  False Negatives: {results['false_negatives']} (must be 0!)")
