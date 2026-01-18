#!/usr/bin/env python3
"""
Armillaria Demo: Corruption-Proof Storage

This demo shows how content-addressable storage makes your data tamper-evident.

The Problem:
    Traditional file systems have no way to detect corruption:
    - Silent bit rot goes unnoticed for years
    - Accidental overwrites corrupt without warning
    - Malicious tampering leaves no trace

The Armillaria Solution:
    Every chunk is named by its BLAKE3 hash. If even one bit changes,
    the hash won't match. Corruption is impossible to hide.

Run: python examples/corruption_proof_demo.py
"""

import os
import sys
import tempfile
import shutil

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


def wait_for_input(prompt: str = "Press Enter to continue...") -> None:
    """Wait for user input, skip if non-interactive."""
    try:
        input(prompt)
    except EOFError:
        pass


def get_chunk_files(chunks_dir: str) -> list:
    """Get all chunk files in the store."""
    chunks = []
    for root, dirs, files in os.walk(chunks_dir):
        for f in files:
            # Chunk files are named by their hash (64 hex characters)
            if len(f) == 64 and all(c in '0123456789abcdef' for c in f):
                chunks.append(os.path.join(root, f))
    return chunks


def corrupt_file(path: str) -> None:
    """Corrupt a file by flipping some bits."""
    with open(path, 'rb') as f:
        data = bytearray(f.read())

    # Flip some bits in the middle of the file
    if len(data) > 100:
        data[50] ^= 0xFF
        data[51] ^= 0xFF
        data[52] ^= 0xFF

    with open(path, 'wb') as f:
        f.write(data)


