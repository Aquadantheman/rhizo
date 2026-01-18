use super::error::MerkleError;
use super::types::{DataChunk, MerkleConfig, MerkleDiff, MerkleNode, MerkleTree};
use std::collections::HashSet;

/// Build a Merkle tree from raw data bytes
pub fn build_tree(data: &[u8], config: &MerkleConfig) -> Result<MerkleTree, MerkleError> {
    if data.is_empty() {
        return Err(MerkleError::EmptyData);
    }
    if config.chunk_size == 0 {
        return Err(MerkleError::InvalidChunkSize(0));
    }

    // Step 1: Split data into chunks and compute leaf hashes
    let chunks = split_into_chunks(data, config.chunk_size);

    if chunks.is_empty() {
        return Err(MerkleError::EmptyData);
    }

    // Step 2: Build tree bottom-up
    let (root_hash, internal_nodes, height) = build_tree_from_leaves(&chunks, config.branching_factor);

    Ok(MerkleTree {
        root_hash,
        chunks,
        internal_nodes,
        total_size: data.len() as u64,
        chunk_size: config.chunk_size,
        height,
    })
}

/// Split data into fixed-size chunks and compute their hashes
fn split_into_chunks(data: &[u8], chunk_size: usize) -> Vec<DataChunk> {
    let mut chunks = Vec::new();
    let mut offset = 0usize;
    let mut index = 0usize;

    while offset < data.len() {
        let end = (offset + chunk_size).min(data.len());
        let chunk_data = &data[offset..end];
        let hash = blake3::hash(chunk_data).to_hex().to_string();

        chunks.push(DataChunk::new(
            hash,
            offset as u64,
            end as u64,
            index,
        ));

        offset = end;
        index += 1;
    }

    chunks
}

/// Build tree from leaf chunks, returning (root_hash, internal_nodes, height)
fn build_tree_from_leaves(
    chunks: &[DataChunk],
    branching_factor: usize,
) -> (String, Vec<Vec<MerkleNode>>, u32) {
    if chunks.is_empty() {
        return (String::new(), Vec::new(), 0);
    }

    if chunks.len() == 1 {
        // Single chunk - it is the root
        return (chunks[0].hash.clone(), Vec::new(), 1);
    }

    // Current level hashes (start with leaf hashes)
    let mut current_level: Vec<String> = chunks.iter().map(|c| c.hash.clone()).collect();
    let mut internal_nodes: Vec<Vec<MerkleNode>> = Vec::new();
    let mut level = 1u32;

    // Build up until we have a single root
    while current_level.len() > 1 {
        let mut next_level = Vec::new();
        let mut level_nodes = Vec::new();

        for (i, chunk) in current_level.chunks(branching_factor).enumerate() {
            // Concatenate child hashes and hash the result
            let combined: String = chunk.join("");
            let parent_hash = blake3::hash(combined.as_bytes()).to_hex().to_string();

            level_nodes.push(MerkleNode::new(
                parent_hash.clone(),
                chunk.to_vec(),
                level,
                i,
            ));

            next_level.push(parent_hash);
        }

        internal_nodes.push(level_nodes);
        current_level = next_level;
        level += 1;
    }

    let root_hash = current_level.into_iter().next().unwrap_or_default();
    (root_hash, internal_nodes, level)
}

/// Compare two Merkle trees and find differences
pub fn diff_trees(old: &MerkleTree, new: &MerkleTree) -> MerkleDiff {
    let old_hashes: HashSet<&String> = old.chunks.iter().map(|c| &c.hash).collect();
    let new_hashes: HashSet<&String> = new.chunks.iter().map(|c| &c.hash).collect();

    let unchanged: Vec<String> = old_hashes
        .intersection(&new_hashes)
        .map(|h| (*h).clone())
        .collect();

    let removed: Vec<String> = old_hashes
        .difference(&new_hashes)
        .map(|h| (*h).clone())
        .collect();

    let added: Vec<String> = new_hashes
        .difference(&old_hashes)
        .map(|h| (*h).clone())
        .collect();

    let mut diff = MerkleDiff {
        unchanged_chunks: unchanged,
        removed_chunks: removed,
        added_chunks: added,
        reuse_ratio: 0.0,
    };

    diff.calculate_reuse_ratio(old.chunks.len(), new.chunks.len());
    diff
}

