# Validation Plan: Proving Coordination Bounds

## Goal

Transform theoretical proofs into empirically validated results suitable for top-tier publication (PODC, VLDB, SIGMOD, OSDI).

---

## Phase 1: Instrument Rhizo

### 1.1 Add Timing Instrumentation

**File: `python/rhizo/writer.py`**

Add measurement hooks at commit points:

```python
import time
from dataclasses import dataclass
from typing import Optional

@dataclass
class CommitMetrics:
    operation_type: str           # "algebraic" or "generic"
    algebraic_signature: str      # "semilattice", "abelian", "generic"
    commit_latency_ms: float      # Time from issue to safe commit
    coordination_rounds: int      # 0 for algebraic, N for consensus
    messages_sent: int            # Network messages required
    bytes_transferred: int        # Total network bytes

class InstrumentedWriter(RhizoWriter):
    def __init__(self, *args, collect_metrics=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.collect_metrics = collect_metrics
        self.metrics: list[CommitMetrics] = []

    def write(self, table, ...):
        if self.collect_metrics:
            start = time.perf_counter_ns()

        # ... existing write logic ...

        if self.collect_metrics:
            elapsed_ms = (time.perf_counter_ns() - start) / 1_000_000
            self.metrics.append(CommitMetrics(
                operation_type=self._classify_operation(table),
                algebraic_signature=self._get_signature(table),
                commit_latency_ms=elapsed_ms,
                coordination_rounds=0,  # Rhizo is coordination-free for algebraic
                messages_sent=0,
                bytes_transferred=0,
            ))
```

### 1.2 Add Operation Classification Hook

Detect algebraic vs generic at write time:

```python
def _classify_operation(self, table) -> str:
    """Classify operation based on merge strategy."""
    # Check if all columns use algebraic merge
    for col in table.schema:
        merge_type = self._get_merge_type(col)
        if merge_type not in ("sum", "max", "min", "union"):
            return "generic"
    return "algebraic"
```

### 1.3 Metrics Export

```python
def export_metrics(self, path: str):
    """Export metrics for analysis."""
    import json
    with open(path, 'w') as f:
        json.dump([m.__dict__ for m in self.metrics], f, indent=2)
```

---

## Phase 2: Baseline Comparisons

### 2.1 Systems to Compare

| System | Type | Expected Coordination |
|--------|------|----------------------|
| **Rhizo (algebraic)** | Coordination-free | 0 rounds |
| **Rhizo (generic)** | Coordinated | O(log N) |
| PostgreSQL | Single-node | N/A (no distribution) |
| CockroachDB | Consensus-based | O(log N) for all |
| TiDB | Consensus-based | O(log N) for all |
| Redis Cluster | Varies | Depends on operation |

### 2.2 Benchmark Script

```python
"""
benchmark_coordination.py

Compare coordination costs across systems.
"""

import time
import statistics
from typing import List, Tuple

# Rhizo
import rhizo

# Comparison systems (via their Python clients)
# import cockroachdb
# import tidb
# import redis

def benchmark_algebraic_ops(num_trials: int = 10000) -> dict:
    """Benchmark algebraic operations across systems."""

    results = {}

    # Rhizo - Algebraic (ADD)
    writer = rhizo.RhizoWriter("benchmark_table", collect_metrics=True)
    latencies = []
    for i in range(num_trials):
        start = time.perf_counter_ns()
        writer.increment("counter", 1)  # ADD operation
        elapsed = (time.perf_counter_ns() - start) / 1_000_000
        latencies.append(elapsed)

    results["rhizo_algebraic"] = {
        "mean_ms": statistics.mean(latencies),
        "p50_ms": statistics.median(latencies),
        "p99_ms": sorted(latencies)[int(len(latencies) * 0.99)],
        "coordination_rounds": 0,
    }

    # CockroachDB - Same operation (but coordinated)
    # ... similar benchmark code ...

    return results

def benchmark_generic_ops(num_trials: int = 1000) -> dict:
    """Benchmark generic operations across systems."""

    results = {}

    # Rhizo - Generic (OVERWRITE) - would need coordination
    # ... benchmark code ...

    # CockroachDB - Generic
    # ... benchmark code ...

    return results

def benchmark_varying_cluster_size() -> dict:
    """
    KEY EXPERIMENT: Validate O(log N) bound.

    Run same operations with N = 2, 4, 8, 16, 32 nodes.
    Plot coordination rounds vs log(N).
    """

    results = {}

    for n_nodes in [2, 4, 8, 16, 32]:
        # Spin up cluster with n_nodes
        # Run benchmark
        # Record coordination rounds and latency
        pass

    return results
```

