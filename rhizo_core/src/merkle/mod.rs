//! Merkle Tree Storage Module
//!
//! This module provides content-addressed storage with Merkle tree structure,
//! enabling efficient incremental deduplication and integrity verification.
//!
//! # Key Features
//!
//! - **O(log n) storage per change**: Only changed chunks need to be stored
//! - **Automatic deduplication**: Identical chunks share storage
//! - **Integrity verification**: Complete data verification via root hash
//! - **Efficient diffing**: Compare versions to find changes
//!
//! # Example
//!
//! ```rust
//! use rhizo_core::merkle::{build_tree, diff_trees, MerkleConfig};
//!
//! // Build a tree from data - use unique bytes per chunk
//! let config = MerkleConfig::new(1024); // 1KB chunks
//! let data: Vec<u8> = (0..4096u32)
//!     .map(|i| ((i / 1024) as u8).wrapping_add((i % 256) as u8))
//!     .collect();
//! let tree = build_tree(&data, &config).unwrap();
//! assert_eq!(tree.chunks.len(), 4); // 4 distinct chunks
//!
//! // Modify first chunk only
//! let mut new_data = data.clone();
//! for i in 0..1024 {
//!     new_data[i] = 255; // Change first chunk entirely
//! }
//! let new_tree = build_tree(&new_data, &config).unwrap();
//!
//! // Compare: 3 unchanged, 1 added (75% reuse)
//! let diff = diff_trees(&tree, &new_tree);
//! assert_eq!(diff.unchanged_chunks.len(), 3);
//! assert_eq!(diff.added_chunks.len(), 1);
//! assert!(diff.reuse_ratio > 0.7); // ~75% reuse
//! ```

mod error;
mod types;
mod tree;

pub use error::MerkleError;
pub use types::{DataChunk, MerkleConfig, MerkleDiff, MerkleNode, MerkleTree};
pub use tree::{build_tree, diff_trees, verify_tree};
