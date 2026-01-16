use thiserror::Error;

#[derive(Error, Debug)]
pub enum ChunkStoreError {
    #[error("IO error: {0}")]
    Io(#[from] std::io::Error),

    #[error("Chunk not found: {0}")]
    NotFound(String),

    #[error("Invalid hash: {0}")]
    InvalidHash(String),

    #[error("Hash mismatch: expected {expected}, got {actual}")]
    HashMismatch { expected: String, actual: String },
}
