use serde::{Deserialize, Serialize};

/// A leaf node in the Merkle tree - contains actual data
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct DataChunk {
    /// BLAKE3 hash of the chunk content
    pub hash: String,
    /// Byte offset in the original data [start, end)
    pub byte_range: (u64, u64),
    /// Size in bytes
    pub size: u64,
    /// Chunk index (0-based)
    pub index: usize,
}

impl DataChunk {
    pub fn new(hash: String, start: u64, end: u64, index: usize) -> Self {
        Self {
            hash,
            byte_range: (start, end),
            size: end - start,
            index,
        }
    }
}

/// Internal node in the Merkle tree
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct MerkleNode {
    /// BLAKE3 hash of concatenated child hashes
    pub hash: String,
    /// Hashes of child nodes (2 for binary tree, but can vary)
    pub children: Vec<String>,
    /// Level in tree (0 = leaves, increases toward root)
    pub level: u32,
    /// Index at this level
    pub index: usize,
}

impl MerkleNode {
    pub fn new(hash: String, children: Vec<String>, level: u32, index: usize) -> Self {
        Self {
            hash,
            children,
            level,
            index,
        }
    }

    /// Create a leaf node reference (points to a DataChunk)
    pub fn leaf(chunk_hash: String, index: usize) -> Self {
        Self {
            hash: chunk_hash.clone(),
            children: vec![chunk_hash],
            level: 0,
            index,
        }
    }
}

/// Complete Merkle tree for a data blob
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct MerkleTree {
    /// Root hash - the identity of this tree
    pub root_hash: String,
    /// All leaf chunks (the actual data references)
    pub chunks: Vec<DataChunk>,
    /// Internal nodes by level (level 0 is not stored, it's the chunks)
    /// Level 1 is parents of chunks, level 2 is grandparents, etc.
    pub internal_nodes: Vec<Vec<MerkleNode>>,
    /// Total data size in bytes
    pub total_size: u64,
    /// Chunk size used for splitting
    pub chunk_size: usize,
    /// Tree height (number of levels including root)
    pub height: u32,
}

impl MerkleTree {
    /// Get all chunk hashes (for storage)
    pub fn chunk_hashes(&self) -> Vec<String> {
        self.chunks.iter().map(|c| c.hash.clone()).collect()
    }

    /// Get the chunk containing a specific byte offset
    pub fn chunk_for_offset(&self, offset: u64) -> Option<&DataChunk> {
        self.chunks.iter().find(|c| {
            offset >= c.byte_range.0 && offset < c.byte_range.1
        })
    }

    /// Get chunks that overlap with a byte range
    pub fn chunks_in_range(&self, start: u64, end: u64) -> Vec<&DataChunk> {
        self.chunks.iter().filter(|c| {
            // Overlaps if: chunk_start < range_end AND chunk_end > range_start
            c.byte_range.0 < end && c.byte_range.1 > start
        }).collect()
    }
}

/// Result of comparing two Merkle trees
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct MerkleDiff {
    /// Chunks that are identical (same hash)
    pub unchanged_chunks: Vec<String>,
    /// Chunks only in the old tree (removed)
    pub removed_chunks: Vec<String>,
    /// Chunks only in the new tree (added)
    pub added_chunks: Vec<String>,
    /// Percentage of data that was reused
    pub reuse_ratio: f64,
}

impl MerkleDiff {
    pub fn new() -> Self {
        Self {
            unchanged_chunks: Vec::new(),
            removed_chunks: Vec::new(),
            added_chunks: Vec::new(),
            reuse_ratio: 0.0,
        }
    }

    /// Calculate storage efficiency
    pub fn calculate_reuse_ratio(&mut self, _old_chunk_count: usize, new_chunk_count: usize) {
        if new_chunk_count == 0 {
            self.reuse_ratio = 1.0;
            return;
        }
        // Reuse = unchanged / new total
        self.reuse_ratio = self.unchanged_chunks.len() as f64 / new_chunk_count as f64;
    }
}

impl Default for MerkleDiff {
    fn default() -> Self {
        Self::new()
    }
}

/// Configuration for Merkle tree building
#[derive(Debug, Clone)]
pub struct MerkleConfig {
    /// Target chunk size in bytes (default: 64KB)
    pub chunk_size: usize,
    /// Branching factor for tree (default: 2 for binary)
    pub branching_factor: usize,
}

impl Default for MerkleConfig {
    fn default() -> Self {
        Self {
            chunk_size: 64 * 1024, // 64 KB
            branching_factor: 2,
        }
    }
}

impl MerkleConfig {
    pub fn new(chunk_size: usize) -> Self {
        Self {
            chunk_size,
            ..Default::default()
        }
    }

    pub fn with_branching_factor(mut self, factor: usize) -> Self {
        self.branching_factor = factor;
        self
    }
}
