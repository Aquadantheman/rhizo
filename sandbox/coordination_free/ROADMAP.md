# Coordination-Free Transactions: Implementation Roadmap

> **Principle:** Each phase is independently testable. No phase modifies existing code until proven in isolation.

## Overview

```
Phase 1: Theory & Proofs (sandbox only)
    ↓
Phase 2: Vector Clocks (new module, no integration)
    ↓
Phase 3: Local Commit Protocol (new module, no integration)
    ↓
Phase 4: Simulated Multi-Node (in-memory, no network)
    ↓
Phase 5: Integration (opt-in flag, existing tests unchanged)
    ↓
Phase 6: Benchmarks & Paper
```

---

## Phase 1: Theory & Proofs
**Goal:** Prove mathematically that this works before writing code
**Location:** `sandbox/coordination_free/`
**Regression risk:** ZERO (no code changes)

### Tasks

- [x] **1.1** Write formal proof of algebraic commutativity → convergence
- [x] **1.2** Write formal proof of vector clock causality tracking
- [x] **1.3** Define exactly which Rhizo operations are algebraic
- [x] **1.4** Identify edge cases (overflow, floating point, etc.)
- [x] **1.5** Draft paper introduction and related work

### Deliverables
- `sandbox/coordination_free/proofs/convergence_proof.md`
- `sandbox/coordination_free/proofs/causality_proof.md`
- `sandbox/coordination_free/paper_draft.md`

### Test
- Peer review of proofs (manual)
- No code tests yet

---

## Phase 2: Vector Clocks
**Goal:** Implement causality tracking as isolated module
**Location:** `rhizo_core/src/distributed/vector_clock.rs` (NEW)
**Regression risk:** ZERO (new module, not imported anywhere)

### Tasks

- [x] **2.1** Create `rhizo_core/src/distributed/mod.rs`
- [x] **2.2** Implement `VectorClock` struct
- [x] **2.3** Implement `tick()`, `merge()`, `happened_before()`, `concurrent()`
- [x] **2.4** Add serialization (serde)
- [x] **2.5** Write comprehensive unit tests
- [x] **2.6** Add Python bindings (PyVectorClock, PyNodeId, PyCausalOrder)
- [x] **2.7** Write Python tests (26 tests)

### Deliverables
- `rhizo_core/src/distributed/mod.rs`
- `rhizo_core/src/distributed/vector_clock.rs`

### Tests
```rust
#[test] fn test_tick_increments_local_clock();
#[test] fn test_merge_takes_max();
#[test] fn test_happened_before_ordering();
#[test] fn test_concurrent_detection();
#[test] fn test_serialization_roundtrip();
```

### Regression Check
```bash
cargo test --all  # All 306 existing tests must pass
```

---

## Phase 3: Local Commit Protocol
**Goal:** Implement commit-without-coordination logic
**Location:** `rhizo_core/src/distributed/local_commit.rs` (NEW)
**Regression risk:** ZERO (new module, not imported anywhere)

### Tasks

- [x] **3.1** Define `AlgebraicTransaction` struct (wraps operations + vector clock)
- [x] **3.2** Implement `can_commit_locally()` check
- [x] **3.3** Implement `commit_local()` that returns versioned update
- [x] **3.4** Implement `merge_updates()` using existing AlgebraicMerger
- [x] **3.5** Write unit tests (32 Rust tests)
- [x] **3.6** Add Python bindings (PyAlgebraicOperation, PyAlgebraicTransaction, PyVersionedUpdate, PyLocalCommitProtocol)
- [x] **3.7** Write Python tests (25 tests covering commutativity, associativity, convergence)

### Deliverables
- `rhizo_core/src/distributed/local_commit.rs`

### Tests
```rust
#[test] fn test_algebraic_tx_can_commit_locally();
#[test] fn test_non_algebraic_tx_cannot_commit_locally();
#[test] fn test_merge_two_local_commits();
#[test] fn test_merge_is_commutative();  // Critical!
#[test] fn test_merge_is_associative();  // Critical!
```

### Regression Check
```bash
cargo test --all  # All existing tests must pass
cargo clippy --all-targets -- -D warnings
```

---

## Phase 4: Simulated Multi-Node
**Goal:** Prove convergence with multiple simulated nodes (no real network)
**Location:** `rhizo_core/src/distributed/simulation.rs` (NEW)
**Regression risk:** ZERO (test/simulation code only)

### Tasks

- [x] **4.1** Create `SimulatedNode` that wraps state + clock + local updates
- [x] **4.2** Create `SimulatedCluster` with N nodes
- [x] **4.3** Implement message passing (in-memory broadcast/receive)
- [x] **4.4** Implement various network conditions (partition, heal, requeue)
- [x] **4.5** Write convergence tests (20 Rust tests)
- [x] **4.6** Add Python bindings (PySimulatedCluster, PySimulationBuilder, etc.)
- [x] **4.7** Write Python tests (12 tests)

