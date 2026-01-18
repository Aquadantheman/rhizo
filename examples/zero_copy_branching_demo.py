#!/usr/bin/env python3
"""
Armillaria Demo: Zero-Copy Data Branching

This demo shows Git-like branching for your data - without copying anything.

The Problem:
    To experiment with data transformations safely, you currently:
    1. Copy the entire dataset (expensive, slow, doubles storage)
    2. Run experiments on a sample (not representative)
    3. YOLO on production (risky)

The Armillaria Solution:
    Create a branch instantly. Zero storage overhead. Full isolation.
    Experiment freely. Merge when ready. Delete if not.

Run: python examples/zero_copy_branching_demo.py
"""

import os
import sys
import tempfile
import shutil
import time

import pandas as pd
import numpy as np

# Add python directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'python'))

import rhizo
import _rhizo
from rhizo import QueryEngine


def print_header(text: str) -> None:
    """Print a section header."""
    print(f"\n{'='*60}")
    print(f"  {text}")
    print('='*60)


def get_directory_size(path: str) -> int:
    """Get total size of directory in bytes."""
    total = 0
    for dirpath, dirnames, filenames in os.walk(path):
        for f in filenames:
            fp = os.path.join(dirpath, f)
            total += os.path.getsize(fp)
    return total


def format_bytes(size_bytes: int) -> str:
    """Format bytes as human-readable string."""
    size = float(size_bytes)
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def wait_for_input(prompt: str = "Press Enter to continue...") -> None:
    """Wait for user input, skip if non-interactive."""
    try:
        input(prompt)
    except EOFError:
        pass


