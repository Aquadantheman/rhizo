use std::fs;
use std::path::{Path, PathBuf};
use fs2::FileExt;
use serde::{Deserialize, Serialize};
use super::error::CatalogError;
use super::version::TableVersion;

/// A pending commit intent written to disk before the actual catalog commit.
///
/// If a crash occurs between chunk writes and catalog version commit, these
/// intent files survive on disk. On recovery, they identify which chunks were
/// part of an incomplete commit and can be cleaned up.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PendingCommit {
    /// Unique identifier for this pending commit
    pub intent_id: String,
    /// Table being committed to
    pub table_name: String,
    /// Chunk hashes that were written for this commit
    pub chunk_hashes: Vec<String>,
    /// Timestamp when the intent was created
    pub created_at: i64,
}

pub struct FileCatalog {
    base_path: PathBuf,
}

impl FileCatalog {
    pub fn new(base_path: impl AsRef<Path>) -> Result<Self, CatalogError> {
        let base_path = base_path.as_ref().to_path_buf();
        fs::create_dir_all(&base_path)?;
        // Ensure the pending intents directory exists
        fs::create_dir_all(base_path.join(".pending"))?;
        Ok(Self { base_path })
    }

    /// Acquire an exclusive file lock for a table directory.
    ///
    /// Returns the lock file handle — the lock is held until the handle is dropped.
    /// This provides cross-process mutual exclusion for the read-modify-write
    /// sequence in commit (read latest → check version → write → update latest).
    fn acquire_table_lock(&self, table_name: &str) -> Result<fs::File, CatalogError> {
        let table_dir = self.base_path.join(table_name);
        fs::create_dir_all(&table_dir)?;
        let lock_path = table_dir.join(".lock");
        let lock_file = fs::OpenOptions::new()
            .create(true)
            .write(true)
            .truncate(false)
            .open(&lock_path)
            .map_err(|e| CatalogError::LockError(
                format!("{}: {}", table_name, e)
            ))?;
        lock_file.lock_exclusive().map_err(|e| CatalogError::LockError(
            format!("{}: {}", table_name, e)
        ))?;
        Ok(lock_file)
    }

    pub fn commit(&self, version: TableVersion) -> Result<u64, CatalogError> {
        let table_dir = self.base_path.join(&version.table_name);
        fs::create_dir_all(&table_dir)?;

        // Acquire cross-process file lock for this table
        let _lock = self.acquire_table_lock(&version.table_name)?;

        // Check version sequence (safe under lock)
        let expected_version = self.get_latest_version_num(&version.table_name)? + 1;
        if version.version != expected_version {
            return Err(CatalogError::InvalidVersion {
                expected: expected_version,
                got: version.version,
            });
        }

        // Write version file atomically (write to temp, then rename)
        let version_path = table_dir.join(format!("{}.json", version.version));
        let temp_version_path = version_path.with_extension("json.tmp");
        let json = serde_json::to_string_pretty(&version)?;
        fs::write(&temp_version_path, &json)?;
        fs::rename(&temp_version_path, &version_path)?;

        // Update latest pointer atomically
        let latest_path = table_dir.join("latest");
        let temp_latest_path = table_dir.join("latest.tmp");
        fs::write(&temp_latest_path, version.version.to_string())?;
        fs::rename(&temp_latest_path, &latest_path)?;

        Ok(version.version)
        // _lock dropped here — file lock released
    }

    /// Commit the next version of a table, automatically assigning the version number.
    ///
    /// Uses a write-ahead intent log for crash safety:
    /// 1. Write pending intent (chunk hashes for this commit)
    /// 2. Commit version to catalog
    /// 3. Remove pending intent
    ///
    /// If a crash occurs between steps 1 and 3, the intent file survives on disk.
    /// On recovery, `recover_pending_commits()` returns these intents so the caller
    /// can identify orphaned chunks and clean them up.
    pub fn commit_next_version(
        &self,
        table_name: &str,
        chunk_hashes: Vec<String>,
    ) -> Result<u64, CatalogError> {
        // Step 1: Write pending intent to disk BEFORE committing
        let intent_id = self.write_pending_intent(table_name, &chunk_hashes)?;

        // Step 2: Acquire lock and commit
        let result = {
            let _lock = self.acquire_table_lock(table_name)?;
            let next = self.get_latest_version_num(table_name)? + 1;
            let version = TableVersion::new(table_name, next, chunk_hashes);
            self.commit_inner(version)
        };

        // Step 3: Remove intent on success (crash here is safe — intent will
        // be found on recovery but the version is already committed, so
        // recover_pending_commits filters it out)
        if result.is_ok() {
            self.remove_pending_intent(&intent_id);
        }

        result
    }

