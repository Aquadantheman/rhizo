"""
Rhizo - Data, connected. SQL queries over versioned, content-addressable data.

This module provides:
- TableWriter: Write DataFrames as chunked Parquet files
- TableReader: Read and assemble tables from chunks
- QueryEngine: SQL interface with time travel support (DuckDB-based)
- OLAPEngine: High-performance analytical queries (DataFusion-based)
- CacheManager: LRU cache for Arrow tables
- TransactionContext: ACID transactions across multiple tables
- Subscriber: Stream changelog events with polling or callbacks
- ChangeEvent: Individual table change within a transaction
- Filter: Predicate filter builder for pushdown optimization
"""

from .writer import TableWriter
from .reader import TableReader, Filter
from .engine import QueryEngine
from .transaction import TransactionContext
from .subscriber import Subscriber, ChangeEvent
from .cache import CacheManager, CacheKey, CacheStats
from .olap_engine import OLAPEngine, is_datafusion_available

__version__ = "0.1.0"
__all__ = [
    "TableWriter",
    "TableReader",
    "QueryEngine",
    "OLAPEngine",
    "CacheManager",
    "CacheKey",
    "CacheStats",
    "is_datafusion_available",
    "TransactionContext",
    "Subscriber",
    "ChangeEvent",
    "Filter",
]