def main():
    print(r"""
     _                _ _ _            _
    / \   _ __ _ __ _(_) | | __ _ _ __(_) __ _
   / _ \ | '__| '_ ` _ \ | | / _` | '__| |/ _` |
  / ___ \| |  | | | | | | | | (_| | |  | | (_| |
 /_/   \_\_|  |_| |_| |_|_|_|\__,_|_|  |_|\__,_|

    Corruption-Proof Storage Demo
    "Trust, but verify. Automatically."
    """)

    # Create temporary storage
    base_dir = tempfile.mkdtemp(prefix="rhizo_integrity_demo_")
    chunks_dir = os.path.join(base_dir, "chunks")
    print(f"Storage: {base_dir}")

    try:
        # Initialize Armillaria with integrity verification ENABLED
        store = _rhizo.PyChunkStore(chunks_dir)
        catalog = _rhizo.PyCatalog(os.path.join(base_dir, "catalog"))

        # verify_integrity=True ensures every read checks the hash
        engine = QueryEngine(store, catalog, verify_integrity=True)

        # ============================================================
        print_header("SETUP: Store Important Data")
        # ============================================================

        print("Creating financial records...")

        np.random.seed(42)
        transactions = pd.DataFrame({
            "id": range(1, 1001),
            "account": np.random.randint(1000, 9999, 1000),
            "amount": np.random.uniform(-10000, 10000, 1000).round(2),
            "timestamp": pd.date_range("2024-01-01", periods=1000, freq="h").astype(str)
        })

        engine.write_table("transactions", transactions)

        total_amount = transactions['amount'].sum()
        print("\nStored 1,000 transactions")
        print(f"Total balance: ${total_amount:,.2f}")

        # Show chunk files
        chunk_files = get_chunk_files(chunks_dir)
        print(f"\nChunk files created: {len(chunk_files)}")
        for cf in chunk_files[:3]:
            hash_name = os.path.basename(cf)
            print(f"  {hash_name[:32]}... (BLAKE3 hash)")

        # ============================================================
        print_header("DEMO 1: How Content-Addressable Storage Works")
        # ============================================================

        print("""
Traditional storage:
  File: transactions.parquet
  - Name chosen by user
  - Content can change without warning
  - No way to verify integrity

Content-addressable storage:
  File: b3_abc123def456...parquet
  - Name IS the hash of content
  - If content changes, name won't match
  - Built-in integrity verification
        """)

        wait_for_input("Press Enter to examine chunk naming...")

        print("\nChunk file structure:")
        for cf in chunk_files[:2]:
            hash_name = os.path.basename(cf)
            size = os.path.getsize(cf)
            print(f"\n  Filename: {hash_name[:32]}...")
            print(f"  Full hash: {hash_name}")
            print(f"  Size: {size:,} bytes")
            print("  Rule: filename == BLAKE3(content)")

        # ============================================================
        print_header("DEMO 2: Detecting Corruption")
        # ============================================================

        print("""
Now let's simulate what happens when data gets corrupted.
This could be:
  - Disk failure (bit rot)
  - Accidental overwrite
  - Malicious tampering

We'll corrupt one of the chunk files directly.
        """)

        wait_for_input("Press Enter to corrupt the data...")

        # Pick a chunk to corrupt
        target_chunk = chunk_files[0]
        target_hash = os.path.basename(target_chunk)
        print(f"\nCorrupting chunk: {target_hash[:32]}...")

        corrupt_file(target_chunk)
        print("Bits flipped! Data is now corrupted.")
        print(f"\nThe file still claims to be '{target_hash[:16]}...'")
        print("But its actual content now hashes to something different.")

        # ============================================================
        print_header("DEMO 3: Corruption Detection in Action")
        # ============================================================

        print("""
Now when we try to read the data, Armillaria will:
1. Read the chunk file
2. Compute BLAKE3 hash of what it read
3. Compare against the expected hash (filename)
4. FAIL if they don't match

This is automatic. No special verification step needed.
        """)

        wait_for_input("Press Enter to try reading corrupted data...")

        try:
            result = engine.query("SELECT * FROM transactions LIMIT 1")
            df = result.to_pandas()
            print(f"\nUnexpectedly succeeded! Got {len(df)} rows")
            print("(Note: If we got here, the corrupted chunk wasn't read)")
        except Exception as e:
            error_msg = str(e)
            print("\nCORRUPTION DETECTED!")
            print(f"\nError: {error_msg[:200]}...")
            print("\nArmillaria refused to serve corrupted data.")
            print("Your application KNOWS something is wrong.")

        # ============================================================
        print_header("DEMO 4: The Traditional Alternative")
        # ============================================================

        print("""
Without content-addressable storage, here's what happens:

1. SILENT CORRUPTION
   File: transactions.parquet (corrupted)
   System: "Here's your data!" (corrupted, but looks fine)
   You: Process corrupted data, make bad decisions

2. DELAYED DETECTION (if you're lucky)
   - Checksums in separate file (easy to forget to check)
   - Periodic integrity scans (corruption spreads first)
   - User notices wrong results (too late)

3. NO DETECTION (common)
   - Bit rot goes unnoticed
   - Backup corrupted files
   - Restore corrupted backups
   - Data permanently lost

With Armillaria:
   - Every read is automatically verified
   - Corruption caught immediately
   - No separate integrity checks needed
   - Hash IS the identifier
        """)

        # ============================================================
        print_header("DEMO 5: Recovery with Deduplication")
        # ============================================================

        print("""
Bonus: Content-addressable storage enables easy recovery.

Since identical content always has the same hash:
  - If chunk exists elsewhere (another version, backup), reuse it
  - Deduplication is automatic and exact
  - No fuzzy matching - hash match = byte-perfect identical
        """)

        wait_for_input("Press Enter to restore from a backup...")

        # Write the same data again - will be deduplicated
        engine.write_table("transactions_backup", transactions)

        chunk_files_after = get_chunk_files(chunks_dir)
        print("\nWrote same data to 'transactions_backup'")
        print(f"Chunks before: {len(chunk_files)}")
        print(f"Chunks after:  {len(chunk_files_after)}")
        print(f"New chunks:    {len(chunk_files_after) - len(chunk_files)}")
        print("\n(Minimal new chunks - data was deduplicated!)")

        # ============================================================
        print_header("SUMMARY")
        # ============================================================

        print("""
What we demonstrated:

1. CONTENT-ADDRESSABLE NAMING
   - Chunk filename = BLAKE3 hash of content
   - Name and content are cryptographically linked

2. AUTOMATIC INTEGRITY VERIFICATION
   - Every read verifies the hash
   - No explicit integrity checks needed
   - Corruption detected immediately

3. TAMPER-EVIDENT STORAGE
   - Any modification breaks the hash link
   - Impossible to silently corrupt data
   - Trust is built into the storage layer

4. DEDUPLICATION BONUS
   - Identical content = identical hash
   - Automatic, exact deduplication
   - Enables efficient backup and recovery

This is why Armillaria uses content-addressable storage.
Your data integrity is guaranteed by mathematics, not trust.
        """)

    finally:
        # Cleanup
        shutil.rmtree(base_dir, ignore_errors=True)
        print(f"\nCleaned up: {base_dir}")


if __name__ == "__main__":
    main()
