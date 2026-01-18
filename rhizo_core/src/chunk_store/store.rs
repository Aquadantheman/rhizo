use std::fs;
use std::path::{Path, PathBuf};
use rayon::prelude::*;
use memmap2::Mmap;
use super::error::ChunkStoreError;

/// BLAKE3 hashes are 64 hex characters (256 bits)
const EXPECTED_HASH_LEN: usize = 64;

pub struct ChunkStore {
    base_path: PathBuf,
}

impl ChunkStore {
    pub fn new(base_path: impl AsRef<Path>) -> Result<Self, ChunkStoreError> {
        let base_path = base_path.as_ref().to_path_buf();
        fs::create_dir_all(&base_path)?;
        Ok(Self { base_path })
    }

    pub fn put(&self, data: &[u8]) -> Result<String, ChunkStoreError> {
        let hash = blake3::hash(data).to_hex().to_string();
        let chunk_path = self.hash_to_path(&hash)?;

        if !chunk_path.exists() {
            if let Some(parent) = chunk_path.parent() {
                fs::create_dir_all(parent)?;
            }
            // Atomic write: write to temp file then rename
            // Use unique temp file name to avoid collisions in parallel writes
            let temp_name = format!("{}.{}.tmp", hash, uuid::Uuid::new_v4());
            let temp_path = chunk_path.with_file_name(&temp_name);
            fs::write(&temp_path, data)?;

            // Rename may fail if another thread created the chunk first - that's OK
            // because content-addressed storage means both wrote the same data
            match fs::rename(&temp_path, &chunk_path) {
                Ok(()) => {}
                Err(_) if chunk_path.exists() => {
                    // Another thread beat us - clean up our temp file
                    let _ = fs::remove_file(&temp_path);
                    // This is not an error - the chunk exists with correct content
                }
                Err(e) => {
                    // Actual error - clean up and return
                    let _ = fs::remove_file(&temp_path);
                    return Err(ChunkStoreError::Io(e));
                }
            }
        }

        Ok(hash)
    }

    pub fn get(&self, hash: &str) -> Result<Vec<u8>, ChunkStoreError> {
        self.validate_hash(hash)?;
        let chunk_path = self.hash_to_path(hash)?;

        if !chunk_path.exists() {
            return Err(ChunkStoreError::NotFound(hash.to_string()));
        }

        Ok(fs::read(&chunk_path)?)
    }

    /// Get chunk data with integrity verification.
    /// Returns error if the data doesn't hash to the expected value.
    pub fn get_verified(&self, hash: &str) -> Result<Vec<u8>, ChunkStoreError> {
        let data = self.get(hash)?;
        let actual_hash = blake3::hash(&data).to_hex().to_string();

        if actual_hash != hash {
            return Err(ChunkStoreError::HashMismatch {
                expected: hash.to_string(),
                actual: actual_hash,
            });
        }

        Ok(data)
    }

    /// Get a memory-mapped view of a chunk.
    ///
    /// This is faster than `get()` for large chunks because:
    /// 1. No copy from kernel buffer to userspace
    /// 2. OS handles page caching automatically
    /// 3. Can be used with zero-copy parsers
    ///
    /// # Safety
    /// The returned Mmap is read-only and the underlying file is immutable
    /// (content-addressed), so this is safe to use.
    ///
    /// # Arguments
    /// * `hash` - The BLAKE3 hash of the chunk to retrieve
    ///
    /// # Returns
    /// A memory-mapped view of the chunk data
    ///
    /// # Errors
    /// - `ChunkStoreError::NotFound` if the chunk doesn't exist
    /// - `ChunkStoreError::InvalidHash` if the hash format is invalid
    /// - `ChunkStoreError::Io` for other I/O errors
    pub fn get_mmap(&self, hash: &str) -> Result<Mmap, ChunkStoreError> {
        self.validate_hash(hash)?;
        let chunk_path = self.hash_to_path(hash)?;

        let file = std::fs::File::open(&chunk_path).map_err(|e| {
            if e.kind() == std::io::ErrorKind::NotFound {
                ChunkStoreError::NotFound(hash.to_string())
            } else {
                ChunkStoreError::Io(e)
            }
        })?;

        // SAFETY: We're only reading the file, and chunks are immutable once written
        // (content-addressed storage guarantees this)
        unsafe { Mmap::map(&file) }.map_err(ChunkStoreError::Io)
    }

