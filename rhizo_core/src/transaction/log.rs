//! Persistent transaction log with epoch-based organization.
//!
//! Storage layout:
//! ```text
//! {base_path}/
//! +-- _config.json                # Epoch configuration
//! +-- _sequence                   # Current tx_id counter (atomic)
//! +-- _epoch_sequence             # Current epoch_id counter (atomic)
//! |
//! +-- epochs/                     # Epoch-organized transaction logs
//!     +-- 000001/                 # Epoch directory
//!     |   +-- _meta.json          # EpochMetadata
//!     |   +-- _committed          # Marker file (empty = committed)
//!     |   +-- tx_000001.json      # TransactionRecord
//!     |   +-- tx_000002.json
//!     +-- 000002/
//!         +-- ...
//! ```

use std::fs;
use std::path::{Path, PathBuf};

use super::types::*;
use super::epoch::*;
use super::error::TransactionError;

const EPOCHS_DIR: &str = "epochs";
const CONFIG_FILE: &str = "_config.json";
const SEQUENCE_FILE: &str = "_sequence";
const EPOCH_SEQUENCE_FILE: &str = "_epoch_sequence";
const LATEST_COMMITTED_FILE: &str = "_latest_committed";
const COMMITTED_INDEX_FILE: &str = "_committed_index";
const EPOCH_META_FILE: &str = "_meta.json";
const EPOCH_COMMITTED_MARKER: &str = "_committed";

/// Persistent storage configuration
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct StorageConfig {
    /// Format version for forward compatibility
    pub format_version: u32,

    /// Epoch configuration
    pub epoch_config: EpochConfig,

    /// Unix timestamp when storage was created
    pub created_at: i64,
}

impl StorageConfig {
    pub const CURRENT_FORMAT_VERSION: u32 = 1;

    pub fn new(epoch_config: EpochConfig) -> Self {
        let now = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_secs() as i64;

        Self {
            format_version: Self::CURRENT_FORMAT_VERSION,
            epoch_config,
            created_at: now,
        }
    }
}

/// Persistent transaction log
pub struct TransactionLog {
    base_path: PathBuf,
}

impl TransactionLog {
    /// Create a new transaction log at the given path
    pub fn new(base_path: impl AsRef<Path>) -> Result<Self, TransactionError> {
        let base_path = base_path.as_ref().to_path_buf();
        let epochs_dir = base_path.join(EPOCHS_DIR);
        fs::create_dir_all(&epochs_dir)?;

        Ok(Self { base_path })
    }

    /// Get the base path
    pub fn base_path(&self) -> &Path {
        &self.base_path
    }

    // === Sequence Management ===

    /// Get next transaction ID (atomic increment)
    pub fn next_tx_id(&self) -> Result<TxId, TransactionError> {
        let path = self.base_path.join(SEQUENCE_FILE);

        let current = if path.exists() {
            fs::read_to_string(&path)?
                .trim()
                .parse::<u64>()
                .unwrap_or(0)
        } else {
            0
        };

        let next = current + 1;

        // Atomic write using temp file + rename
        let temp_path = path.with_extension("tmp");
        fs::write(&temp_path, next.to_string())?;
        fs::rename(&temp_path, &path)?;

        Ok(next)
    }

    /// Get current transaction ID without incrementing
    pub fn current_tx_id(&self) -> Result<TxId, TransactionError> {
        let path = self.base_path.join(SEQUENCE_FILE);

        if path.exists() {
            Ok(fs::read_to_string(&path)?
                .trim()
                .parse::<u64>()
                .unwrap_or(0))
        } else {
            Ok(0)
        }
    }

    /// Get current epoch ID (or create first epoch)
    pub fn current_epoch_id(&self) -> Result<EpochId, TransactionError> {
        let path = self.base_path.join(EPOCH_SEQUENCE_FILE);

        if path.exists() {
            Ok(fs::read_to_string(&path)?
                .trim()
                .parse::<u64>()
                .unwrap_or(1))
        } else {
            // Create first epoch
            self.create_epoch(1)?;
            Ok(1)
        }
    }