/// Verify integrity of a Merkle tree by recomputing hashes
pub fn verify_tree(tree: &MerkleTree, get_chunk_data: impl Fn(&str) -> Option<Vec<u8>>) -> Result<bool, MerkleError> {
    // Verify each leaf chunk
    for chunk in &tree.chunks {
        let data = get_chunk_data(&chunk.hash)
            .ok_or_else(|| MerkleError::ChunkNotFound(chunk.hash.clone()))?;

        let computed_hash = blake3::hash(&data).to_hex().to_string();
        if computed_hash != chunk.hash {
            return Err(MerkleError::IntegrityError {
                expected: chunk.hash.clone(),
                actual: computed_hash,
            });
        }
    }

    // Verify internal nodes bottom-up
    let mut current_level: Vec<String> = tree.chunks.iter().map(|c| c.hash.clone()).collect();

    for level_nodes in &tree.internal_nodes {
        let mut next_level = Vec::new();

        for node in level_nodes {
            let combined: String = node.children.join("");
            let computed_hash = blake3::hash(combined.as_bytes()).to_hex().to_string();

            if computed_hash != node.hash {
                return Err(MerkleError::IntegrityError {
                    expected: node.hash.clone(),
                    actual: computed_hash,
                });
            }

            next_level.push(node.hash.clone());
        }

        current_level = next_level;
    }

    // Verify root
    if current_level.len() == 1 && current_level[0] == tree.root_hash {
        Ok(true)
    } else if tree.internal_nodes.is_empty() && tree.chunks.len() == 1 {
        // Single chunk tree
        Ok(tree.chunks[0].hash == tree.root_hash)
    } else {
        Err(MerkleError::TreeCorruption(
            "Root hash does not match computed value".to_string(),
        ))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn default_config() -> MerkleConfig {
        MerkleConfig::new(1024) // 1KB chunks for testing
    }

    #[test]
    fn test_build_tree_single_chunk() {
        let data = b"hello world";
        let config = MerkleConfig::new(1024);
        let tree = build_tree(data, &config).unwrap();

        assert_eq!(tree.chunks.len(), 1);
        assert_eq!(tree.total_size, 11);
        assert_eq!(tree.height, 1);
        assert_eq!(tree.root_hash, tree.chunks[0].hash);
    }

    #[test]
    fn test_build_tree_multiple_chunks() {
        // 3KB of data with 1KB chunks = 3 chunks
        let data: Vec<u8> = (0..3072).map(|i| (i % 256) as u8).collect();
        let config = MerkleConfig::new(1024);
        let tree = build_tree(&data, &config).unwrap();

        assert_eq!(tree.chunks.len(), 3);
        assert_eq!(tree.total_size, 3072);
        assert!(tree.height > 1);
        // Root hash should be different from any leaf
        assert!(tree.chunks.iter().all(|c| c.hash != tree.root_hash));
    }

    #[test]
    fn test_build_tree_deterministic() {
        let data = b"deterministic test data";
        let config = default_config();

        let tree1 = build_tree(data, &config).unwrap();
        let tree2 = build_tree(data, &config).unwrap();

        assert_eq!(tree1.root_hash, tree2.root_hash);
        assert_eq!(tree1.chunks, tree2.chunks);
    }

    #[test]
    fn test_build_tree_empty_data() {
        let data = b"";
        let config = default_config();
        let result = build_tree(data, &config);

        assert!(matches!(result, Err(MerkleError::EmptyData)));
    }

    #[test]
    fn test_build_tree_invalid_chunk_size() {
        let data = b"test";
        let config = MerkleConfig::new(0);
        let result = build_tree(data, &config);

        assert!(matches!(result, Err(MerkleError::InvalidChunkSize(0))));
    }

    #[test]
    fn test_diff_identical_trees() {
        let data = b"identical data for both trees";
        let config = default_config();

        let tree1 = build_tree(data, &config).unwrap();
        let tree2 = build_tree(data, &config).unwrap();

        let diff = diff_trees(&tree1, &tree2);

        assert_eq!(diff.unchanged_chunks.len(), tree1.chunks.len());
        assert!(diff.removed_chunks.is_empty());
        assert!(diff.added_chunks.is_empty());
        assert!((diff.reuse_ratio - 1.0).abs() < 0.001);
    }

    #[test]
    fn test_diff_completely_different_trees() {
        // Use unique patterns that don't repeat within chunk boundaries
        // Chunk 1: hash of index creates unique content
        let data1: Vec<u8> = (0..2048u64)
            .flat_map(|i| blake3::hash(&i.to_le_bytes()).as_bytes()[0..1].to_vec())
            .collect();
        let data2: Vec<u8> = (2048..4096u64)
            .flat_map(|i| blake3::hash(&i.to_le_bytes()).as_bytes()[0..1].to_vec())
            .collect();
        let config = MerkleConfig::new(1024);

        let tree1 = build_tree(&data1, &config).unwrap();
        let tree2 = build_tree(&data2, &config).unwrap();

        // Verify chunks are unique within each tree
        let unique1: std::collections::HashSet<_> = tree1.chunks.iter().map(|c| &c.hash).collect();
        let unique2: std::collections::HashSet<_> = tree2.chunks.iter().map(|c| &c.hash).collect();
        assert_eq!(unique1.len(), tree1.chunks.len(), "tree1 should have unique chunks");
        assert_eq!(unique2.len(), tree2.chunks.len(), "tree2 should have unique chunks");

        let diff = diff_trees(&tree1, &tree2);

        assert!(diff.unchanged_chunks.is_empty());
        assert_eq!(diff.removed_chunks.len(), tree1.chunks.len());
        assert_eq!(diff.added_chunks.len(), tree2.chunks.len());
        assert!((diff.reuse_ratio - 0.0).abs() < 0.001);
    }

    #[test]
    fn test_diff_partial_change() {
        // Create 4KB data with unique chunks using hash-based content
        let data1: Vec<u8> = (0..4096u64)
            .flat_map(|i| blake3::hash(&i.to_le_bytes()).as_bytes()[0..1].to_vec())
            .collect();

        // Clone and modify only the last chunk (bytes 3072-4095)
        let mut data2 = data1.clone();
        for i in 3072..4096 {
            data2[i] = data2[i].wrapping_add(1);
        }

        let config = MerkleConfig::new(1024);
        let tree1 = build_tree(&data1, &config).unwrap();
        let tree2 = build_tree(&data2, &config).unwrap();

        // Verify all 4 chunks are unique in tree1
        let unique1: std::collections::HashSet<_> = tree1.chunks.iter().map(|c| &c.hash).collect();
        assert_eq!(unique1.len(), 4, "tree1 should have 4 unique chunks");

        let diff = diff_trees(&tree1, &tree2);

        // 3 out of 4 chunks should be unchanged
        assert_eq!(diff.unchanged_chunks.len(), 3);
        assert_eq!(diff.removed_chunks.len(), 1);
        assert_eq!(diff.added_chunks.len(), 1);
        assert!((diff.reuse_ratio - 0.75).abs() < 0.001);
    }

    #[test]
    fn test_verify_tree_valid() {
        let data: Vec<u8> = (0..2048).map(|i| (i % 256) as u8).collect();
        let config = MerkleConfig::new(1024);
        let tree = build_tree(&data, &config).unwrap();

        // Create a mock data provider
        let chunk_data: std::collections::HashMap<String, Vec<u8>> = tree
            .chunks
            .iter()
            .map(|c| {
                let start = c.byte_range.0 as usize;
                let end = c.byte_range.1 as usize;
                (c.hash.clone(), data[start..end].to_vec())
            })
            .collect();

        let result = verify_tree(&tree, |hash| chunk_data.get(hash).cloned());
        assert!(result.is_ok());
        assert!(result.unwrap());
    }

    #[test]
    fn test_verify_tree_corrupted_chunk() {
        let data: Vec<u8> = (0..2048).map(|i| (i % 256) as u8).collect();
        let config = MerkleConfig::new(1024);
        let tree = build_tree(&data, &config).unwrap();

        // Create corrupted data for one chunk
        let mut chunk_data: std::collections::HashMap<String, Vec<u8>> = tree
            .chunks
            .iter()
            .map(|c| {
                let start = c.byte_range.0 as usize;
                let end = c.byte_range.1 as usize;
                (c.hash.clone(), data[start..end].to_vec())
            })
            .collect();

        // Corrupt the first chunk
        if let Some(first_hash) = tree.chunks.first().map(|c| c.hash.clone()) {
            if let Some(chunk) = chunk_data.get_mut(&first_hash) {
                chunk[0] = chunk[0].wrapping_add(1);
            }
        }

        let result = verify_tree(&tree, |hash| chunk_data.get(hash).cloned());
        assert!(matches!(result, Err(MerkleError::IntegrityError { .. })));
    }

    #[test]
    fn test_chunk_for_offset() {
        let data: Vec<u8> = (0..3072).map(|i| (i % 256) as u8).collect();
        let config = MerkleConfig::new(1024);
        let tree = build_tree(&data, &config).unwrap();

        // Offset 0 should be in chunk 0
        let chunk = tree.chunk_for_offset(0).unwrap();
        assert_eq!(chunk.index, 0);

        // Offset 1023 should still be in chunk 0
        let chunk = tree.chunk_for_offset(1023).unwrap();
        assert_eq!(chunk.index, 0);

        // Offset 1024 should be in chunk 1
        let chunk = tree.chunk_for_offset(1024).unwrap();
        assert_eq!(chunk.index, 1);

        // Offset 2500 should be in chunk 2
        let chunk = tree.chunk_for_offset(2500).unwrap();
        assert_eq!(chunk.index, 2);

        // Offset beyond data should return None
        assert!(tree.chunk_for_offset(5000).is_none());
    }

    #[test]
    fn test_chunks_in_range() {
        let data: Vec<u8> = (0..4096).map(|i| (i % 256) as u8).collect();
        let config = MerkleConfig::new(1024);
        let tree = build_tree(&data, &config).unwrap();

        // Range within one chunk
        let chunks = tree.chunks_in_range(100, 500);
        assert_eq!(chunks.len(), 1);
        assert_eq!(chunks[0].index, 0);

        // Range spanning two chunks
        let chunks = tree.chunks_in_range(900, 1100);
        assert_eq!(chunks.len(), 2);

        // Range spanning all chunks
        let chunks = tree.chunks_in_range(0, 4096);
        assert_eq!(chunks.len(), 4);
    }

    #[test]
    fn test_tree_height() {
        let config = MerkleConfig::new(1024);

        // 1 chunk = height 1
        let tree = build_tree(&[0u8; 512], &config).unwrap();
        assert_eq!(tree.height, 1);

        // 2 chunks = height 2 (leaves + root)
        let tree = build_tree(&[0u8; 2048], &config).unwrap();
        assert_eq!(tree.height, 2);

        // 4 chunks = height 3
        let tree = build_tree(&[0u8; 4096], &config).unwrap();
        assert_eq!(tree.height, 3);

        // 8 chunks = height 4
        let tree = build_tree(&[0u8; 8192], &config).unwrap();
        assert_eq!(tree.height, 4);
    }

    #[test]
    fn test_large_data_performance() {
        // 1MB of data
        let data: Vec<u8> = (0..1_000_000).map(|i| (i % 256) as u8).collect();
        let config = MerkleConfig::new(64 * 1024); // 64KB chunks

        let start = std::time::Instant::now();
        let tree = build_tree(&data, &config).unwrap();
        let elapsed = start.elapsed();

        // Should complete in reasonable time
        assert!(elapsed.as_millis() < 1000, "Tree building took too long: {:?}", elapsed);

        // Should have ~16 chunks (1MB / 64KB)
        assert!(tree.chunks.len() >= 15 && tree.chunks.len() <= 17);
    }

    #[test]
    fn test_deduplication_ratio_calculation() {
        // Simulate 5% change scenario
        let _chunk_count = 100;

        // Old tree has 100 chunks
        // New tree has 100 chunks, 95 unchanged, 5 new
        let mut diff = MerkleDiff::new();
        diff.unchanged_chunks = (0..95).map(|i| format!("hash_{}", i)).collect();
        diff.removed_chunks = (95..100).map(|i| format!("old_hash_{}", i)).collect();
        diff.added_chunks = (0..5).map(|i| format!("new_hash_{}", i)).collect();

        diff.calculate_reuse_ratio(100, 100);

        // Should be 95% reuse
        assert!((diff.reuse_ratio - 0.95).abs() < 0.001);
    }
}
