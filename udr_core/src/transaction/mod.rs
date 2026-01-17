//! Transaction module for cross-table ACID transactions.
//!
//! This module provides:
//! - `TransactionManager` - Coordinates transactions with conflict detection
//! - `TransactionRecord` - Complete transaction state and metadata
//! - `TransactionLog` - Persistent storage for transaction records
//! - `EpochConfig` / `EpochMetadata` - Epoch-based organization
//! - `ConflictDetector` - Pluggable conflict detection strategies

mod types;
mod epoch;
mod error;
mod log;
mod conflict;
mod manager;
mod recovery;

pub use types::{
    TxId, EpochId, TransactionStatus, WriteGranularity,
    TableWrite, TransactionRecord,
};
pub use epoch::{EpochConfig, EpochStatus, EpochMetadata};
pub use error::TransactionError;
pub use log::TransactionLog;
pub use conflict::{Conflict, ConflictDetector, TableLevelConflictDetector};
pub use manager::TransactionManager;
pub use recovery::{RecoveryReport, RecoveryManager};
