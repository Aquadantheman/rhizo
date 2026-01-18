use std::io;
use thiserror::Error;

#[derive(Error, Debug)]
pub enum MerkleError {
    #[error("IO error: {0}")]
    Io(#[from] io::Error),

    #[error("Chunk not found: {0}")]
    ChunkNotFound(String),

    #[error("Invalid chunk size: must be > 0, got {0}")]
    InvalidChunkSize(usize),

    #[error("Empty data: cannot build Merkle tree from empty input")]
    EmptyData,

    #[error("Integrity error: expected hash {expected}, got {actual}")]
    IntegrityError { expected: String, actual: String },

    #[error("Tree corruption: {0}")]
    TreeCorruption(String),

    #[error("Serialization error: {0}")]
    Serialization(String),

    #[error("Chunk store error: {0}")]
    ChunkStore(String),
}