    /// Get next epoch ID (atomic increment)
    pub fn next_epoch_id(&self) -> Result<EpochId, TransactionError> {
        let current = self.current_epoch_id()?;
        let next = current + 1;

        let path = self.base_path.join(EPOCH_SEQUENCE_FILE);
        let temp_path = path.with_extension("tmp");
        fs::write(&temp_path, next.to_string())?;
        fs::rename(&temp_path, &path)?;

        Ok(next)
    }

    // === Epoch Management ===

    /// Create a new epoch
    pub fn create_epoch(&self, epoch_id: EpochId) -> Result<EpochMetadata, TransactionError> {
        let epoch_dir = self.epoch_dir(epoch_id);
        fs::create_dir_all(&epoch_dir)?;

        let meta = EpochMetadata::new(epoch_id);
        self.write_epoch_metadata(&meta)?;

        // Update epoch sequence
        let seq_path = self.base_path.join(EPOCH_SEQUENCE_FILE);
        let temp_path = seq_path.with_extension("tmp");
        fs::write(&temp_path, epoch_id.to_string())?;
        fs::rename(&temp_path, &seq_path)?;

        Ok(meta)
    }

    /// Get epoch metadata
    pub fn get_epoch(&self, epoch_id: EpochId) -> Result<EpochMetadata, TransactionError> {
        let meta_path = self.epoch_dir(epoch_id).join(EPOCH_META_FILE);

        if !meta_path.exists() {
            return Err(TransactionError::EpochNotFound(epoch_id));
        }

        let json = fs::read_to_string(&meta_path)?;
        let meta: EpochMetadata = serde_json::from_str(&json)?;
        Ok(meta)
    }

    /// Write epoch metadata
    pub fn write_epoch_metadata(&self, meta: &EpochMetadata) -> Result<(), TransactionError> {
        let epoch_dir = self.epoch_dir(meta.epoch_id);
        fs::create_dir_all(&epoch_dir)?;

        let meta_path = epoch_dir.join(EPOCH_META_FILE);
        let temp_path = meta_path.with_extension("json.tmp");

        let json = serde_json::to_string_pretty(meta)?;
        fs::write(&temp_path, &json)?;
        fs::rename(&temp_path, &meta_path)?;

        Ok(())
    }

    /// Mark epoch as committed (create marker file)
    pub fn mark_epoch_committed(&self, epoch_id: EpochId) -> Result<(), TransactionError> {
        let marker_path = self.epoch_dir(epoch_id).join(EPOCH_COMMITTED_MARKER);
        fs::write(&marker_path, "")?;
        Ok(())
    }

    /// Check if epoch is committed (marker file exists)
    pub fn is_epoch_committed(&self, epoch_id: EpochId) -> Result<bool, TransactionError> {
        let marker_path = self.epoch_dir(epoch_id).join(EPOCH_COMMITTED_MARKER);
        Ok(marker_path.exists())
    }

    /// List all epoch IDs
    pub fn list_epochs(&self) -> Result<Vec<EpochId>, TransactionError> {
        let epochs_dir = self.base_path.join(EPOCHS_DIR);
        if !epochs_dir.exists() {
            return Ok(Vec::new());
        }

        let mut epochs = Vec::new();
        for entry in fs::read_dir(&epochs_dir)? {
            let entry = entry?;
            if entry.file_type()?.is_dir() {
                if let Some(name) = entry.file_name().to_str() {
                    if let Ok(epoch_id) = name.parse::<u64>() {
                        epochs.push(epoch_id);
                    }
                }
            }
        }

        epochs.sort();
        Ok(epochs)
    }

    // === Transaction Management ===

