use std::fs;
use std::path::{Path, PathBuf};
use fs2::FileExt;
use super::error::CatalogError;
use super::version::TableVersion;

pub struct FileCatalog {
    base_path: PathBuf,
}

impl FileCatalog {
    pub fn new(base_path: impl AsRef<Path>) -> Result<Self, CatalogError> {
        let base_path = base_path.as_ref().to_path_buf();
        fs::create_dir_all(&base_path)?;
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
    /// Acquires an exclusive file lock, reads the latest version, increments, and
    /// commits atomically. Safe for both multi-thread and multi-process access.
    pub fn commit_next_version(
        &self,
        table_name: &str,
        chunk_hashes: Vec<String>,
    ) -> Result<u64, CatalogError> {
        // Acquire lock BEFORE reading latest, so the entire read-increment-write
        // is atomic across processes
        let _lock = self.acquire_table_lock(table_name)?;
        let next = self.get_latest_version_num(table_name)? + 1;
        let version = TableVersion::new(table_name, next, chunk_hashes);
        self.commit_inner(version)
        // _lock dropped here — file lock released
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
}
