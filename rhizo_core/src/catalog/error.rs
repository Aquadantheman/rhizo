use thiserror::Error;

#[derive(Error, Debug)]
pub enum CatalogError {
    #[error("IO error: {0}")]
    Io(#[from] std::io::Error),

    #[error("JSON error: {0}")]
    Json(#[from] serde_json::Error),

    #[error("Table not found: {0}")]
    TableNotFound(String),

    #[error("Version not found: {0} v{1}")]
    VersionNotFound(String, u64),

    #[error("Invalid version: expected {expected}, got {got}")]
    InvalidVersion { expected: u64, got: u64 },

    #[error("Latest pointer corrupted for table: {0}")]
    LatestPointerCorrupted(String),

    #[error("Failed to acquire file lock for table: {0}")]
    LockError(String),

    #[error("Cannot delete latest version: {0} v{1}")]
    CannotDeleteLatest(String, u64),
}