    /// Write transaction record to current epoch.
    ///
    /// If the transaction is committed, also updates the `_latest_committed`
    /// pointer file so that `latest_committed_tx_id()` is O(1).
    pub fn write_transaction(&self, tx: &TransactionRecord) -> Result<(), TransactionError> {
        let epoch_dir = self.epoch_dir(tx.epoch_id);
        fs::create_dir_all(&epoch_dir)?;

        let tx_path = epoch_dir.join(format!("tx_{:06}.json", tx.tx_id));
        let temp_path = tx_path.with_extension("json.tmp");

        let json = serde_json::to_string_pretty(tx)?;
        fs::write(&temp_path, &json)?;
        fs::rename(&temp_path, &tx_path)?;

        // Update committed tracking when persisting a committed transaction
        if tx.is_committed() {
            self.update_latest_committed(tx.tx_id)?;
            self.append_committed_index(tx.epoch_id, tx.tx_id)?;
        }

        Ok(())
    }

    /// Update the latest committed transaction pointer file.
    ///
    /// Uses atomic write (temp + rename) to avoid partial reads.
    fn update_latest_committed(&self, tx_id: TxId) -> Result<(), TransactionError> {
        let path = self.base_path.join(LATEST_COMMITTED_FILE);
        let temp_path = path.with_extension("tmp");
        fs::write(&temp_path, tx_id.to_string())?;
        fs::rename(&temp_path, &path)?;
        Ok(())
    }

    /// Append a committed transaction entry to the index file.
    ///
    /// The index is an append-only text file with one `epoch_id:tx_id` per line,
    /// enabling O(entries) reads without scanning epoch directories.
    fn append_committed_index(&self, epoch_id: EpochId, tx_id: TxId) -> Result<(), TransactionError> {
        use std::io::Write;
        let path = self.base_path.join(COMMITTED_INDEX_FILE);
        let mut file = fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(&path)?;
        writeln!(file, "{}:{}", epoch_id, tx_id)?;
        Ok(())
    }

    /// Read the committed index, returning (epoch_id, tx_id) pairs in order.
    ///
    /// Returns None if the index doesn't exist (pre-index data — caller
    /// should fall back to full scan and rebuild the index).
    fn read_committed_index(&self) -> Result<Option<Vec<(EpochId, TxId)>>, TransactionError> {
        let path = self.base_path.join(COMMITTED_INDEX_FILE);
        if !path.exists() {
            return Ok(None);
        }

        let content = fs::read_to_string(&path)?;
        let mut entries = Vec::new();

        for line in content.lines() {
            let line = line.trim();
            if line.is_empty() {
                continue;
            }
            if let Some((epoch_str, tx_str)) = line.split_once(':') {
                if let (Ok(epoch_id), Ok(tx_id)) = (epoch_str.parse::<u64>(), tx_str.parse::<u64>()) {
                    entries.push((epoch_id, tx_id));
                }
                // Skip malformed lines silently
            }
        }

        Ok(Some(entries))
    }

    /// Rebuild the committed index from a full epoch scan.
    ///
    /// Used for backward compatibility when the index file doesn't exist.
    fn rebuild_committed_index(&self) -> Result<Vec<(EpochId, TxId)>, TransactionError> {
        let epochs = self.list_epochs()?;
        let mut entries = Vec::new();

        for epoch_id in epochs {
            let tx_ids = self.list_transactions_in_epoch(epoch_id)?;
            for tx_id in tx_ids {
                match self.read_transaction_from_epoch(tx_id, epoch_id) {
                    Ok(tx) if tx.is_committed() => {
                        entries.push((epoch_id, tx_id));
                    }
                    _ => continue,
                }
            }
        }

        entries.sort_by_key(|&(_, tx_id)| tx_id);

        // Write the index so future calls are fast
        let path = self.base_path.join(COMMITTED_INDEX_FILE);
        let content: String = entries
            .iter()
            .map(|(epoch_id, tx_id)| format!("{}:{}", epoch_id, tx_id))
            .collect::<Vec<_>>()
            .join("\n");
        if !content.is_empty() {
            fs::write(&path, format!("{}\n", content))?;
        }

        Ok(entries)
    }

