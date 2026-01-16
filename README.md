# Unified Data Runtime (UDR)

A high-performance, content-addressable data storage system with versioned catalogs, written in Rust with Python bindings.

## Overview

UDR provides a foundation for building data systems that need:
- **Content-addressable storage** - Automatic deduplication via BLAKE3 hashing
- **Immutable versioning** - Git-like snapshots with full history preservation
- **Time travel** - Access any historical version of your data
- **Integrity verification** - Optional hash verification on reads

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                      Application Layer                       │
│                    (Python / Rust / CLI)                     │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                        FileCatalog                           │
│   • Versioned table metadata                                 │
│   • Sequential version enforcement                           │
│   • Time travel queries                                      │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                        ChunkStore                            │
│   • Content-addressed storage (BLAKE3)                       │
│   • Automatic deduplication                                  │
│   • Atomic writes                                            │
│   • Integrity verification                                   │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                       File System                            │
│   • 2-level directory tree for O(1) lookups                 │
│   • JSON metadata files                                      │
└─────────────────────────────────────────────────────────────┘
```

## Features

### Phase 1 (Current)
- [x] Content-addressable chunk store with BLAKE3 hashing
- [x] Versioned file catalog with sequential version enforcement
- [x] Time travel (access any historical version)
- [x] Python bindings via PyO3
- [x] Atomic writes (write-to-temp-rename pattern)
- [x] Hash validation and integrity verification
- [x] Comprehensive test coverage

### Planned
- [ ] Query layer with DuckDB integration
- [ ] Branching support
- [ ] Transaction support with MVCC
- [ ] Changelog and subscriptions
- [ ] Garbage collection

## Installation

### Prerequisites
- Rust 1.70+ (`rustup install stable`)
- Python 3.8+ (for Python bindings)
- maturin (`pip install maturin`)

### Build from source

```bash
# Clone the repository
git clone https://github.com/Aquadantheman/unifieddataruntime.git
cd unifieddataruntime

# Build and test Rust code
cargo build --release
cargo test --all

# Build Python bindings (development mode)
cd udr_python
maturin develop --release
```

## Usage

### Rust

```rust
use udr_core::{ChunkStore, FileCatalog, TableVersion};

// Create a chunk store
let store = ChunkStore::new("./data/chunks")?;

// Store data and get content hash
let hash = store.put(b"Hello, World!")?;
println!("Stored with hash: {}", hash);

// Retrieve data
let data = store.get(&hash)?;

// Or with integrity verification
let data = store.get_verified(&hash)?;

// Create a versioned catalog
let catalog = FileCatalog::new("./data/catalog")?;

// Commit a version
let version = TableVersion::new("users", 1, vec![hash.clone()]);
catalog.commit(version)?;

// Time travel to any version
let v1 = catalog.get_version("users", Some(1))?;
let latest = catalog.get_version("users", None)?;  // Gets latest

// List all versions
let versions = catalog.list_versions("users")?;

// List all tables
let tables = catalog.list_tables()?;
```

### Python

```python
import udr

# Create a chunk store
store = udr.PyChunkStore("./data/chunks")

# Store data
hash_str = store.put(b"Hello, World!")
print(f"Stored with hash: {hash_str}")

# Retrieve data
data = store.get(hash_str)

# Or with integrity verification
data = store.get_verified(hash_str)

# Check if chunk exists
if store.exists(hash_str):
    print("Chunk exists!")

# Create a versioned catalog
catalog = udr.PyCatalog("./data/catalog")

# Commit a version
version = udr.PyTableVersion("users", 1, [hash_str])
catalog.commit(version)

# Time travel to any version
v1 = catalog.get_version("users", 1)
latest = catalog.get_version("users")  # Gets latest

# List all versions and tables
versions = catalog.list_versions("users")
tables = catalog.list_tables()
```

## Testing

```bash
# Run Rust tests
cargo test --all

# Run Python tests (after building with maturin develop)
pytest tests/test_udr.py -v
```

## Project Structure

```
unifieddataruntime/
├── Cargo.toml              # Workspace configuration
├── README.md               # This file
├── udr_core/               # Rust core library
│   ├── Cargo.toml
│   └── src/
│       ├── lib.rs          # Public exports
│       ├── chunk_store/    # Content-addressable storage
│       │   ├── mod.rs
│       │   ├── store.rs    # ChunkStore implementation
│       │   └── error.rs    # Error types
│       └── catalog/        # Versioned catalog
│           ├── mod.rs
│           ├── file_catalog.rs  # FileCatalog implementation
│           ├── version.rs       # TableVersion struct
│           └── error.rs         # Error types
├── udr_python/             # Python bindings (PyO3)
│   ├── Cargo.toml
│   └── src/
│       └── lib.rs          # PyO3 bindings
├── tests/                  # Integration tests
│   └── test_udr.py         # Python tests
├── data/                   # Default data directory
│   ├── chunks/             # Chunk storage
│   └── catalog/            # Catalog metadata
└── udr_roadmap.md          # Development roadmap
```

## Design Principles

1. **Immutability**: All data is immutable once written. Updates create new versions.
2. **Content Addressing**: Data is identified by its BLAKE3 hash, enabling automatic deduplication.
3. **Atomic Operations**: All writes use the write-to-temp-rename pattern to prevent corruption.
4. **Error Handling**: Explicit `Result` types throughout; no panics in library code.
5. **Layered Architecture**: ChunkStore and FileCatalog are independent and composable.

## Performance Characteristics

| Operation | Complexity | Notes |
|-----------|------------|-------|
| Write chunk | O(n) | n = data size; hashing + file write |
| Read chunk | O(n) | n = data size; file read |
| Check exists | O(1) | Path lookup |
| Commit version | O(k) | k = number of chunk hashes |
| Get version | O(1) | Single file read |
| Time travel | O(1) | Direct version lookup |

## License

MIT

## Contributing

Contributions welcome! Please read the development roadmap (`udr_roadmap.md`) for planned features and architecture decisions.
