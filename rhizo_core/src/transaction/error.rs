//! Error types for the transaction system.

use thiserror::Error;
use super::types::TxId;

/// Errors that can occur during transaction operations
#[derive(Error, Debug)]
pub enum TransactionError {
    /// I/O error during file operations
    #[error("IO error: {0}")]
    Io(#[from] std::io::Error),

    /// JSON serialization/deserialization error
    #[error("JSON error: {0}")]
    Json(#[from] serde_json::Error),

    /// Transaction not found in active set or log
    #[error("Transaction not found: {0}")]
    TransactionNotFound(TxId),

    /// Attempted operation on non-active transaction
    #[error("Transaction {0} is not active")]
    TransactionNotActive(TxId),

    /// Attempted to commit already committed transaction
    #[error("Transaction {0} already committed")]
    AlreadyCommitted(TxId),

    /// Attempted to modify already aborted transaction
    #[error("Transaction {0} already aborted")]
    AlreadyAborted(TxId),

    /// Write-write conflict detected between transactions
    #[error("Write-write conflict on tables: {0:?}")]
    WriteConflict(Vec<String>),

    /// Snapshot conflict: table was modified since transaction started
    #[error("Snapshot conflict: table {table} was modified (read v{read_version}, now v{current_version})")]
    SnapshotConflict {
        table: String,
        read_version: u64,
        current_version: u64,
    },

    /// Attempted operation on non-active epoch
    #[error("Epoch {0} is not active")]
    EpochNotActive(u64),

    /// Epoch not found
    #[error("Epoch {0} not found")]
    EpochNotFound(u64),

    /// Epoch is full (max transactions reached)
    #[error("Epoch {0} is full (max {1} transactions)")]
    EpochFull(u64, u64),

    /// Invalid transaction state transition
    #[error("Invalid transaction state: expected {expected}, got {actual}")]
    InvalidState {
        expected: String,
        actual: String,
    },

    /// Error from catalog operations
    #[error("Catalog error: {0}")]
    CatalogError(String),

    /// Error from branch operations
    #[error("Branch error: {0}")]
    BranchError(String),

    /// Error during recovery
    #[error("Recovery error: {0}")]
    RecoveryError(String),

    /// Transaction timeout
    #[error("Transaction {0} timed out after {1}ms")]
    Timeout(TxId, u64),

    /// Nested transactions not supported
    #[error("Nested transactions not supported")]
    NestedTransaction,

    /// Invalid transaction configuration
    #[error("Invalid configuration: {0}")]
    InvalidConfig(String),

    /// Lock acquisition failed
    #[error("Failed to acquire lock: {0}")]
    LockError(String),

    /// Data integrity check failed (checksum mismatch)
    #[error("Integrity error: {0}")]
    IntegrityError(String),
}

impl TransactionError {
    /// Check if this error indicates a conflict
    pub fn is_conflict(&self) -> bool {
        matches!(
            self,
            TransactionError::WriteConflict(_) | TransactionError::SnapshotConflict { .. }
        )
    }

    /// Check if this error is retryable
    pub fn is_retryable(&self) -> bool {
        matches!(
            self,
            TransactionError::WriteConflict(_)
                | TransactionError::SnapshotConflict { .. }
                | TransactionError::Timeout(_, _)
                | TransactionError::LockError(_)
        )
    }

    /// Create a write conflict error
    pub fn write_conflict(tables: Vec<String>) -> Self {
        TransactionError::WriteConflict(tables)
    }

    /// Create a snapshot conflict error
    pub fn snapshot_conflict(table: impl Into<String>, read_version: u64, current_version: u64) -> Self {
        TransactionError::SnapshotConflict {
            table: table.into(),
            read_version,
            current_version,
        }
    }

    /// Create a catalog error
    pub fn catalog_error(msg: impl Into<String>) -> Self {
        TransactionError::CatalogError(msg.into())
    }

    /// Create a branch error
    pub fn branch_error(msg: impl Into<String>) -> Self {
        TransactionError::BranchError(msg.into())
    }

    /// Create a recovery error
    pub fn recovery_error(msg: impl Into<String>) -> Self {
        TransactionError::RecoveryError(msg.into())
    }
}

/// Result type alias for transaction operations
/// (Planned for broader use in future phases)
#[allow(dead_code)]
pub type TxResult<T> = Result<T, TransactionError>;

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_error_display() {
        let err = TransactionError::TransactionNotFound(42);
        assert_eq!(err.to_string(), "Transaction not found: 42");

        let err = TransactionError::WriteConflict(vec!["users".to_string(), "orders".to_string()]);
        assert!(err.to_string().contains("users"));
        assert!(err.to_string().contains("orders"));
    }

    #[test]
    fn test_snapshot_conflict_display() {
        let err = TransactionError::snapshot_conflict("users", 5, 7);
        let msg = err.to_string();
        assert!(msg.contains("users"));
        assert!(msg.contains("5"));
        assert!(msg.contains("7"));
    }

    #[test]
    fn test_is_conflict() {
        assert!(TransactionError::WriteConflict(vec!["users".to_string()]).is_conflict());
        assert!(TransactionError::snapshot_conflict("users", 1, 2).is_conflict());
        assert!(!TransactionError::TransactionNotFound(1).is_conflict());
    }

    #[test]
    fn test_is_retryable() {
        assert!(TransactionError::WriteConflict(vec!["users".to_string()]).is_retryable());
        assert!(TransactionError::snapshot_conflict("users", 1, 2).is_retryable());
        assert!(TransactionError::Timeout(1, 5000).is_retryable());
        assert!(!TransactionError::TransactionNotFound(1).is_retryable());
        assert!(!TransactionError::AlreadyCommitted(1).is_retryable());
    }

    #[test]
    fn test_error_constructors() {
        let err = TransactionError::catalog_error("Table not found");
        assert!(matches!(err, TransactionError::CatalogError(_)));

        let err = TransactionError::branch_error("Branch not found");
        assert!(matches!(err, TransactionError::BranchError(_)));

        let err = TransactionError::recovery_error("Corrupted log");
        assert!(matches!(err, TransactionError::RecoveryError(_)));
    }

    #[test]
    fn test_io_error_conversion() {
        let io_err = std::io::Error::new(std::io::ErrorKind::NotFound, "file not found");
        let tx_err: TransactionError = io_err.into();
        assert!(matches!(tx_err, TransactionError::Io(_)));
    }
}