def main():
    print(r"""
     _                _ _ _            _
    / \   _ __ _ __ _(_) | | __ _ _ __(_) __ _
   / _ \ | '__| '_ ` _ \ | | / _` | '__| |/ _` |
  / ___ \| |  | | | | | | | | (_| | |  | | (_| |
 /_/   \_\_|  |_| |_| |_|_|_|\__,_|_|  |_|\__,_|

    Zero-Copy Data Branching Demo
    "Git for your data. No copies required."
    """)

    # Create temporary storage
    base_dir = tempfile.mkdtemp(prefix="rhizo_branch_demo_")
    print(f"Storage: {base_dir}")

    try:
        # Initialize Armillaria
        store = _rhizo.PyChunkStore(os.path.join(base_dir, "chunks"))
        catalog = _rhizo.PyCatalog(os.path.join(base_dir, "catalog"))
        branches = _rhizo.PyBranchManager(os.path.join(base_dir, "branches"))

        engine = QueryEngine(store, catalog, branch_manager=branches)

        # ============================================================
        print_header("SETUP: Create Production Data")
        # ============================================================

        print("Creating a realistic dataset...")

        # Create a larger dataset to show storage savings
        np.random.seed(42)
        n_rows = 50000

        scores_df = pd.DataFrame({
            "id": range(1, n_rows + 1),
            "user_id": np.random.randint(1, 1000, n_rows),
            "score": np.random.uniform(0, 100, n_rows).round(2),
            "category": np.random.choice(["A", "B", "C", "D"], n_rows),
            "created_at": pd.date_range("2024-01-01", periods=n_rows, freq="min").astype(str)
        })

        engine.write_table("scores", scores_df)

        storage_after_initial = get_directory_size(base_dir)
        print(f"\nDataset: {n_rows:,} rows")
        print(f"Storage used: {format_bytes(storage_after_initial)}")

        avg_score = engine.query("SELECT AVG(score) as avg FROM scores").to_pandas()['avg'].iloc[0]
        print(f"Average score: {avg_score:.2f}")

        # ============================================================
        print_header("DEMO 1: Instant Branch Creation")
        # ============================================================

        print("""
Traditional approach to experiment safely:
  1. Copy entire dataset: cp -r data/ data_experiment/
  2. Wait for copy to complete...
  3. Storage doubles immediately

Armillaria approach:
  1. Create branch (instant, zero-copy)
  2. Done.
        """)

        wait_for_input("Press Enter to create a branch...")

        storage_before_branch = get_directory_size(base_dir)
        start_time = time.time()

        engine.create_branch("experiment/new-scoring", description="Testing new scoring algorithm")

        branch_time = time.time() - start_time
        storage_after_branch = get_directory_size(base_dir)

        print("\nBranch created: 'experiment/new-scoring'")
        print(f"Time taken: {branch_time*1000:.2f} ms")
        print(f"Storage before: {format_bytes(storage_before_branch)}")
        print(f"Storage after:  {format_bytes(storage_after_branch)}")
        print(f"Storage increase: {format_bytes(storage_after_branch - storage_before_branch)}")
        print("\n(That's just metadata - the data itself is shared!)")

        # ============================================================
        print_header("DEMO 2: Isolated Experimentation")
        # ============================================================

        print("""
Now we can experiment on the branch without affecting production.
Let's apply a new scoring algorithm that boosts all scores by 10%.
        """)

        wait_for_input("Press Enter to modify data on the branch...")

        # Switch to experiment branch
        engine.checkout("experiment/new-scoring")
        print(f"\nChecked out: {engine.current_branch}")

        # Apply experimental transformation
        boosted_scores = scores_df.copy()
        boosted_scores["score"] = (boosted_scores["score"] * 1.1).clip(upper=100).round(2)

        engine.write_table("scores", boosted_scores)

        storage_after_experiment = get_directory_size(base_dir)
        new_data_size = storage_after_experiment - storage_after_branch

        print("\nApplied 10% score boost on experiment branch")
        print(f"New data written: {format_bytes(new_data_size)}")
        print(f"Total storage: {format_bytes(storage_after_experiment)}")

        # ============================================================
        print_header("DEMO 3: Branch Isolation Proof")
        # ============================================================

        print("""
Key question: Did our experiment affect production?
Let's query both branches and compare.
        """)

        wait_for_input("Press Enter to compare branches...")

        # Query both branches
        main_avg = engine.query(
            "SELECT AVG(score) as avg FROM scores",
            branch="main"
        ).to_pandas()['avg'].iloc[0]

        exp_avg = engine.query(
            "SELECT AVG(score) as avg FROM scores",
            branch="experiment/new-scoring"
        ).to_pandas()['avg'].iloc[0]

        print(f"\nAverage score on 'main':                  {main_avg:.2f}")
        print(f"Average score on 'experiment/new-scoring': {exp_avg:.2f}")
        print(f"Difference:                                +{exp_avg - main_avg:.2f}")
        print("\nProduction data is UNCHANGED!")

        # ============================================================
        print_header("DEMO 4: Diff Between Branches")
        # ============================================================

        print("""
Before merging, let's see exactly what changed.
        """)

        wait_for_input("Press Enter to diff branches...")

        diff = engine.diff_branches("experiment/new-scoring", "main")

        print("\nBranch diff (experiment vs main):")
        print(f"  Modified tables: {diff['modified']}")
        print(f"  Added tables:    {diff['added_in_source']}")
        print(f"  Removed tables:  {diff['added_in_target']}")

        # ============================================================
        print_header("DEMO 5: Promote Experiment to Production")
        # ============================================================

        print("""
Now we can decide:
  - Happy with results? Promote to production.
  - Not happy? Delete branch. No mess.

For this demo, we'll "promote" by copying the experiment's table version
to main. In a real workflow, you might:
  - Use the experiment branch AS the new production
  - Or rebuild main from the experiment
        """)

        wait_for_input("Press Enter to promote the experiment...")

        # Get the experiment's version and update main's head to point to it
        exp_branch = branches.get("experiment/new-scoring")
        exp_version = exp_branch.head.get("scores")
        if exp_version is not None:
            branches.update_head("main", "scores", exp_version)

        engine.checkout("main")
        # Clear cache so it reloads from the new head
        engine._registered.clear()

        promoted_avg = engine.query("SELECT AVG(score) as avg FROM scores").to_pandas()['avg'].iloc[0]

        print(f"\nPromoted experiment's scores (v{exp_version}) to main")
        print(f"Production average score is now: {promoted_avg:.2f}")

        # ============================================================
        print_header("DEMO 6: Storage Efficiency Summary")
        # ============================================================

        final_storage = get_directory_size(base_dir)

        print(f"""
Storage Analysis:

  Initial data:           {format_bytes(storage_after_initial)}
  After branch creation:  {format_bytes(storage_after_branch)} (+{format_bytes(storage_after_branch - storage_after_initial)})
  After experiment:       {format_bytes(storage_after_experiment)} (+{format_bytes(storage_after_experiment - storage_after_branch)})
  After merge:            {format_bytes(final_storage)}

Traditional approach (copy entire dataset):
  Initial:    {format_bytes(storage_after_initial)}
  Copy:       {format_bytes(storage_after_initial)} (duplicate)
  Total:      {format_bytes(storage_after_initial * 2)}

Armillaria approach:
  Total:      {format_bytes(final_storage)}
  Savings:    {format_bytes(storage_after_initial * 2 - final_storage)} ({100 * (1 - final_storage / (storage_after_initial * 2)):.0f}% less!)
        """)

        # ============================================================
        print_header("SUMMARY")
        # ============================================================

        print("""
What we demonstrated:

1. INSTANT BRANCH CREATION
   - Milliseconds, not minutes
   - Zero storage overhead (just metadata)

2. FULL ISOLATION
   - Experiments don't affect production
   - Query any branch independently

3. EFFICIENT STORAGE
   - Only changed data is stored
   - Shared chunks between branches

4. SIMPLE WORKFLOW
   - Create branch, experiment, merge or delete
   - Git-like workflow for data

This is what makes Armillaria unique.
Data versioning should be this simple.
        """)

    finally:
        # Cleanup
        shutil.rmtree(base_dir, ignore_errors=True)
        print(f"\nCleaned up: {base_dir}")


if __name__ == "__main__":
    main()