### Deliverables
- `rhizo_core/src/distributed/simulation.rs` (1100+ lines)
- `tests/test_distributed.py` (simulation tests)
- `python/_rhizo.pyi` (type stubs)

### Tests
```rust
#[test] fn test_two_nodes_converge();
#[test] fn test_five_nodes_converge();
#[test] fn test_convergence_under_message_reordering();
#[test] fn test_convergence_after_partition_heal();
#[test] fn test_concurrent_writes_same_key_merge_correctly();

// Property-based tests
#[test] fn prop_any_order_same_result();
#[test] fn prop_merge_commutative();
#[test] fn prop_merge_associative();
```

### Regression Check
```bash
cargo test --all
python -m pytest tests/  # Python tests still pass
```

---

## Phase 5: Integration (Opt-In)
**Goal:** Integrate with existing transaction system via opt-in flag
**Location:** Modify `rhizo_core/src/transaction/` (CAREFUL)
**Regression risk:** LOW (opt-in only, default behavior unchanged)

### Tasks

- [ ] **5.1** Add `TransactionMode` enum to transaction types
- [ ] **5.2** Add `distributed` feature flag to Cargo.toml
- [ ] **5.3** Extend `TransactionManager` with optional distributed mode
- [ ] **5.4** Add Python bindings for distributed mode
- [ ] **5.5** Write integration tests

### Key Constraint
```rust
// Default behavior MUST be unchanged
impl Default for TransactionMode {
    fn default() -> Self {
        Self::Coordinated  // Existing behavior
    }
}
```

### Tests
```rust
// All existing tests run with default mode
#[test] fn test_existing_behavior_unchanged();

// New tests for distributed mode
#[test] fn test_distributed_mode_local_commit();
#[test] fn test_distributed_mode_merge();
```

### Regression Check
```bash
# Critical: run without distributed feature
cargo test --all  # Must pass

# Then with feature
cargo test --all --features distributed
```

---

## Phase 6: Benchmarks & Paper
**Goal:** Demonstrate performance and write paper
**Location:** `benchmarks/`, `sandbox/coordination_free/paper/`
**Regression risk:** ZERO (benchmarks and docs only)

### Tasks

- [ ] **6.1** Create multi-node benchmark harness
- [ ] **6.2** Benchmark: latency comparison (local vs consensus)
- [ ] **6.3** Benchmark: throughput scaling with nodes
- [ ] **6.4** Benchmark: convergence time under various conditions
- [ ] **6.5** Complete paper with results

### Deliverables
- `benchmarks/distributed_benchmark.py`
- `sandbox/coordination_free/paper/coordination_free_acid.md`
- Performance graphs and tables

---

## Regression Prevention Strategy

### Before Each Phase
```bash
# Save baseline
cargo test --all 2>&1 | tee baseline_rust.txt
python -m pytest tests/ 2>&1 | tee baseline_python.txt
```

### After Each Phase
```bash
# Compare
cargo test --all 2>&1 | tee current_rust.txt
diff baseline_rust.txt current_rust.txt  # Should show no regressions

python -m pytest tests/ 2>&1 | tee current_python.txt
diff baseline_python.txt current_python.txt
```

### CI Integration
```yaml
# Add to CI workflow
- name: Regression check
  run: |
    cargo test --all
    python -m pytest tests/
    # Distributed feature doesn't break non-distributed
    cargo test --all --features distributed
```

---

## Timeline Estimate

| Phase | Effort | Dependencies |
|-------|--------|--------------|
| Phase 1: Theory | 2-3 days | None |
| Phase 2: Vector Clocks | 1-2 days | Phase 1 |
| Phase 3: Local Commit | 2-3 days | Phase 2 |
| Phase 4: Simulation | 3-4 days | Phase 3 |
| Phase 5: Integration | 2-3 days | Phase 4 |
| Phase 6: Benchmarks | 2-3 days | Phase 5 |

**Total: ~2-3 weeks for publishable result**

---

## Decision Points

### After Phase 1
- Do the proofs hold? Any edge cases we can't handle?
- Go/no-go for implementation

### After Phase 4
- Does simulation show convergence?
- Performance acceptable?
- Go/no-go for integration

### After Phase 5
- Any regressions?
- API ergonomic?
- Go/no-go for public release

---

## What Success Looks Like

```python
# User code (Phase 5 complete)
with engine.transaction(mode="coordination_free") as tx:
    tx.increment("counters", "page_views", 1)  # Algebraic!
    tx.add_to_set("users", "user_123", "tags", "premium")  # Algebraic!
    # Commits locally in <10ms
    # Propagates to other nodes automatically
    # Merges correctly regardless of order
```

```
Benchmark results (Phase 6 complete):
- 5 nodes, 10K transactions each
- Traditional: 150ms avg latency, 500 tx/sec total
- Coordination-free: 5ms avg latency, 50K tx/sec total
- All nodes converge to identical state
```