    /// Get memory-mapped views of multiple chunks in parallel.
    ///
    /// Returns results in the same order as input hashes.
    /// If any chunk is not found, returns an error.
    ///
    /// # Arguments
    /// * `hashes` - Slice of hash strings to retrieve
    ///
    /// # Returns
    /// Vector of memory-mapped views in the same order as input hashes
    pub fn get_mmap_batch(&self, hashes: &[&str]) -> Result<Vec<Mmap>, ChunkStoreError> {
        hashes
            .par_iter()
            .map(|hash| self.get_mmap(hash))
            .collect()
    }

    pub fn exists(&self, hash: &str) -> Result<bool, ChunkStoreError> {
        self.validate_hash(hash)?;
        Ok(self.hash_to_path(hash)?.exists())
    }

    pub fn delete(&self, hash: &str) -> Result<(), ChunkStoreError> {
        self.validate_hash(hash)?;
        let chunk_path = self.hash_to_path(hash)?;

        if chunk_path.exists() {
            fs::remove_file(&chunk_path)?;
        }

        Ok(())
    }

    // =========================================================================
    // Batch Operations (Parallel)
    // =========================================================================

    /// Store multiple chunks in parallel, returning their hashes.
    ///
    /// This is significantly faster than calling `put()` in a loop because:
    /// 1. BLAKE3 hashing runs in parallel across CPU cores
    /// 2. Disk I/O is parallelized
    /// 3. Single function call overhead instead of N calls
    ///
    /// # Arguments
    /// * `chunks` - Slice of byte slices to store
    ///
    /// # Returns
    /// Vector of hashes in the same order as input chunks
    ///
    /// # Example
    /// ```
    /// # use rhizo_core::ChunkStore;
    /// # let dir = std::env::temp_dir().join("batch_example");
    /// # std::fs::create_dir_all(&dir).unwrap();
    /// let store = ChunkStore::new(&dir).unwrap();
    /// let chunks: Vec<&[u8]> = vec![b"chunk1", b"chunk2", b"chunk3"];
    /// let hashes = store.put_batch(&chunks).unwrap();
    /// assert_eq!(hashes.len(), 3);
    /// # std::fs::remove_dir_all(&dir).ok();
    /// ```
    pub fn put_batch(&self, chunks: &[&[u8]]) -> Result<Vec<String>, ChunkStoreError> {
        chunks
            .par_iter()
            .map(|data| self.put(data))
            .collect()
    }

    /// Retrieve multiple chunks in parallel by their hashes.
    ///
    /// Returns results in the same order as input hashes.
    /// If any chunk is not found, returns an error.
    ///
    /// # Arguments
    /// * `hashes` - Slice of hash strings to retrieve
    ///
    /// # Returns
    /// Vector of chunk data in the same order as input hashes
    ///
    /// # Example
    /// ```
    /// # use rhizo_core::ChunkStore;
    /// # let dir = std::env::temp_dir().join("get_batch_example");
    /// # std::fs::create_dir_all(&dir).unwrap();
    /// let store = ChunkStore::new(&dir).unwrap();
    /// let h1 = store.put(b"data1").unwrap();
    /// let h2 = store.put(b"data2").unwrap();
    /// let results = store.get_batch(&[&h1, &h2]).unwrap();
    /// assert_eq!(results[0], b"data1");
    /// assert_eq!(results[1], b"data2");
    /// # std::fs::remove_dir_all(&dir).ok();
    /// ```
    pub fn get_batch(&self, hashes: &[&str]) -> Result<Vec<Vec<u8>>, ChunkStoreError> {
        hashes
            .par_iter()
            .map(|hash| self.get(hash))
            .collect()
    }

