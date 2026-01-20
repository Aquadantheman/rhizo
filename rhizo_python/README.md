# rhizo-core

Low-level Rust bindings for [Rhizo](https://github.com/rhizodata/rhizo).

**Most users should install `rhizo` instead**, which includes these bindings plus the high-level Python API:

```bash
pip install rhizo
```

## What is rhizo-core?

This package provides the native Rust components:

- `PyChunkStore` - Content-addressable storage with BLAKE3 hashing
- `PyCatalog` - Versioned table catalog
- `PyBranchManager` - Git-like branching
- `PyTransactionManager` - Cross-table ACID transactions
- `PyParquetEncoder/Decoder` - High-performance Parquet I/O
- Merkle tree operations for incremental deduplication

## When to use rhizo-core directly

Only if you're building custom tooling on top of the low-level primitives. For normal usage, install `rhizo` which provides a simple API:

```python
import rhizo

db = rhizo.open("./mydata")
db.write("users", df)
result = db.sql("SELECT * FROM users")
```

## License

MIT