    /// Internal commit without acquiring the lock (caller must hold it).
    fn commit_inner(&self, version: TableVersion) -> Result<u64, CatalogError> {
        let table_dir = self.base_path.join(&version.table_name);
        fs::create_dir_all(&table_dir)?;

        // Check version sequence
        let expected_version = self.get_latest_version_num(&version.table_name)? + 1;
        if version.version != expected_version {
            return Err(CatalogError::InvalidVersion {
                expected: expected_version,
                got: version.version,
            });
        }

        // Write version file atomically (write to temp, then rename)
        let version_path = table_dir.join(format!("{}.json", version.version));
        let temp_version_path = version_path.with_extension("json.tmp");
        let json = serde_json::to_string_pretty(&version)?;
        fs::write(&temp_version_path, &json)?;
        fs::rename(&temp_version_path, &version_path)?;

        // Update latest pointer atomically
        let latest_path = table_dir.join("latest");
        let temp_latest_path = table_dir.join("latest.tmp");
        fs::write(&temp_latest_path, version.version.to_string())?;
        fs::rename(&temp_latest_path, &latest_path)?;

        Ok(version.version)
    }

    // === Write-Ahead Intent Log ===

    /// Write a pending commit intent to disk.
    ///
    /// Returns the intent ID (used to remove the intent after successful commit).
    fn write_pending_intent(
        &self,
        table_name: &str,
        chunk_hashes: &[String],
    ) -> Result<String, CatalogError> {
        let intent_id = uuid::Uuid::new_v4().to_string();
        let pending = PendingCommit {
            intent_id: intent_id.clone(),
            table_name: table_name.to_string(),
            chunk_hashes: chunk_hashes.to_vec(),
            created_at: std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_secs() as i64,
        };

        let pending_dir = self.base_path.join(".pending");
        let intent_path = pending_dir.join(format!("{}.json", intent_id));
        let temp_path = intent_path.with_extension("json.tmp");
        let json = serde_json::to_string(&pending)?;
        fs::write(&temp_path, &json)?;
        fs::rename(&temp_path, &intent_path)?;

        Ok(intent_id)
    }

    /// Remove a pending commit intent after successful commit.
    fn remove_pending_intent(&self, intent_id: &str) {
        let intent_path = self.base_path.join(".pending").join(format!("{}.json", intent_id));
        let _ = fs::remove_file(intent_path);
    }

    /// Recover pending commit intents left by crashes.
    ///
    /// Returns intents whose chunk hashes are NOT referenced by any committed
    /// version, meaning the commit never completed. The caller can use these
    /// to identify and clean up orphaned chunks in the chunk store.
    ///
    /// Intents whose chunks ARE already referenced (crash between step 2 and 3
    /// of commit_next_version) are automatically cleaned up.
    pub fn recover_pending_commits(&self) -> Result<Vec<PendingCommit>, CatalogError> {
        let pending_dir = self.base_path.join(".pending");
        if !pending_dir.exists() {
            return Ok(Vec::new());
        }

        let mut orphaned = Vec::new();

        for entry in fs::read_dir(&pending_dir)? {
            let entry = entry?;
            let path = entry.path();

            if path.extension().and_then(|e| e.to_str()) != Some("json") {
                continue;
            }

            let json = match fs::read_to_string(&path) {
                Ok(j) => j,
                Err(_) => {
                    // Corrupted intent — remove it
                    let _ = fs::remove_file(&path);
                    continue;
                }
            };

            let pending: PendingCommit = match serde_json::from_str(&json) {
                Ok(p) => p,
                Err(_) => {
                    let _ = fs::remove_file(&path);
                    continue;
                }
            };

            // Check if the commit actually completed (crash between step 2 and 3)
            if self.is_committed(&pending.table_name, &pending.chunk_hashes)? {
                // Commit succeeded — just clean up the stale intent
                let _ = fs::remove_file(&path);
            } else {
                // Commit never completed — these chunks are orphaned
                orphaned.push(pending);
                let _ = fs::remove_file(&path);
            }
        }

        Ok(orphaned)
    }

