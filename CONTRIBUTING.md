# Contributing to Rhizo

Thank you for your interest in contributing to Rhizo! This document provides guidelines and instructions for contributing.

## Getting Started

### Prerequisites

- **Rust 1.70+**: Install via [rustup](https://rustup.rs/)
- **Python 3.9+**: Required for the query layer and tests
- **maturin**: For building Python bindings (`pip install maturin`)

### Development Setup

```bash
# Clone the repository
git clone https://github.com/aquadantheman/rhizo.git
cd rhizo

# Create a virtual environment
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate

# Install development dependencies
pip install maturin pytest pandas pyarrow duckdb ruff

# Build the Rust extension
cd rhizo_python && maturin develop --release && cd ..

# Install the Python query layer
pip install -e python/

# Verify everything works
cargo test --all
pytest tests/ -v
```

## Development Workflow

### Running Tests

```bash
# Rust tests (204 tests)
cargo test --all

# Python tests (247 tests)
pytest tests/ -v

# Run a specific test file
pytest tests/test_transactions.py -v
```

### Linting

```bash
# Rust linting
cargo clippy --all

# Python linting
python -m ruff check .
```

### Code Style

- **Rust**: Follow standard Rust conventions. Run `cargo clippy` before committing.
- **Python**: Follow PEP 8. Run `ruff check .` before committing.

## Project Structure

```
rhizo/
├── rhizo_core/           # Rust core library
│   └── src/
│       ├── chunk_store/    # Content-addressable storage
│       ├── catalog/        # Versioned file catalog
│       ├── branch/         # Git-like branching
│       ├── transaction/    # Cross-table ACID transactions
│       └── changelog/      # Change tracking
├── rhizo_python/         # PyO3 bindings (builds 'rhizo' module)
├── python/             # Python query layer (rhizo-query)
│   └── rhizo/
├── tests/              # Python test suite
└── examples/           # Usage examples
```

## Making Changes

### For Bug Fixes

1. Create a branch: `git checkout -b fix/description-of-fix`
2. Write a test that reproduces the bug
3. Fix the bug
4. Ensure all tests pass
5. Submit a pull request

### For New Features

1. Open an issue first to discuss the feature
2. Create a branch: `git checkout -b feature/description`
3. Implement the feature with tests
4. Update documentation if needed
5. Submit a pull request

## Pull Request Guidelines

- Keep PRs focused on a single change
- Include tests for new functionality
- Update documentation as needed
- Ensure CI passes (clippy, ruff, all tests)
- Write clear commit messages

## Architecture Notes

### Design Principles

1. **Immutability**: All data is immutable once written
2. **Content Addressing**: Data identified by BLAKE3 hash
3. **Atomic Operations**: Write-to-temp-rename pattern
4. **Layered Architecture**: ChunkStore → FileCatalog → BranchManager → TransactionManager

### Key Concepts

- **ChunkStore**: Content-addressable storage with BLAKE3 hashing
- **FileCatalog**: Versioned table metadata with time travel
- **BranchManager**: Git-like branching with zero-copy semantics
- **TransactionManager**: Cross-table ACID with snapshot isolation

## Questions?

- Open an issue for bugs or feature requests
- Check existing issues before creating new ones

## License

By contributing, you agree that your contributions will be licensed under the MIT License.