    /// Read transaction record.
    ///
    /// Uses the committed index for O(1) epoch lookup when available,
    /// falling back to linear epoch scan for uncommitted or pre-index transactions.
    pub fn read_transaction(&self, tx_id: TxId) -> Result<TransactionRecord, TransactionError> {
        // Fast path: look up epoch from committed index
        if let Ok(Some(entries)) = self.read_committed_index() {
            if let Some(&(epoch_id, _)) = entries.iter().find(|(_, tid)| *tid == tx_id) {
                return self.read_transaction_from_epoch(tx_id, epoch_id);
            }
        }

        // Fallback: linear scan across epochs (handles uncommitted or pre-index txns)
        let epochs = self.list_epochs()?;

        for epoch_id in epochs.into_iter().rev() {
            let tx_path = self.epoch_dir(epoch_id).join(format!("tx_{:06}.json", tx_id));
            if tx_path.exists() {
                let json = fs::read_to_string(&tx_path)?;
                let tx: TransactionRecord = serde_json::from_str(&json)?;
                return Ok(tx);
            }
        }

        Err(TransactionError::TransactionNotFound(tx_id))
    }

    /// Read transaction from specific epoch
    pub fn read_transaction_from_epoch(
        &self,
        tx_id: TxId,
        epoch_id: EpochId,
    ) -> Result<TransactionRecord, TransactionError> {
        let tx_path = self.epoch_dir(epoch_id).join(format!("tx_{:06}.json", tx_id));
        if !tx_path.exists() {
            return Err(TransactionError::TransactionNotFound(tx_id));
        }

        let json = fs::read_to_string(&tx_path)?;
        let tx: TransactionRecord = serde_json::from_str(&json)?;
        Ok(tx)
    }

    /// List all transactions in an epoch
    pub fn list_transactions_in_epoch(
        &self,
        epoch_id: EpochId,
    ) -> Result<Vec<TxId>, TransactionError> {
        let epoch_dir = self.epoch_dir(epoch_id);
        if !epoch_dir.exists() {
            return Err(TransactionError::EpochNotFound(epoch_id));
        }

        let mut tx_ids = Vec::new();
        for entry in fs::read_dir(&epoch_dir)? {
            let entry = entry?;
            if let Some(name) = entry.file_name().to_str() {
                if name.starts_with("tx_") && name.ends_with(".json") {
                    // Parse tx_000001.json -> 1
                    let id_str = name
                        .trim_start_matches("tx_")
                        .trim_end_matches(".json");
                    if let Ok(tx_id) = id_str.parse::<u64>() {
                        tx_ids.push(tx_id);
                    }
                }
            }
        }

        tx_ids.sort();
        Ok(tx_ids)
    }

    // === Configuration ===

    /// Load storage configuration
    pub fn load_config(&self) -> Result<Option<StorageConfig>, TransactionError> {
        let path = self.base_path.join(CONFIG_FILE);
        if !path.exists() {
            return Ok(None);
        }

        let json = fs::read_to_string(&path)?;
        let config: StorageConfig = serde_json::from_str(&json)?;
        Ok(Some(config))
    }

    /// Save storage configuration
    pub fn save_config(&self, config: &StorageConfig) -> Result<(), TransactionError> {
        let path = self.base_path.join(CONFIG_FILE);
        let temp_path = path.with_extension("tmp");

        let json = serde_json::to_string_pretty(config)?;
        fs::write(&temp_path, &json)?;
        fs::rename(&temp_path, &path)?;

        Ok(())
    }

    /// Initialize with default configuration if not exists
    pub fn initialize_if_needed(&self) -> Result<StorageConfig, TransactionError> {
        if let Some(config) = self.load_config()? {
            return Ok(config);
        }

        let config = StorageConfig::new(EpochConfig::single_node());
        self.save_config(&config)?;
        Ok(config)
    }

    // === Changelog Support ===