    /// Retrieve multiple chunks with verification in parallel.
    ///
    /// Like `get_batch`, but verifies each chunk's integrity by
    /// comparing its content hash to the expected hash.
    ///
    /// # Arguments
    /// * `hashes` - Slice of hash strings to retrieve and verify
    ///
    /// # Returns
    /// Vector of verified chunk data in the same order as input hashes
    ///
    /// # Errors
    /// Returns `ChunkStoreError::HashMismatch` if any chunk fails verification
    pub fn get_batch_verified(&self, hashes: &[&str]) -> Result<Vec<Vec<u8>>, ChunkStoreError> {
        hashes
            .par_iter()
            .map(|hash| self.get_verified(hash))
            .collect()
    }

    /// Validate that a hash string is properly formatted.
    fn validate_hash(&self, hash: &str) -> Result<(), ChunkStoreError> {
        if hash.len() != EXPECTED_HASH_LEN {
            return Err(ChunkStoreError::InvalidHash(format!(
                "expected {} characters, got {}",
                EXPECTED_HASH_LEN,
                hash.len()
            )));
        }
        if !hash.chars().all(|c| c.is_ascii_hexdigit()) {
            return Err(ChunkStoreError::InvalidHash(
                "hash must contain only hexadecimal characters".to_string()
            ));
        }
        Ok(())
    }

