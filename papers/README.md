# Research Papers

Technical papers documenting Rhizo's theoretical foundations and implementation.

## Papers

| Paper | Description | Status |
|-------|-------------|--------|
| [cross_table_acid_paper.md](cross_table_acid_paper.md) | Cross-table ACID transactions via content-addressable storage | Core implementation |
| [acid_without_consensus_paper.md](acid_without_consensus_paper.md) | Algebraic transactions for coordination-free distributed commits | Implemented |
| [poac_paper.md](poac_paper.md) | Probabilistic Optimistic Algebraic Consistency | Research/Future |

## Summary

### Cross-Table ACID (Implemented)

The foundational paper describing how Rhizo achieves multi-table ACID transactions without a coordination service. Key contributions:
- Content-addressable storage with BLAKE3 hashing
- O(t) complexity for t-table transactions
- Zero-copy branching via pointer manipulation
- 1,500+ MB/s write throughput on commodity hardware

### ACID Without Consensus (Implemented)

Describes how algebraic operation classification enables coordination-free distributed transactions. Key contributions:
- Operations classified by algebraic structure (semilattice, Abelian group)
- Local commits for algebraically conflict-free operations
- 59x measured latency improvement vs localhost 2PC; 355x vs SQLite FULL sync (all measured, same machine)
- 97,943x energy reduction vs consensus energy model (estimated)

### POAC (Research)

Explores probabilistic techniques for further performance optimization. Experimental techniques:
- Bloom filter write-sets for O(1) conflict detection
- Speculative execution for low-conflict workloads
- Escrow transactions for hot spot scalability

## Citation

These papers are working drafts. If referencing this work, please cite the GitHub repository:

```
Rhizo: Data, connected.
https://github.com/rhizodata/rhizo
```

## Related Documentation

- [TECHNICAL_FOUNDATIONS.md](../docs/TECHNICAL_FOUNDATIONS.md) - Verified mathematical proofs
- [PERFORMANCE.md](../docs/PERFORMANCE.md) - Benchmark methodology and results