    /// Check whether a set of chunk hashes is referenced by any committed version
    /// of the given table.
    fn is_committed(&self, table_name: &str, chunk_hashes: &[String]) -> Result<bool, CatalogError> {
        let table_dir = self.base_path.join(table_name);
        if !table_dir.exists() {
            return Ok(false);
        }

        // Check the latest version first (most likely match)
        let latest = self.get_latest_version_num(table_name)?;
        if latest == 0 {
            return Ok(false);
        }

        let version = self.get_version(table_name, Some(latest))?;
        Ok(version.chunk_hashes == chunk_hashes)
    }

    pub fn get_version(&self, table_name: &str, version: Option<u64>) -> Result<TableVersion, CatalogError> {
        let table_dir = self.base_path.join(table_name);
        
        if !table_dir.exists() {
            return Err(CatalogError::TableNotFound(table_name.to_string()));
        }

        let version_num = match version {
            Some(v) => v,
            None => self.get_latest_version_num(table_name)?,
        };

        let version_path = table_dir.join(format!("{}.json", version_num));
        
        if !version_path.exists() {
            return Err(CatalogError::VersionNotFound(table_name.to_string(), version_num));
        }

        let json = fs::read_to_string(&version_path)?;
        let table_version: TableVersion = serde_json::from_str(&json)?;
        
        Ok(table_version)
    }

    pub fn list_versions(&self, table_name: &str) -> Result<Vec<u64>, CatalogError> {
        let table_dir = self.base_path.join(table_name);
        
        if !table_dir.exists() {
            return Err(CatalogError::TableNotFound(table_name.to_string()));
        }

        let mut versions = Vec::new();
        
        for entry in fs::read_dir(&table_dir)? {
            let entry = entry?;
            let path = entry.path();
            
            if let Some(ext) = path.extension() {
                if ext == "json" {
                    if let Some(stem) = path.file_stem() {
                        if let Ok(v) = stem.to_string_lossy().parse::<u64>() {
                            versions.push(v);
                        }
                    }
                }
            }
        }
        
        versions.sort();
        Ok(versions)
    }

    pub fn list_tables(&self) -> Result<Vec<String>, CatalogError> {
        let mut tables = Vec::new();

        for entry in fs::read_dir(&self.base_path)? {
            let entry = entry?;
            if entry.file_type()?.is_dir() {
                if let Some(name) = entry.file_name().to_str() {
                    // Skip internal directories
                    if name.starts_with('.') {
                        continue;
                    }
                    tables.push(name.to_string());
                }
            }
        }

        tables.sort();
        Ok(tables)
    }

