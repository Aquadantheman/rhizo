use std::fs;
use std::path::{Path, PathBuf};
use super::error::ChunkStoreError;

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
        let chunk_path = self.hash_to_path(&hash);
        
        if !chunk_path.exists() {
            if let Some(parent) = chunk_path.parent() {
                fs::create_dir_all(parent)?;
            }
            fs::write(&chunk_path, data)?;
        }
        
        Ok(hash)
    }

    pub fn get(&self, hash: &str) -> Result<Vec<u8>, ChunkStoreError> {
        let chunk_path = self.hash_to_path(hash);
        
        if !chunk_path.exists() {
            return Err(ChunkStoreError::NotFound(hash.to_string()));
        }
        
        Ok(fs::read(&chunk_path)?)
    }

    pub fn exists(&self, hash: &str) -> bool {
        self.hash_to_path(hash).exists()
    }

    pub fn delete(&self, hash: &str) -> Result<(), ChunkStoreError> {
        let chunk_path = self.hash_to_path(hash);
        
        if chunk_path.exists() {
            fs::remove_file(&chunk_path)?;
        }
        
        Ok(())
    }

    fn hash_to_path(&self, hash: &str) -> PathBuf {
        self.base_path
            .join(&hash[0..2])
            .join(&hash[2..4])
            .join(hash)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;

    fn temp_dir() -> PathBuf {
        let dir = std::env::temp_dir().join(format!("udr_test_{}", uuid::Uuid::new_v4()));
        fs::create_dir_all(&dir).unwrap();
        dir
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
        
        let result = store.get("nonexistent_hash");
        assert!(matches!(result, Err(ChunkStoreError::NotFound(_))));
        fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn test_exists() {
        let dir = temp_dir();
        let store = ChunkStore::new(&dir).unwrap();
        
        let hash = store.put(b"test data").unwrap();
        assert!(store.exists(&hash));
        assert!(!store.exists("fake_hash"));
        fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn test_delete() {
        let dir = temp_dir();
        let store = ChunkStore::new(&dir).unwrap();
        
        let hash = store.put(b"to be deleted").unwrap();
        assert!(store.exists(&hash));
        
        store.delete(&hash).unwrap();
        assert!(!store.exists(&hash));
        fs::remove_dir_all(&dir).ok();
    }
}
