#!/usr/bin/env python3
"""
Time Travel Demo - Showcase Armillaria's versioning and query capabilities.

This demo shows:
1. Writing data with automatic versioning
2. Querying specific historical versions (time travel)
3. Comparing versions to see what changed
4. SQL queries over versioned data

Run:
    python examples/time_travel_demo.py
"""

import os
import sys
import shutil
import tempfile

# Add python package to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'python'))

import pandas as pd

# Import Armillaria components
import rhizo
import _rhizo
from rhizo import QueryEngine


def print_section(title: str):
    """Print a section header."""
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}\n")


def main():
    print(r"""
     _                _ _ _            _
    / \   _ __ _ __ _(_) | | __ _ _ __(_) __ _
   / _ \ | '__| '_ ` _ \ | | / _` | '__| |/ _` |
  / ___ \| |  | | | | | | | | (_| | |  | | (_| |
 /_/   \_\_|  |_| |_| |_|_|_|\__,_|_|  |_|\__,_|

    Time Travel Demo
    "Query any version. Instantly."
    """)

    # Create temporary storage
    temp_dir = tempfile.mkdtemp(prefix="rhizo_demo_")
    chunks_dir = os.path.join(temp_dir, "chunks")
    catalog_dir = os.path.join(temp_dir, "catalog")

    print(f"Storage location: {temp_dir}")

    try:
        # Initialize Armillaria components
        store = _rhizo.PyChunkStore(chunks_dir)
        catalog = _rhizo.PyCatalog(catalog_dir)
        engine = QueryEngine(store, catalog)

        # =================================================================
        # STEP 1: Create initial data (Version 1)
        # =================================================================
        print_section("Step 1: Create Initial Data (Version 1)")

        parcels_v1 = pd.DataFrame({
            "parcel_id": ["P001", "P002", "P003", "P004", "P005"],
            "address": [
                "123 Main St",
                "456 Oak Ave",
                "789 Pine Rd",
                "321 Elm Blvd",
                "654 Maple Dr",
            ],
            "sqft": [1500, 2200, 1800, 3000, 1200],
            "score": [75, 82, 68, 91, 55],
        })

        result = engine.write_table("parcels", parcels_v1)
        print(f"Wrote parcels table: Version {result.version}, {result.total_rows} rows")
        print(f"Chunk hashes: {result.chunk_hashes}")
        print("\nData:")
        print(parcels_v1.to_string(index=False))

        # =================================================================
        # STEP 2: Update data (Version 2) - Score recalculation
        # =================================================================
        print_section("Step 2: Update Scores (Version 2)")

        parcels_v2 = parcels_v1.copy()
        parcels_v2["score"] = [78, 85, 72, 88, 60]  # Updated scores
        parcels_v2["last_updated"] = "2024-01-15"

        result = engine.write_table("parcels", parcels_v2)
        print(f"Wrote parcels table: Version {result.version}, {result.total_rows} rows")
        print("\nUpdated Data:")
        print(parcels_v2.to_string(index=False))

        # =================================================================
        # STEP 3: Add new parcels (Version 3)
        # =================================================================
        print_section("Step 3: Add New Parcels (Version 3)")

        new_parcels = pd.DataFrame({
            "parcel_id": ["P006", "P007"],
            "address": ["987 Cedar Ln", "147 Birch Way"],
            "sqft": [2500, 1900],
            "score": [79, 83],
            "last_updated": "2024-01-16",
        })

        parcels_v3 = pd.concat([parcels_v2, new_parcels], ignore_index=True)
        result = engine.write_table("parcels", parcels_v3)
        print(f"Wrote parcels table: Version {result.version}, {result.total_rows} rows")
        print("\nNew parcels added:")
        print(new_parcels.to_string(index=False))

        # =================================================================
        # STEP 4: Time Travel Queries
        # =================================================================
        print_section("Step 4: Time Travel Queries")

        # Query current (latest) version
        print("Query: SELECT COUNT(*), AVG(score) FROM parcels (latest)")
        result = engine.query("SELECT COUNT(*) as count, AVG(score) as avg_score FROM parcels")
        print(result.to_pandas().to_string(index=False))

        # Query version 1 (original data)
        print("\nQuery: SELECT COUNT(*), AVG(score) FROM parcels AS OF VERSION 1")
        result = engine.query(
            "SELECT COUNT(*) as count, AVG(score) as avg_score FROM parcels",
            versions={"parcels": 1}
        )
        print(result.to_pandas().to_string(index=False))

        # Query version 2 (after score update)
        print("\nQuery: SELECT COUNT(*), AVG(score) FROM parcels AS OF VERSION 2")
        result = engine.query(
            "SELECT COUNT(*) as count, AVG(score) as avg_score FROM parcels",
            versions={"parcels": 2}
        )
        print(result.to_pandas().to_string(index=False))

        # =================================================================
        # STEP 5: Version Comparison
        # =================================================================
        print_section("Step 5: Compare Versions")

        # Compare v1 to v3
        diff = engine.diff_versions("parcels", 1, 3, key_columns=["parcel_id"])
        print("Diff between Version 1 and Version 3:")
        print(f"  Rows in V1: {diff['rows_a']}")
        print(f"  Rows in V3: {diff['rows_b']}")
        print(f"  Rows added: {diff['rows_added']}")
        print(f"  Rows removed: {diff['rows_removed']}")
        print(f"  Schema changed: {diff['schema_changed']}")

        # =================================================================
        # STEP 6: Complex Time Travel Query
        # =================================================================
        print_section("Step 6: Complex Time Travel Query")

        print("Find parcels whose score INCREASED between v1 and v3:")
        print("(This requires querying two versions and comparing)")

        # Get v1 scores
        v1_scores = engine.query(
            "SELECT parcel_id, score as score_v1 FROM parcels",
            versions={"parcels": 1}
        ).to_pandas()

        # Get v3 scores
        v3_scores = engine.query(
            "SELECT parcel_id, score as score_v3 FROM parcels",
            versions={"parcels": 3}
        ).to_pandas()

        # Merge and compare
        comparison = v1_scores.merge(v3_scores, on="parcel_id", how="inner")
        comparison["score_change"] = comparison["score_v3"] - comparison["score_v1"]
        improved = comparison[comparison["score_change"] > 0]

        print("\nParcels with improved scores:")
        print(improved.to_string(index=False))

        # =================================================================
        # STEP 7: Version History
        # =================================================================
        print_section("Step 7: Version History")

        versions = engine.list_versions("parcels")
        print(f"Available versions: {versions}")

        for v in versions:
            info = engine.get_table_info("parcels", version=v)
            print(f"\nVersion {v}:")
            print(f"  Rows: {info['row_count']}")
            print(f"  Chunks: {info['chunk_count']}")
            print(f"  Schema: {list(info['schema'].keys())}")

        # =================================================================
        # Summary
        # =================================================================
        print_section("Summary")

        print("Key capabilities demonstrated:")
        print("  1. Automatic versioning on every write")
        print("  2. Time travel queries to any historical version")
        print("  3. Version comparison and diff")
        print("  4. Content-addressable storage (deduplication)")
        print("  5. SQL queries via DuckDB")
        print("")
        print("Storage efficiency:")
        chunks_size = sum(
            os.path.getsize(os.path.join(root, f))
            for root, _, files in os.walk(chunks_dir)
            for f in files
        )
        print(f"  Total chunk storage: {chunks_size / 1024:.1f} KB")
        print(f"  Versions stored: {len(versions)}")

    finally:
        # Cleanup
        print(f"\nCleaning up {temp_dir}...")
        shutil.rmtree(temp_dir, ignore_errors=True)
        print("Done!")


if __name__ == "__main__":
    main()