---

## Phase 3: Validate Theoretical Predictions

### 3.1 Hypothesis Tests

| Hypothesis | Test | Expected Result |
|------------|------|-----------------|
| H1: Algebraic ops have C=0 | Measure coordination rounds | rounds = 0 |
| H2: Generic ops have C=Ω(log N) | Vary N, measure rounds | rounds ~ log(N) |
| H3: Latency ratio matches theory | Compare algebraic/generic | ratio ~ (log N * RTT) / local |
| H4: Multi-key inherits max | Mixed transaction | coord = max(individual) |

### 3.2 Statistical Analysis

```python
def validate_log_n_bound(measurements: List[Tuple[int, float]]) -> dict:
    """
    Validate that coordination rounds scale as O(log N).

    Args:
        measurements: List of (cluster_size, coordination_rounds)

    Returns:
        Regression results and confidence intervals
    """
    import numpy as np
    from scipy import stats

    sizes = np.array([m[0] for m in measurements])
    rounds = np.array([m[1] for m in measurements])

    # Fit: rounds = a * log(N) + b
    log_sizes = np.log2(sizes)
    slope, intercept, r_value, p_value, std_err = stats.linregress(log_sizes, rounds)

    return {
        "slope": slope,  # Should be ~1 for O(log N)
        "intercept": intercept,
        "r_squared": r_value ** 2,  # Should be close to 1
        "p_value": p_value,  # Should be < 0.05
        "conclusion": "VALIDATED" if r_value ** 2 > 0.9 else "NEEDS INVESTIGATION"
    }
```

### 3.3 Multi-Key Transaction Test

```python
def test_multikey_coordination():
    """
    Prove: C(transaction) = max(C(operation))

    Test cases:
    1. All algebraic -> C = 0
    2. All generic -> C = O(log N)
    3. Mixed (1 generic) -> C = O(log N)
    """

    test_cases = [
        # (operations, expected_coordination)
        (["ADD", "ADD", "MAX"], 0),
        (["OVERWRITE", "OVERWRITE"], "O(log N)"),
        (["ADD", "ADD", "OVERWRITE"], "O(log N)"),  # One generic forces coordination
    ]

    for ops, expected in test_cases:
        actual = measure_transaction_coordination(ops)
        assert actual == expected, f"Failed: {ops}"
```

---

## Phase 4: Extended Test Cases

### 4.1 Edge Cases

| Test Case | Description | Expected |
|-----------|-------------|----------|
| Empty transaction | No operations | C = 0 |
| Single algebraic | One ADD | C = 0 |
| Single generic | One OVERWRITE | C = O(log N) |
| 1000 algebraic + 1 generic | Stress test | C = O(log N) |
| Concurrent algebraic | Parallel ADDs | All succeed, C = 0 |
| Concurrent generic | Parallel OVERWRITEs | Serialized, C = O(log N) |

### 4.2 Boundary Conditions

```python
def test_bounded_arithmetic():
    """
    Bounded ADD (with overflow check) loses algebraic property.
    Should require coordination.
    """
    # ADD with MAX_INT check -> becomes generic
    pass

def test_conditional_algebraic():
    """
    ADD WHERE condition -> generic (condition check)
    ADD unconditional -> algebraic
    """
    pass
```