    /// List all committed transactions in order.
    ///
    /// Uses the committed index for O(C) performance where C is committed
    /// transaction count, rather than scanning all epoch directories.
    /// Falls back to full scan + index rebuild for pre-index data.
    pub fn list_committed_transactions(&self) -> Result<Vec<TransactionRecord>, TransactionError> {
        let index = match self.read_committed_index()? {
            Some(entries) => entries,
            None => self.rebuild_committed_index()?,
        };

        let mut committed = Vec::new();
        for (epoch_id, tx_id) in index {
            match self.read_transaction_from_epoch(tx_id, epoch_id) {
                Ok(tx) => committed.push(tx),
                Err(_) => continue,
            }
        }

        Ok(committed)
    }

    /// Get the latest committed transaction ID, if any.
    ///
    /// Reads from the `_latest_committed` pointer file for O(1) performance.
    /// Falls back to a full scan if the pointer file doesn't exist (backward
    /// compatibility with data created before the pointer was introduced).
    pub fn latest_committed_tx_id(&self) -> Result<Option<TxId>, TransactionError> {
        let path = self.base_path.join(LATEST_COMMITTED_FILE);

        if path.exists() {
            let content = fs::read_to_string(&path)?;
            match content.trim().parse::<u64>() {
                Ok(tx_id) => return Ok(Some(tx_id)),
                Err(_) => {
                    // Corrupted pointer — fall through to full scan
                }
            }
        }

        // Fallback: full scan (only needed for pre-pointer data or corrupted pointer)
        let committed = self.list_committed_transactions()?;
        let latest = committed.last().map(|tx| tx.tx_id);

        // Backfill the pointer so future calls are O(1)
        if let Some(tx_id) = latest {
            let _ = self.update_latest_committed(tx_id);
        }

        Ok(latest)
    }

    /// List committed transactions since a specific tx_id (exclusive).
    ///
    /// Uses the committed index to skip directly to entries after `since_tx_id`,
    /// only deserializing the transactions that match.
    pub fn list_committed_since(
        &self,
        since_tx_id: TxId,
    ) -> Result<Vec<TransactionRecord>, TransactionError> {
        let index = match self.read_committed_index()? {
            Some(entries) => entries,
            None => self.rebuild_committed_index()?,
        };

        let mut committed = Vec::new();
        for (epoch_id, tx_id) in index {
            if tx_id <= since_tx_id {
                continue;
            }
            match self.read_transaction_from_epoch(tx_id, epoch_id) {
                Ok(tx) => committed.push(tx),
                Err(_) => continue,
            }
        }

        Ok(committed)
    }

    // === Private Helpers ===

