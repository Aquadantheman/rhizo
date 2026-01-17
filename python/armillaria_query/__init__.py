"""
Armillaria Query Layer - SQL queries over versioned, content-addressable data.

This module provides:
- TableWriter: Write DataFrames as chunked Parquet files
- TableReader: Read and assemble tables from chunks
- QueryEngine: SQL interface with time travel support
- TransactionContext: ACID transactions across multiple tables
- Subscriber: Stream changelog events with polling or callbacks
- ChangeEvent: Individual table change within a transaction
"""

from .writer import TableWriter
from .reader import TableReader
from .engine import QueryEngine
from .transaction import TransactionContext
from .subscriber import Subscriber, ChangeEvent

__version__ = "0.1.0"
__all__ = [
    "TableWriter",
    "TableReader",
    "QueryEngine",
    "TransactionContext",
    "Subscriber",
    "ChangeEvent",
]