### 4.3 Real Workload Traces

Use real query logs to validate workload composition:

```python
def analyze_production_workload(query_log: str) -> dict:
    """
    Analyze a production query log.

    Returns:
        - % algebraic operations
        - % generic operations
        - Predicted speedup from coordination-free commits
        - Actual measured speedup (if available)
    """
    pass
```

---

## Phase 5: Reproduce Known Results

### 5.1 Compare with Published Benchmarks

| Paper | Claimed Result | Our Validation |
|-------|---------------|----------------|
| Bailis 2014 (Coordination Avoidance) | Identifies avoidable coordination | We prove tight bounds |
| Shapiro 2011 (CRDTs) | Eventual consistency for specific types | We generalize to operations |
| Attiya & Welch | Consensus lower bound | We apply to DB operations |

### 5.2 Reproduce CRDT Convergence

```python
def test_crdt_equivalence():
    """
    Show that Rhizo's algebraic operations are equivalent to CRDTs.

    - G-Counter ~ ADD operation
    - Max-Register ~ MAX operation
    - OR-Set ~ UNION operation
    """
    pass
```

---

## Phase 6: Publication-Ready Artifacts

### 6.1 Reproducibility Package

```
coordination_bounds/
├── FULL_PAPER.md           # The paper
├── proofs/
│   ├── proofs_refined.md   # Formal proofs
│   ├── multi_key_extension.md
│   └── verification/       # Coq/Lean proofs (stretch goal)
├── benchmarks/
│   ├── benchmark_coordination.py
│   ├── benchmark_multikey.py
│   └── benchmark_varying_n.py
├── data/
│   ├── raw_measurements/
│   └── processed_results/
├── figures/
│   ├── latency_comparison.png
│   ├── log_n_validation.png
│   └── workload_analysis.png
└── README.md               # Reproduction instructions
```

### 6.2 Key Figures to Generate

1. **Figure 1**: Latency comparison (algebraic vs generic vs baseline)
2. **Figure 2**: Coordination rounds vs cluster size (validate O(log N))
3. **Figure 3**: Workload composition analysis
4. **Figure 4**: Energy consumption comparison
5. **Figure 5**: Multi-key transaction breakdown

---

## Timeline (No Dates - Just Sequence)

| Phase | Dependencies | Effort |
|-------|--------------|--------|
| 1. Instrument Rhizo | None | Medium |
| 2. Baseline comparisons | Phase 1 | High |
| 3. Validate predictions | Phase 1, 2 | Medium |
| 4. Extended test cases | Phase 1 | Medium |
| 5. Reproduce known results | Phase 3 | Low |
| 6. Publication artifacts | All above | Medium |

---

## Why This Could Be Huge

1. **Novel Contribution**: First formal proof of coordination bounds by algebraic type
2. **Practical Impact**: Gives practitioners a decision framework
3. **Explains Existing Systems**: Why CRDTs work, why some workloads can't be optimized
4. **Broad Applicability**: Applies to any distributed system, not just databases
5. **Timely**: Sustainability angle with energy analysis
6. **Strong Validation**: Real system (Rhizo) + comparisons + statistical analysis

**Target Venues (in order of fit):**
1. **PODC** - Theoretical distributed computing (best fit for proofs)
2. **VLDB/SIGMOD** - Database systems (best fit for practical impact)
3. **OSDI/SOSP** - Systems (if we emphasize implementation)
4. **Nature Communications** - If energy/sustainability angle is strong

---

## Next Steps

1. [ ] Instrument Rhizo writer with metrics collection
2. [ ] Create benchmark harness
3. [ ] Set up comparison systems (CockroachDB, etc.)
4. [ ] Run initial measurements
5. [ ] Generate validation plots
6. [ ] Iterate on paper based on results

