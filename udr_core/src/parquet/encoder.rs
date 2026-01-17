//! Parquet encoding for Arrow data.
//!
//! This module provides high-performance Parquet encoding using the Rust
//! parquet crate, with support for parallel batch encoding via Rayon.

use std::str::FromStr;

use arrow::record_batch::RecordBatch;
use parquet::arrow::ArrowWriter;
use parquet::basic::Compression;
use parquet::file::properties::WriterProperties;
use rayon::prelude::*;

use super::error::ParquetError;

/// Compression options for Parquet encoding.
#[derive(Debug, Clone, Copy, Default)]
pub enum ParquetCompression {
    /// No compression
    Uncompressed,
    /// Snappy compression (fast, moderate ratio)
    Snappy,
    /// Gzip compression (slow, good ratio)
    Gzip,
    /// LZ4 compression (very fast, moderate ratio)
    Lz4,
    /// Zstd compression (good balance of speed and ratio)
    #[default]
    Zstd,
}

impl ParquetCompression {
    /// Convert to parquet crate's Compression type.
    fn to_parquet_compression(self) -> Compression {
        match self {
            ParquetCompression::Uncompressed => Compression::UNCOMPRESSED,
            ParquetCompression::Snappy => Compression::SNAPPY,
            ParquetCompression::Gzip => Compression::GZIP(Default::default()),
            ParquetCompression::Lz4 => Compression::LZ4,
            ParquetCompression::Zstd => Compression::ZSTD(Default::default()),
        }
    }
}

impl FromStr for ParquetCompression {
    type Err = ParquetError;

    fn from_str(s: &str) -> Result<Self, Self::Err> {
        match s.to_lowercase().as_str() {
            "none" | "uncompressed" => Ok(ParquetCompression::Uncompressed),
            "snappy" => Ok(ParquetCompression::Snappy),
            "gzip" => Ok(ParquetCompression::Gzip),
            "lz4" => Ok(ParquetCompression::Lz4),
            "zstd" => Ok(ParquetCompression::Zstd),
            _ => Err(ParquetError::InvalidCompression(s.to_string())),
        }
    }
}

/// High-performance Parquet encoder.
///
/// Converts Arrow RecordBatches to Parquet bytes with configurable compression.
/// Supports parallel batch encoding via Rayon for multiple chunks.
#[derive(Debug, Clone)]
pub struct ParquetEncoder {
    compression: ParquetCompression,
    write_statistics: bool,
}

impl Default for ParquetEncoder {
    fn default() -> Self {
        Self {
            compression: ParquetCompression::Zstd,
            write_statistics: true,
        }
    }
}

impl ParquetEncoder {
    /// Create a new encoder with default settings (Zstd compression, statistics enabled).
    pub fn new() -> Self {
        Self::default()
    }

    /// Create an encoder with specified compression.
    pub fn with_compression(compression: ParquetCompression) -> Self {
        Self {
            compression,
            ..Default::default()
        }
    }

    /// Set whether to write column statistics (for predicate pushdown).
    pub fn with_statistics(mut self, enabled: bool) -> Self {
        self.write_statistics = enabled;
        self
    }

    /// Encode a single Arrow RecordBatch to Parquet bytes.
    ///
    /// # Arguments
    /// * `batch` - The Arrow RecordBatch to encode
    ///
    /// # Returns
    /// * `Ok(Vec<u8>)` - The Parquet-encoded bytes
    /// * `Err(ParquetError)` - If encoding fails
    ///
    /// # Example
    /// ```ignore
    /// use arrow::array::{Int64Array, StringArray};
    /// use arrow::record_batch::RecordBatch;
    /// use udr_core::parquet::ParquetEncoder;
    ///
    /// let ids = Int64Array::from(vec![1, 2, 3]);
    /// let names = StringArray::from(vec!["Alice", "Bob", "Charlie"]);
    /// let batch = RecordBatch::try_from_iter(vec![
    ///     ("id", Arc::new(ids) as _),
    ///     ("name", Arc::new(names) as _),
    /// ]).unwrap();
    ///
    /// let encoder = ParquetEncoder::new();
    /// let bytes = encoder.encode(&batch).unwrap();
    /// ```
    pub fn encode(&self, batch: &RecordBatch) -> Result<Vec<u8>, ParquetError> {
        if batch.num_rows() == 0 {
            return Err(ParquetError::EmptyData);
        }

        let mut buffer = Vec::new();
        let props = WriterProperties::builder()
            .set_compression(self.compression.to_parquet_compression())
            .set_statistics_enabled(
                if self.write_statistics {
                    parquet::file::properties::EnabledStatistics::Chunk
                } else {
                    parquet::file::properties::EnabledStatistics::None
                }
            )
            .build();

        let mut writer = ArrowWriter::try_new(&mut buffer, batch.schema(), Some(props))?;
        writer.write(batch)?;
        writer.close()?;

        Ok(buffer)
    }

