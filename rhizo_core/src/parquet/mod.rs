//! Native Parquet encoding and decoding.
//!
//! This module provides high-performance Parquet operations using Rust's
//! arrow-rs and parquet crates, eliminating Python GIL contention and
//! enabling parallel processing with Rayon.
//!
//! # Architecture
//!
//! ```text
//! Python DataFrame --> PyArrow Table --[zero-copy FFI]--> Rust Arrow
//!                                                              |
//!                                                              v
//!                                          ParquetEncoder (parallel with Rayon)
//!                                                              |
//!                                                              v
//!                                                        Parquet bytes
//! ```
//!
//! # Performance
//!
//! Moving Parquet encoding to Rust provides:
//! - No Python GIL contention during encoding
//! - Parallel encoding of multiple batches with Rayon
//! - Zero-copy data transfer with pyo3-arrow
//!
//! # Example
//!
//! ```ignore
//! use rhizo_core::parquet::{ParquetEncoder, ParquetDecoder};
//!
//! // Encode
//! let encoder = ParquetEncoder::new();
//! let parquet_bytes = encoder.encode(&record_batch)?;
//!
//! // Decode
//! let decoder = ParquetDecoder::new();
//! let batch = decoder.decode(&parquet_bytes)?;
//! ```

mod decoder;
mod encoder;
mod error;
mod filter;

pub use decoder::ParquetDecoder;
pub use encoder::{ParquetEncoder, ParquetCompression};
pub use error::ParquetError;
pub use filter::{FilterOp, ScalarValue, PredicateFilter};