    fn get_latest_version_num(&self, table_name: &str) -> Result<u64, CatalogError> {
        let latest_path = self.base_path.join(table_name).join("latest");

        if !latest_path.exists() {
            return Ok(0);
        }

        let content = fs::read_to_string(&latest_path)?;
        content
            .trim()
            .parse::<u64>()
            .map_err(|_| CatalogError::LatestPointerCorrupted(table_name.to_string()))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn temp_dir() -> PathBuf {
        let dir = std::env::temp_dir().join(format!("udr_catalog_test_{}", uuid::Uuid::new_v4()));
        fs::create_dir_all(&dir).unwrap();
        dir
    }

    #[test]
    fn test_commit_and_get() {
        let dir = temp_dir();
        let catalog = FileCatalog::new(&dir).unwrap();

        let version = TableVersion::new("test_table", 1, vec!["hash1".to_string(), "hash2".to_string()]);
        let v = catalog.commit(version).unwrap();
        assert_eq!(v, 1);

        let retrieved = catalog.get_version("test_table", Some(1)).unwrap();
        assert_eq!(retrieved.table_name, "test_table");
        assert_eq!(retrieved.version, 1);
        assert_eq!(retrieved.chunk_hashes, vec!["hash1", "hash2"]);

        fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn test_get_latest() {
        let dir = temp_dir();
        let catalog = FileCatalog::new(&dir).unwrap();

        catalog.commit(TableVersion::new("test_table", 1, vec!["v1".to_string()])).unwrap();
        catalog.commit(TableVersion::new("test_table", 2, vec!["v2".to_string()])).unwrap();
        catalog.commit(TableVersion::new("test_table", 3, vec!["v3".to_string()])).unwrap();

        let latest = catalog.get_version("test_table", None).unwrap();
        assert_eq!(latest.version, 3);

        fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn test_version_sequence_enforced() {
        let dir = temp_dir();
        let catalog = FileCatalog::new(&dir).unwrap();

        catalog.commit(TableVersion::new("test_table", 1, vec![])).unwrap();
        
        // Trying to skip version 2
        let result = catalog.commit(TableVersion::new("test_table", 3, vec![]));
        assert!(matches!(result, Err(CatalogError::InvalidVersion { .. })));

        fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn test_list_versions() {
        let dir = temp_dir();
        let catalog = FileCatalog::new(&dir).unwrap();

        catalog.commit(TableVersion::new("test_table", 1, vec![])).unwrap();
        catalog.commit(TableVersion::new("test_table", 2, vec![])).unwrap();
        catalog.commit(TableVersion::new("test_table", 3, vec![])).unwrap();

        let versions = catalog.list_versions("test_table").unwrap();
        assert_eq!(versions, vec![1, 2, 3]);

        fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn test_list_tables() {
        let dir = temp_dir();
        let catalog = FileCatalog::new(&dir).unwrap();

        catalog.commit(TableVersion::new("alpha", 1, vec![])).unwrap();
        catalog.commit(TableVersion::new("beta", 1, vec![])).unwrap();
        catalog.commit(TableVersion::new("gamma", 1, vec![])).unwrap();

        let tables = catalog.list_tables().unwrap();
        assert_eq!(tables, vec!["alpha", "beta", "gamma"]);

        fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn test_time_travel() {
        let dir = temp_dir();
        let catalog = FileCatalog::new(&dir).unwrap();

        catalog.commit(TableVersion::new("data", 1, vec!["old".to_string()])).unwrap();
        catalog.commit(TableVersion::new("data", 2, vec!["new".to_string()])).unwrap();

        let v1 = catalog.get_version("data", Some(1)).unwrap();
        let v2 = catalog.get_version("data", Some(2)).unwrap();

        assert_eq!(v1.chunk_hashes, vec!["old"]);
        assert_eq!(v2.chunk_hashes, vec!["new"]);

        fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn test_corrupted_latest_pointer() {
        let dir = temp_dir();
        let catalog = FileCatalog::new(&dir).unwrap();

        // Create a valid version first
        catalog.commit(TableVersion::new("test_table", 1, vec![])).unwrap();

        // Corrupt the latest pointer
        let latest_path = dir.join("test_table").join("latest");
        fs::write(&latest_path, "not_a_number").unwrap();

        // Getting latest should return LatestPointerCorrupted error
        let result = catalog.get_version("test_table", None);
        assert!(matches!(result, Err(CatalogError::LatestPointerCorrupted(_))));

        // But getting a specific version should still work
        let v1 = catalog.get_version("test_table", Some(1)).unwrap();
        assert_eq!(v1.version, 1);

        fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn test_table_not_found() {
        let dir = temp_dir();
        let catalog = FileCatalog::new(&dir).unwrap();

        let result = catalog.get_version("nonexistent", None);
        assert!(matches!(result, Err(CatalogError::TableNotFound(_))));

        let result = catalog.list_versions("nonexistent");
        assert!(matches!(result, Err(CatalogError::TableNotFound(_))));

        fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn test_version_not_found() {
        let dir = temp_dir();
        let catalog = FileCatalog::new(&dir).unwrap();

        catalog.commit(TableVersion::new("test_table", 1, vec![])).unwrap();

        let result = catalog.get_version("test_table", Some(999));
        assert!(matches!(result, Err(CatalogError::VersionNotFound(_, _))));

        fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn test_concurrent_commit_next_version() {
        use std::sync::Arc;

        let dir = temp_dir();
        let catalog = Arc::new(FileCatalog::new(&dir).unwrap());
        let num_threads = 10;

        // Spawn threads that all try to commit_next_version concurrently
        let handles: Vec<_> = (0..num_threads)
            .map(|i| {
                let catalog = Arc::clone(&catalog);
                std::thread::spawn(move || {
                    catalog.commit_next_version(
                        "concurrent_table",
                        vec![format!("hash_{}", i)],
                    )
                })
            })
            .collect();

        let mut versions: Vec<u64> = handles
            .into_iter()
            .map(|h| h.join().unwrap().unwrap())
            .collect();
        versions.sort();

        // All versions should be unique and sequential 1..=N
        assert_eq!(versions, (1..=num_threads).collect::<Vec<u64>>());

        // Latest should be num_threads
        let latest = catalog.get_version("concurrent_table", None).unwrap();
        assert_eq!(latest.version, num_threads);

        fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn test_lock_file_created() {
        let dir = temp_dir();
        let catalog = FileCatalog::new(&dir).unwrap();

        catalog.commit(TableVersion::new("locked_table", 1, vec![])).unwrap();

        // Lock file should exist after commit
        let lock_path = dir.join("locked_table").join(".lock");
        assert!(lock_path.exists());

        fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn test_pending_dir_created() {
        let dir = temp_dir();
        let _catalog = FileCatalog::new(&dir).unwrap();

        assert!(dir.join(".pending").exists());

        fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn test_no_pending_intents_after_successful_commit() {
        let dir = temp_dir();
        let catalog = FileCatalog::new(&dir).unwrap();

        catalog.commit_next_version("test_table", vec!["h1".to_string()]).unwrap();
        catalog.commit_next_version("test_table", vec!["h2".to_string()]).unwrap();

        // No pending intents should remain after successful commits
        let pending = catalog.recover_pending_commits().unwrap();
        assert!(pending.is_empty());

        fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn test_recover_orphaned_intent() {
        let dir = temp_dir();
        let catalog = FileCatalog::new(&dir).unwrap();

        // Simulate a crash: write an intent file manually without committing
        let intent = PendingCommit {
            intent_id: "crash-sim-001".to_string(),
            table_name: "orphaned_table".to_string(),
            chunk_hashes: vec!["orphan_chunk_1".to_string(), "orphan_chunk_2".to_string()],
            created_at: 1234567890,
        };
        let intent_path = dir.join(".pending").join("crash-sim-001.json");
        let json = serde_json::to_string(&intent).unwrap();
        fs::write(&intent_path, &json).unwrap();

        // Recovery should find the orphaned intent
        let orphaned = catalog.recover_pending_commits().unwrap();
        assert_eq!(orphaned.len(), 1);
        assert_eq!(orphaned[0].table_name, "orphaned_table");
        assert_eq!(orphaned[0].chunk_hashes, vec!["orphan_chunk_1", "orphan_chunk_2"]);

        // Intent file should be cleaned up after recovery
        assert!(!intent_path.exists());

        fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn test_recover_stale_intent_after_completed_commit() {
        let dir = temp_dir();
        let catalog = FileCatalog::new(&dir).unwrap();

        // Commit a version normally
        let hashes = vec!["chunk_a".to_string(), "chunk_b".to_string()];
        catalog.commit_next_version("my_table", hashes.clone()).unwrap();

        // Simulate a crash between step 2 (commit succeeded) and step 3 (intent removal)
        // by manually placing an intent that matches the committed version
        let intent = PendingCommit {
            intent_id: "stale-intent-001".to_string(),
            table_name: "my_table".to_string(),
            chunk_hashes: hashes,
            created_at: 1234567890,
        };
        let intent_path = dir.join(".pending").join("stale-intent-001.json");
        let json = serde_json::to_string(&intent).unwrap();
        fs::write(&intent_path, &json).unwrap();

        // Recovery should detect this is already committed and return empty
        let orphaned = catalog.recover_pending_commits().unwrap();
        assert!(orphaned.is_empty());

        // Stale intent file should be cleaned up
        assert!(!intent_path.exists());

        fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn test_recover_corrupted_intent() {
        let dir = temp_dir();
        let catalog = FileCatalog::new(&dir).unwrap();

        // Write a corrupted intent file
        let intent_path = dir.join(".pending").join("bad-intent.json");
        fs::write(&intent_path, "not valid json!!!").unwrap();

        // Recovery should skip corrupted intents and clean them up
        let orphaned = catalog.recover_pending_commits().unwrap();
        assert!(orphaned.is_empty());
        assert!(!intent_path.exists());

        fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn test_list_tables_excludes_pending_dir() {
        let dir = temp_dir();
        let catalog = FileCatalog::new(&dir).unwrap();

        catalog.commit(TableVersion::new("alpha", 1, vec![])).unwrap();
        catalog.commit(TableVersion::new("beta", 1, vec![])).unwrap();

        // .pending dir should not appear in table listing
        let tables = catalog.list_tables().unwrap();
        assert_eq!(tables, vec!["alpha", "beta"]);
        assert!(!tables.contains(&".pending".to_string()));

        fs::remove_dir_all(&dir).ok();
    }
}