    fn epoch_dir(&self, epoch_id: EpochId) -> PathBuf {
        self.base_path.join(EPOCHS_DIR).join(format!("{:06}", epoch_id))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::TempDir;

    fn create_test_log() -> (TransactionLog, TempDir) {
        let temp_dir = TempDir::new().unwrap();
        let log = TransactionLog::new(temp_dir.path()).unwrap();
        (log, temp_dir)
    }

    #[test]
    fn test_transaction_log_new() {
        let (log, _temp) = create_test_log();
        assert!(log.base_path().exists());
        assert!(log.base_path().join(EPOCHS_DIR).exists());
    }

    #[test]
    fn test_tx_id_sequence() {
        let (log, _temp) = create_test_log();

        assert_eq!(log.current_tx_id().unwrap(), 0);

        let id1 = log.next_tx_id().unwrap();
        assert_eq!(id1, 1);

        let id2 = log.next_tx_id().unwrap();
        assert_eq!(id2, 2);

        let id3 = log.next_tx_id().unwrap();
        assert_eq!(id3, 3);

        assert_eq!(log.current_tx_id().unwrap(), 3);
    }

    #[test]
    fn test_epoch_creation() {
        let (log, _temp) = create_test_log();

        let epoch = log.create_epoch(1).unwrap();
        assert_eq!(epoch.epoch_id, 1);
        assert!(epoch.is_active());

        let loaded = log.get_epoch(1).unwrap();
        assert_eq!(loaded.epoch_id, 1);
    }

    #[test]
    fn test_current_epoch_creates_first() {
        let (log, _temp) = create_test_log();

        let epoch_id = log.current_epoch_id().unwrap();
        assert_eq!(epoch_id, 1);

        // Should have created epoch 1
        assert!(log.get_epoch(1).is_ok());
    }

    #[test]
    fn test_list_epochs() {
        let (log, _temp) = create_test_log();

        log.create_epoch(1).unwrap();
        log.create_epoch(2).unwrap();
        log.create_epoch(3).unwrap();

        let epochs = log.list_epochs().unwrap();
        assert_eq!(epochs, vec![1, 2, 3]);
    }

    #[test]
    fn test_write_read_transaction() {
        let (log, _temp) = create_test_log();

        log.create_epoch(1).unwrap();

        let mut tx = TransactionRecord::new(1, 1, "main".to_string());
        tx.record_read("users", 5);

        log.write_transaction(&tx).unwrap();

        let loaded = log.read_transaction(1).unwrap();
        assert_eq!(loaded.tx_id, 1);
        assert_eq!(loaded.epoch_id, 1);
        assert_eq!(loaded.branch, "main");
        assert_eq!(loaded.read_snapshot.get("users"), Some(&5));
    }

    #[test]
    fn test_transaction_not_found() {
        let (log, _temp) = create_test_log();

        let result = log.read_transaction(999);
        assert!(matches!(result, Err(TransactionError::TransactionNotFound(999))));
    }

    #[test]
    fn test_epoch_not_found() {
        let (log, _temp) = create_test_log();

        let result = log.get_epoch(999);
        assert!(matches!(result, Err(TransactionError::EpochNotFound(999))));
    }

    #[test]
    fn test_list_transactions_in_epoch() {
        let (log, _temp) = create_test_log();

        log.create_epoch(1).unwrap();

        let tx1 = TransactionRecord::new(1, 1, "main".to_string());
        let tx2 = TransactionRecord::new(2, 1, "main".to_string());
        let tx3 = TransactionRecord::new(3, 1, "main".to_string());

        log.write_transaction(&tx1).unwrap();
        log.write_transaction(&tx2).unwrap();
        log.write_transaction(&tx3).unwrap();

        let tx_ids = log.list_transactions_in_epoch(1).unwrap();
        assert_eq!(tx_ids, vec![1, 2, 3]);
    }

    #[test]
    fn test_epoch_committed_marker() {
        let (log, _temp) = create_test_log();

        log.create_epoch(1).unwrap();

        assert!(!log.is_epoch_committed(1).unwrap());

        log.mark_epoch_committed(1).unwrap();

        assert!(log.is_epoch_committed(1).unwrap());
    }

    #[test]
    fn test_storage_config() {
        let (log, _temp) = create_test_log();

        // Initially no config
        assert!(log.load_config().unwrap().is_none());

        // Save config
        let config = StorageConfig::new(EpochConfig::high_throughput());
        log.save_config(&config).unwrap();

        // Load config
        let loaded = log.load_config().unwrap().unwrap();
        assert_eq!(loaded.epoch_config.duration_ms, 50);
        assert_eq!(loaded.epoch_config.max_transactions, 10000);
    }

    #[test]
    fn test_initialize_if_needed() {
        let (log, _temp) = create_test_log();

        // First call creates config
        let config = log.initialize_if_needed().unwrap();
        assert!(config.epoch_config.is_immediate()); // single_node mode

        // Second call returns existing
        let config2 = log.initialize_if_needed().unwrap();
        assert_eq!(config.created_at, config2.created_at);
    }

    #[test]
    fn test_update_epoch_metadata() {
        let (log, _temp) = create_test_log();

        let mut meta = log.create_epoch(1).unwrap();

        meta.add_transaction(1);
        meta.add_transaction(2);
        meta.record_commit();

        log.write_epoch_metadata(&meta).unwrap();

        let loaded = log.get_epoch(1).unwrap();
        assert_eq!(loaded.transactions, vec![1, 2]);
        assert_eq!(loaded.committed_count, 1);
    }
}