    fn hash_to_path(&self, hash: &str) -> Result<PathBuf, ChunkStoreError> {
        // For internal use after put(), we trust the hash is valid
        // For external use, validate_hash should be called first
        if hash.len() < 4 {
            return Err(ChunkStoreError::InvalidHash(format!(
                "hash too short: {} characters",
                hash.len()
            )));
        }
        Ok(self.base_path
            .join(&hash[0..2])
            .join(&hash[2..4])
            .join(hash))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use std::io::Write;

    fn temp_dir() -> PathBuf {
        let dir = std::env::temp_dir().join(format!("udr_test_{}", uuid::Uuid::new_v4()));
        fs::create_dir_all(&dir).unwrap();
        dir
    }

    // A valid-format hash for testing (64 hex chars)
    fn fake_valid_hash() -> String {
        "a".repeat(EXPECTED_HASH_LEN)
    }

    #[test]
    fn test_put_get_roundtrip() {
        let dir = temp_dir();
        let store = ChunkStore::new(&dir).unwrap();

        let data = b"hello world";
        let hash = store.put(data).unwrap();
        let retrieved = store.get(&hash).unwrap();

        assert_eq!(data.to_vec(), retrieved);
        fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn test_deduplication() {
        let dir = temp_dir();
        let store = ChunkStore::new(&dir).unwrap();

        let data = b"same data";
        let hash1 = store.put(data).unwrap();
        let hash2 = store.put(data).unwrap();

        assert_eq!(hash1, hash2);
        fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn test_not_found() {
        let dir = temp_dir();
        let store = ChunkStore::new(&dir).unwrap();

        // Use a valid-format hash that doesn't exist
        let result = store.get(&fake_valid_hash());
        assert!(matches!(result, Err(ChunkStoreError::NotFound(_))));
        fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn test_exists() {
        let dir = temp_dir();
        let store = ChunkStore::new(&dir).unwrap();

        let hash = store.put(b"test data").unwrap();
        assert!(store.exists(&hash).unwrap());
        assert!(!store.exists(&fake_valid_hash()).unwrap());
        fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn test_delete() {
        let dir = temp_dir();
        let store = ChunkStore::new(&dir).unwrap();

        let hash = store.put(b"to be deleted").unwrap();
        assert!(store.exists(&hash).unwrap());

        store.delete(&hash).unwrap();
        assert!(!store.exists(&hash).unwrap());
        fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn test_invalid_hash_too_short() {
        let dir = temp_dir();
        let store = ChunkStore::new(&dir).unwrap();

        let result = store.get("abc");
        assert!(matches!(result, Err(ChunkStoreError::InvalidHash(_))));

        let result = store.exists("abc");
        assert!(matches!(result, Err(ChunkStoreError::InvalidHash(_))));

        let result = store.delete("abc");
        assert!(matches!(result, Err(ChunkStoreError::InvalidHash(_))));

        fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn test_invalid_hash_wrong_length() {
        let dir = temp_dir();
        let store = ChunkStore::new(&dir).unwrap();

        // 32 chars instead of 64
        let short_hash = "a".repeat(32);
        let result = store.get(&short_hash);
        assert!(matches!(result, Err(ChunkStoreError::InvalidHash(_))));

        fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn test_invalid_hash_non_hex() {
        let dir = temp_dir();
        let store = ChunkStore::new(&dir).unwrap();

        // Contains 'g' which is not hex
        let bad_hash = "g".repeat(EXPECTED_HASH_LEN);
        let result = store.get(&bad_hash);
        assert!(matches!(result, Err(ChunkStoreError::InvalidHash(_))));

        fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn test_get_verified_success() {
        let dir = temp_dir();
        let store = ChunkStore::new(&dir).unwrap();

        let data = b"verified data";
        let hash = store.put(data).unwrap();
        let retrieved = store.get_verified(&hash).unwrap();

        assert_eq!(data.to_vec(), retrieved);
        fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn test_get_verified_detects_corruption() {
        let dir = temp_dir();
        let store = ChunkStore::new(&dir).unwrap();

        let data = b"original data";
        let hash = store.put(data).unwrap();

        // Corrupt the file directly
        let chunk_path = store.hash_to_path(&hash).unwrap();
        {
            let mut file = fs::OpenOptions::new()
                .write(true)
                .truncate(true)
                .open(&chunk_path)
                .unwrap();
            file.write_all(b"corrupted!").unwrap();
        }

        // Regular get returns corrupted data
        let result = store.get(&hash).unwrap();
        assert_eq!(result, b"corrupted!");

        // get_verified catches the corruption
        let result = store.get_verified(&hash);
        assert!(matches!(result, Err(ChunkStoreError::HashMismatch { .. })));

        fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn test_large_chunk() {
        let dir = temp_dir();
        let store = ChunkStore::new(&dir).unwrap();

        // 10MB of data
        let data: Vec<u8> = (0..10_000_000).map(|i| (i % 256) as u8).collect();
        let hash = store.put(&data).unwrap();
        let retrieved = store.get(&hash).unwrap();

        assert_eq!(data.len(), retrieved.len());
        assert_eq!(data, retrieved);
        fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn test_empty_chunk() {
        let dir = temp_dir();
        let store = ChunkStore::new(&dir).unwrap();

        let data = b"";
        let hash = store.put(data).unwrap();
        let retrieved = store.get(&hash).unwrap();

        assert_eq!(data.to_vec(), retrieved);
        fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn test_binary_data() {
        let dir = temp_dir();
        let store = ChunkStore::new(&dir).unwrap();

        // Binary data with null bytes and all byte values
        let data: Vec<u8> = (0..=255).collect();
        let hash = store.put(&data).unwrap();
        let retrieved = store.get(&hash).unwrap();

        assert_eq!(data, retrieved);
        fs::remove_dir_all(&dir).ok();
    }

    // =========================================================================
    // Batch Operation Tests
    // =========================================================================

    #[test]
    fn test_put_batch_empty() {
        let dir = temp_dir();
        let store = ChunkStore::new(&dir).unwrap();

        let chunks: Vec<&[u8]> = vec![];
        let hashes = store.put_batch(&chunks).unwrap();

        assert!(hashes.is_empty());
        fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn test_put_batch_single() {
        let dir = temp_dir();
        let store = ChunkStore::new(&dir).unwrap();

        let data = b"single chunk";
        let chunks: Vec<&[u8]> = vec![data];
        let hashes = store.put_batch(&chunks).unwrap();

        assert_eq!(hashes.len(), 1);
        // Verify we can retrieve it
        let retrieved = store.get(&hashes[0]).unwrap();
        assert_eq!(retrieved, data);
        fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn test_put_batch_multiple() {
        let dir = temp_dir();
        let store = ChunkStore::new(&dir).unwrap();

        let data1 = b"chunk one";
        let data2 = b"chunk two";
        let data3 = b"chunk three";
        let chunks: Vec<&[u8]> = vec![data1, data2, data3];
        let hashes = store.put_batch(&chunks).unwrap();

        assert_eq!(hashes.len(), 3);

        // All chunks should be retrievable and in order
        assert_eq!(store.get(&hashes[0]).unwrap(), data1);
        assert_eq!(store.get(&hashes[1]).unwrap(), data2);
        assert_eq!(store.get(&hashes[2]).unwrap(), data3);
        fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn test_put_batch_deduplication() {
        let dir = temp_dir();
        let store = ChunkStore::new(&dir).unwrap();

        // Same data appears multiple times
        let data = b"duplicate data";
        let chunks: Vec<&[u8]> = vec![data, data, data];
        let hashes = store.put_batch(&chunks).unwrap();

        assert_eq!(hashes.len(), 3);
        // All hashes should be identical (deduplication)
        assert_eq!(hashes[0], hashes[1]);
        assert_eq!(hashes[1], hashes[2]);
        fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn test_put_batch_large() {
        let dir = temp_dir();
        let store = ChunkStore::new(&dir).unwrap();

        // Create 100 unique chunks
        let chunks_data: Vec<Vec<u8>> = (0..100)
            .map(|i| format!("chunk number {}", i).into_bytes())
            .collect();
        let chunks: Vec<&[u8]> = chunks_data.iter().map(|c| c.as_slice()).collect();

        let hashes = store.put_batch(&chunks).unwrap();

        assert_eq!(hashes.len(), 100);
        // Verify all chunks stored correctly
        for (i, hash) in hashes.iter().enumerate() {
            let retrieved = store.get(hash).unwrap();
            assert_eq!(retrieved, chunks_data[i]);
        }
        fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn test_get_batch_empty() {
        let dir = temp_dir();
        let store = ChunkStore::new(&dir).unwrap();

        let hashes: Vec<&str> = vec![];
        let results = store.get_batch(&hashes).unwrap();

        assert!(results.is_empty());
        fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn test_get_batch_single() {
        let dir = temp_dir();
        let store = ChunkStore::new(&dir).unwrap();

        let data = b"single chunk for get";
        let hash = store.put(data).unwrap();

        let results = store.get_batch(&[&hash]).unwrap();

        assert_eq!(results.len(), 1);
        assert_eq!(results[0], data);
        fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn test_get_batch_multiple() {
        let dir = temp_dir();
        let store = ChunkStore::new(&dir).unwrap();

        let data1 = b"data one";
        let data2 = b"data two";
        let data3 = b"data three";

        let hash1 = store.put(data1).unwrap();
        let hash2 = store.put(data2).unwrap();
        let hash3 = store.put(data3).unwrap();

        let results = store.get_batch(&[&hash1, &hash2, &hash3]).unwrap();

        assert_eq!(results.len(), 3);
        assert_eq!(results[0], data1);
        assert_eq!(results[1], data2);
        assert_eq!(results[2], data3);
        fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn test_get_batch_not_found() {
        let dir = temp_dir();
        let store = ChunkStore::new(&dir).unwrap();

        let data = b"existing chunk";
        let hash = store.put(data).unwrap();
        let fake_hash = fake_valid_hash();

        // Mix of existing and non-existing
        let result = store.get_batch(&[&hash, &fake_hash]);

        assert!(result.is_err());
        assert!(matches!(result, Err(ChunkStoreError::NotFound(_))));
        fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn test_get_batch_verified() {
        let dir = temp_dir();
        let store = ChunkStore::new(&dir).unwrap();

        let data1 = b"verified one";
        let data2 = b"verified two";

        let hash1 = store.put(data1).unwrap();
        let hash2 = store.put(data2).unwrap();

        let results = store.get_batch_verified(&[&hash1, &hash2]).unwrap();

        assert_eq!(results.len(), 2);
        assert_eq!(results[0], data1);
        assert_eq!(results[1], data2);
        fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn test_put_batch_get_batch_roundtrip() {
        let dir = temp_dir();
        let store = ChunkStore::new(&dir).unwrap();

        // Create varied chunk sizes
        let chunks_data: Vec<Vec<u8>> = vec![
            vec![1, 2, 3],
            vec![],  // empty chunk
            (0..1000).map(|i| (i % 256) as u8).collect(),  // 1KB
            (0..10000).map(|i| (i % 256) as u8).collect(), // 10KB
        ];
        let chunks: Vec<&[u8]> = chunks_data.iter().map(|c| c.as_slice()).collect();

        // Put all chunks
        let hashes = store.put_batch(&chunks).unwrap();

        // Get all chunks back
        let hash_refs: Vec<&str> = hashes.iter().map(|s| s.as_str()).collect();
        let results = store.get_batch(&hash_refs).unwrap();

        // Verify roundtrip
        assert_eq!(results.len(), chunks_data.len());
        for (i, result) in results.iter().enumerate() {
            assert_eq!(result, &chunks_data[i], "Chunk {} mismatch", i);
        }
        fs::remove_dir_all(&dir).ok();
    }

    // =========================================================================
    // Memory-Mapped I/O Tests
    // =========================================================================

    #[test]
    fn test_get_mmap_basic() {
        let dir = temp_dir();
        let store = ChunkStore::new(&dir).unwrap();

        let data = b"memory mapped data";
        let hash = store.put(data).unwrap();

        let mmap = store.get_mmap(&hash).unwrap();

        // Mmap should contain the same data
        assert_eq!(&mmap[..], data);
        fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn test_get_mmap_large() {
        let dir = temp_dir();
        let store = ChunkStore::new(&dir).unwrap();

        // 1MB of data
        let data: Vec<u8> = (0..1_000_000).map(|i| (i % 256) as u8).collect();
        let hash = store.put(&data).unwrap();

        let mmap = store.get_mmap(&hash).unwrap();

        assert_eq!(mmap.len(), data.len());
        assert_eq!(&mmap[..], &data[..]);
        fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn test_get_mmap_not_found() {
        let dir = temp_dir();
        let store = ChunkStore::new(&dir).unwrap();

        let result = store.get_mmap(&fake_valid_hash());
        assert!(matches!(result, Err(ChunkStoreError::NotFound(_))));
        fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn test_get_mmap_invalid_hash() {
        let dir = temp_dir();
        let store = ChunkStore::new(&dir).unwrap();

        let result = store.get_mmap("invalid");
        assert!(matches!(result, Err(ChunkStoreError::InvalidHash(_))));
        fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn test_get_mmap_batch() {
        let dir = temp_dir();
        let store = ChunkStore::new(&dir).unwrap();

        let data1 = b"mmap batch one";
        let data2 = b"mmap batch two";
        let data3 = b"mmap batch three";

        let hash1 = store.put(data1).unwrap();
        let hash2 = store.put(data2).unwrap();
        let hash3 = store.put(data3).unwrap();

        let mmaps = store.get_mmap_batch(&[&hash1, &hash2, &hash3]).unwrap();

        assert_eq!(mmaps.len(), 3);
        assert_eq!(&mmaps[0][..], data1);
        assert_eq!(&mmaps[1][..], data2);
        assert_eq!(&mmaps[2][..], data3);
        fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn test_get_mmap_batch_empty() {
        let dir = temp_dir();
        let store = ChunkStore::new(&dir).unwrap();

        let hashes: Vec<&str> = vec![];
        let mmaps = store.get_mmap_batch(&hashes).unwrap();

        assert!(mmaps.is_empty());
        fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn test_get_mmap_vs_get_same_content() {
        let dir = temp_dir();
        let store = ChunkStore::new(&dir).unwrap();

        let data = b"compare mmap vs get";
        let hash = store.put(data).unwrap();

        let via_get = store.get(&hash).unwrap();
        let via_mmap = store.get_mmap(&hash).unwrap();

        // Both should return identical content
        assert_eq!(via_get, &via_mmap[..]);
        fs::remove_dir_all(&dir).ok();
    }
}
