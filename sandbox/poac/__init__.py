"""
POAC: Probabilistic Optimistic Algebraic Consistency

A sandbox for validating the mathematical foundations of POAC
before integration into the main Rhizo system.

This module provides:
- BloomWriteSet: O(1) memory conflict detection with bounded false positives
- SpeculativeExecutor: Adaptive speculation based on conflict probability
- EscrowManager: Pre-allocated quotas for hot-spot mitigation
- AlgebraicClassifier: Operation classification for conflict-free merging
- ExperimentHarness: Controlled experiments with metrics collection

Usage:
    from sandbox.poac import ExperimentHarness
    harness = ExperimentHarness()
    results = harness.run_all_experiments()
    harness.generate_report(results)
"""

from .bloom_write_set import BloomWriteSet
from .speculative_executor import SpeculativeExecutor
from .escrow_manager import EscrowManager
from .algebraic_classifier import AlgebraicClassifier, OpType
from .experiment_harness import ExperimentHarness
from .metrics import MetricsCollector

__all__ = [
    'BloomWriteSet',
    'SpeculativeExecutor',
    'EscrowManager',
    'AlgebraicClassifier',
    'OpType',
    'ExperimentHarness',
    'MetricsCollector',
]

__version__ = '0.1.0'
