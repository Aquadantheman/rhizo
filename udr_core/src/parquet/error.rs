//! Error types for Parquet encoding/decoding.

use thiserror::Error;

/// Errors that can occur during Parquet operations.
#[derive(Error, Debug)]
pub enum ParquetError {
    /// Arrow error during conversion
    #[error("Arrow error: {0}")]
    Arrow(#[from] arrow::error::ArrowError),

    /// Parquet encoding/decoding error
    #[error("Parquet error: {0}")]
    Parquet(#[from] parquet::errors::ParquetError),

    /// Empty input data
    #[error("Cannot encode empty data")]
    EmptyData,

    /// Invalid compression type
    #[error("Invalid compression: {0}")]
    InvalidCompression(String),

    /// Invalid column name or index
    #[error("Invalid column: {0}")]
    InvalidColumn(String),
}