    /// Encode multiple RecordBatches in parallel using Rayon.
    ///
    /// This is significantly faster than encoding sequentially when you have
    /// multiple batches (e.g., multiple chunks of a table).
    ///
    /// # Arguments
    /// * `batches` - Slice of RecordBatches to encode
    ///
    /// # Returns
    /// * `Ok(Vec<Vec<u8>>)` - Parquet bytes for each batch, in same order
    /// * `Err(ParquetError)` - If any encoding fails (returns first error)
    pub fn encode_batch(&self, batches: &[RecordBatch]) -> Result<Vec<Vec<u8>>, ParquetError> {
        batches
            .par_iter()
            .map(|batch| self.encode(batch))
            .collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use arrow::array::{Int64Array, Float64Array, StringArray};
    use arrow::datatypes::{DataType, Field, Schema};
    use std::sync::Arc;

    fn create_test_batch(num_rows: usize) -> RecordBatch {
        let schema = Schema::new(vec![
            Field::new("id", DataType::Int64, false),
            Field::new("value", DataType::Float64, false),
            Field::new("name", DataType::Utf8, true),
        ]);

        let ids: Vec<i64> = (0..num_rows as i64).collect();
        let values: Vec<f64> = (0..num_rows).map(|i| i as f64 * 1.5).collect();
        let names: Vec<Option<&str>> = (0..num_rows)
            .map(|i| if i % 2 == 0 { Some("test") } else { None })
            .collect();

        RecordBatch::try_new(
            Arc::new(schema),
            vec![
                Arc::new(Int64Array::from(ids)),
                Arc::new(Float64Array::from(values)),
                Arc::new(StringArray::from(names)),
            ],
        )
        .unwrap()
    }

    #[test]
    fn test_encode_simple_batch() {
        let batch = create_test_batch(100);
        let encoder = ParquetEncoder::new();

        let bytes = encoder.encode(&batch).unwrap();

        // Verify we got non-empty bytes
        assert!(!bytes.is_empty());
        // Parquet magic bytes: PAR1
        assert_eq!(&bytes[0..4], b"PAR1");
    }

    #[test]
    fn test_encode_empty_batch_fails() {
        let schema = Schema::new(vec![Field::new("id", DataType::Int64, false)]);
        let batch = RecordBatch::try_new(
            Arc::new(schema),
            vec![Arc::new(Int64Array::from(Vec::<i64>::new()))],
        )
        .unwrap();

        let encoder = ParquetEncoder::new();
        let result = encoder.encode(&batch);

        assert!(matches!(result, Err(ParquetError::EmptyData)));
    }

    #[test]
    fn test_encode_with_different_compressions() {
        let batch = create_test_batch(1000);

        let compressions = [
            ParquetCompression::Uncompressed,
            ParquetCompression::Snappy,
            ParquetCompression::Zstd,
        ];

        let mut sizes = Vec::new();
        for compression in compressions {
            let encoder = ParquetEncoder::with_compression(compression);
            let bytes = encoder.encode(&batch).unwrap();
            sizes.push(bytes.len());
            // All should produce valid Parquet
            assert_eq!(&bytes[0..4], b"PAR1");
        }

        // Uncompressed should be largest
        assert!(sizes[0] > sizes[2], "Uncompressed should be larger than Zstd");
    }

    #[test]
    fn test_encode_batch_parallel() {
        let batches: Vec<RecordBatch> = (0..10)
            .map(|_| create_test_batch(1000))
            .collect();

        let encoder = ParquetEncoder::new();
        let results = encoder.encode_batch(&batches).unwrap();

        assert_eq!(results.len(), 10);
        for bytes in &results {
            assert!(!bytes.is_empty());
            assert_eq!(&bytes[0..4], b"PAR1");
        }
    }

    #[test]
    fn test_encode_large_batch() {
        let batch = create_test_batch(100_000);
        let encoder = ParquetEncoder::new();

        let bytes = encoder.encode(&batch).unwrap();

        assert!(!bytes.is_empty());
        assert_eq!(&bytes[0..4], b"PAR1");
        // Should be significantly compressed
        // 100K rows * ~20 bytes per row = ~2MB uncompressed
        // Zstd should compress to much less
        assert!(bytes.len() < 1_000_000, "Should compress to < 1MB");
    }

    #[test]
    fn test_compression_from_str() {
        assert!(matches!(
            ParquetCompression::from_str("zstd").unwrap(),
            ParquetCompression::Zstd
        ));
        assert!(matches!(
            ParquetCompression::from_str("ZSTD").unwrap(),
            ParquetCompression::Zstd
        ));
        assert!(matches!(
            ParquetCompression::from_str("snappy").unwrap(),
            ParquetCompression::Snappy
        ));
        assert!(ParquetCompression::from_str("invalid").is_err());
    }

    #[test]
    fn test_encoder_builder_pattern() {
        let encoder = ParquetEncoder::with_compression(ParquetCompression::Snappy)
            .with_statistics(false);

        assert!(!encoder.write_statistics);
    }
}
