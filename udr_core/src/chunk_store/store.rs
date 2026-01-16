use std::fs;
use std::path::{Path, PathBuf};
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
            let temp_path = chunk_path.with_extension("tmp");
            fs::write(&temp_path, data)?;
            fs::rename(&temp_path, &chunk_path)?;
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
}
