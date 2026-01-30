//! Transaction manager for cross-table ACID transactions.
//!
//! The TransactionManager coordinates transactions across multiple tables,
//! providing snapshot isolation with conflict detection.

use std::collections::{HashMap, VecDeque};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex, RwLock};

use super::types::*;
use super::epoch::*;
use super::error::TransactionError;
use super::log::TransactionLog;
use super::conflict::{ConflictDetector, TableLevelConflictDetector};
use super::recovery::RecoveryReport;
use crate::catalog::FileCatalog;
use crate::branch::BranchManager;

/// Manages cross-table ACID transactions
pub struct TransactionManager {
    /// Base path for transaction storage
    #[allow(dead_code)] // Stored for future use (recovery, diagnostics)
    base_path: PathBuf,

    /// Transaction log (persists transactions and epochs)
    log: TransactionLog,

    /// Current epoch configuration
    config: EpochConfig,

    /// Active transactions (in-memory for fast access)
    active_transactions: RwLock<HashMap<TxId, TransactionRecord>>,

    /// Recently committed transactions for conflict checking (bounded size).
    /// Capped at max_recent_committed to prevent unbounded memory growth.
    /// validate_snapshot provides a safety net if entries are evicted.
    recent_committed: RwLock<VecDeque<TransactionRecord>>,

    /// Maximum entries in recent_committed before oldest are evicted
    max_recent_committed: usize,

    /// Serializes the commit critical section (conflict check → catalog write → recent_committed update).
    /// Prevents TOCTOU race where two concurrent commits both pass conflict checks before either applies writes.
    commit_lock: Mutex<()>,

    /// Conflict detector (pluggable strategy)
    conflict_detector: Arc<dyn ConflictDetector + Send + Sync>,

    /// Reference to catalog (for version resolution)
    catalog: Arc<FileCatalog>,

    /// Reference to branch manager (optional)
    branch_manager: Option<Arc<BranchManager>>,
}

impl TransactionManager {
    /// Create a new TransactionManager
    pub fn new(
        base_path: impl AsRef<Path>,
        catalog: Arc<FileCatalog>,
        branch_manager: Option<Arc<BranchManager>>,
    ) -> Result<Self, TransactionError> {
        Self::with_config(base_path, catalog, branch_manager, EpochConfig::single_node())
    }

    /// Create a new TransactionManager with custom epoch configuration
    pub fn with_config(
        base_path: impl AsRef<Path>,
        catalog: Arc<FileCatalog>,
        branch_manager: Option<Arc<BranchManager>>,
        _config: EpochConfig,
    ) -> Result<Self, TransactionError> {
        let base_path = base_path.as_ref().to_path_buf();
        let tx_path = base_path.join("transactions");
        std::fs::create_dir_all(&tx_path)?;

        let log = TransactionLog::new(&tx_path)?;

        // Initialize config if needed
        let storage_config = log.initialize_if_needed()?;

        let max_recent = storage_config.epoch_config.max_transactions as usize;

        Ok(Self {
            base_path: tx_path,
            log,
            config: storage_config.epoch_config,
            active_transactions: RwLock::new(HashMap::new()),
            recent_committed: RwLock::new(VecDeque::new()),
            max_recent_committed: max_recent,
            commit_lock: Mutex::new(()),
            conflict_detector: Arc::new(TableLevelConflictDetector::new()),
            catalog,
            branch_manager,
        })
    }

    /// Set a custom conflict detector
    pub fn set_conflict_detector(
        &mut self,
        detector: Arc<dyn ConflictDetector + Send + Sync>,
    ) {
        self.conflict_detector = detector;
    }

    /// Get the epoch configuration
    pub fn config(&self) -> &EpochConfig {
        &self.config
    }

    /// Begin a new transaction
    pub fn begin(&self, branch: Option<&str>) -> Result<TxId, TransactionError> {
        // Get next transaction ID
        let tx_id = self.log.next_tx_id()?;

        // Get current epoch (or create new one)
        let epoch_id = self.log.current_epoch_id()?;

        // Determine branch
        let branch_name = match branch {
            Some(b) => b.to_string(),
            None => self.default_branch()?,
        };

        // Create transaction record
        let mut tx = TransactionRecord::new(tx_id, epoch_id, branch_name.clone());

        // Capture read snapshot (current versions of all tables on branch)
        tx.read_snapshot = self.capture_snapshot(&branch_name)?;

        // Add to active transactions
        {
            let mut active = self.active_transactions.write()
                .map_err(|_| TransactionError::LockError("active_transactions".to_string()))?;
            active.insert(tx_id, tx.clone());
        }

        // Persist to log
        self.log.write_transaction(&tx)?;

        // Update epoch metadata
        let mut epoch_meta = self.log.get_epoch(epoch_id)?;
        epoch_meta.add_transaction(tx_id);
        self.log.write_epoch_metadata(&epoch_meta)?;

        Ok(tx_id)
    }

    /// Add a read to the transaction (for conflict detection)
    pub fn record_read(
        &self,
        tx_id: TxId,
        table_name: &str,
        version: u64,
    ) -> Result<(), TransactionError> {
        let mut active = self.active_transactions.write()
            .map_err(|_| TransactionError::LockError("active_transactions".to_string()))?;
        let tx = active.get_mut(&tx_id)
            .ok_or(TransactionError::TransactionNotFound(tx_id))?;

        if !tx.is_active() {
            return Err(TransactionError::TransactionNotActive(tx_id));
        }

        tx.read_snapshot.insert(table_name.to_string(), version);
        Ok(())
    }

    /// Add a write to the transaction
    pub fn add_write(
        &self,
        tx_id: TxId,
        write: TableWrite,
    ) -> Result<(), TransactionError> {
        let mut active = self.active_transactions.write()
            .map_err(|_| TransactionError::LockError("active_transactions".to_string()))?;
        let tx = active.get_mut(&tx_id)
            .ok_or(TransactionError::TransactionNotFound(tx_id))?;

        if !tx.is_active() {
            return Err(TransactionError::TransactionNotActive(tx_id));
        }

        tx.writes.push(write);
        Ok(())
    }

    /// Commit a transaction
    ///
    /// The commit path is serialized via `commit_lock` to prevent a TOCTOU race
    /// where two concurrent commits both pass conflict checks before either applies
    /// writes to the catalog. The lock is held from conflict detection through
    /// catalog write and recent_committed update, ensuring linearizable commits.
    pub fn commit(&self, tx_id: TxId) -> Result<(), TransactionError> {
        // Get transaction from active set (clone required to release RwLock)
        let mut tx = {
            let active = self.active_transactions.read()
                .map_err(|_| TransactionError::LockError("active_transactions".to_string()))?;
            active.get(&tx_id)
                .ok_or(TransactionError::TransactionNotFound(tx_id))?
                .clone()
        };

        if !tx.is_active() {
            return Err(TransactionError::TransactionNotActive(tx_id));
        }

        // === BEGIN SERIALIZED COMMIT CRITICAL SECTION ===
        // Hold commit_lock from conflict check through catalog write to prevent
        // two transactions from both passing conflict checks concurrently.
        let _commit_guard = self.commit_lock.lock()
            .map_err(|_| TransactionError::LockError("commit_lock".to_string()))?;

        // Check for conflicts with recently committed transactions
        self.check_conflicts(&tx)?;

        // Validate snapshot (tables we read haven't changed)
        self.validate_snapshot(&tx)?;

        // Mark committed in place (no clone needed — we own tx)
        tx.mark_committed();

        // Apply writes to catalog and update branch heads.
        // apply_writes returns actual committed versions (which may differ from
        // pre-computed versions if another writer committed between planning and execution).
        let committed_versions = self.apply_writes(&tx)?;
        self.update_branch_heads(&tx, &committed_versions)?;

        // Persist committed status
        self.log.write_transaction(&tx)?;

        // Update epoch metadata
        let epoch_id = tx.epoch_id;
        let mut epoch_meta = self.log.get_epoch(epoch_id)?;
        epoch_meta.record_commit();
        self.log.write_epoch_metadata(&epoch_meta)?;

        // Add to recently committed for conflict detection (bounded).
        // Move tx instead of cloning — this is the last use.
        {
            let mut recent = self.recent_committed.write()
                .map_err(|_| TransactionError::LockError("recent_committed".to_string()))?;
            recent.push_back(tx);
            while recent.len() > self.max_recent_committed {
                recent.pop_front();
            }
        }

        // === END SERIALIZED COMMIT CRITICAL SECTION ===
        // commit_lock released here (drop of _commit_guard)

        // Remove from active set (safe outside lock — tx_id is unique)
        {
            let mut active = self.active_transactions.write()
                .map_err(|_| TransactionError::LockError("active_transactions".to_string()))?;
            active.remove(&tx_id);
        }

        Ok(())
    }

    /// Abort a transaction
    pub fn abort(&self, tx_id: TxId, reason: &str) -> Result<(), TransactionError> {
        let mut active = self.active_transactions.write()
            .map_err(|_| TransactionError::LockError("active_transactions".to_string()))?;
        let tx = active.get_mut(&tx_id)
            .ok_or(TransactionError::TransactionNotFound(tx_id))?;

        if !tx.is_active() {
            return Err(TransactionError::TransactionNotActive(tx_id));
        }

        tx.mark_aborted(reason);

        // Persist aborted status
        self.log.write_transaction(tx)?;

        // Update epoch metadata
        let mut epoch_meta = self.log.get_epoch(tx.epoch_id)?;
        epoch_meta.record_abort();
        self.log.write_epoch_metadata(&epoch_meta)?;

        // Remove from active set
        let tx_id = tx.tx_id;
        active.remove(&tx_id);

        Ok(())
    }

    /// Get a transaction by ID
    pub fn get_transaction(&self, tx_id: TxId) -> Result<TransactionRecord, TransactionError> {
        // Check active first
        {
            let active = self.active_transactions.read()
                .map_err(|_| TransactionError::LockError("active_transactions".to_string()))?;
            if let Some(tx) = active.get(&tx_id) {
                return Ok(tx.clone());
            }
        }

        // Fall back to log
        self.log.read_transaction(tx_id)
    }

    /// Get all active transactions
    pub fn active_transactions(&self) -> Result<Vec<TransactionRecord>, TransactionError> {
        let active = self.active_transactions.read()
            .map_err(|_| TransactionError::LockError("active_transactions".to_string()))?;
        Ok(active.values().cloned().collect())
    }

    /// Get count of active transactions
    pub fn active_count(&self) -> Result<usize, TransactionError> {
        let active = self.active_transactions.read()
            .map_err(|_| TransactionError::LockError("active_transactions".to_string()))?;
        Ok(active.len())
    }

    /// Clear recently committed transactions (called at epoch boundary)
    pub fn clear_recent_committed(&self) -> Result<(), TransactionError> {
        let mut recent = self.recent_committed.write()
            .map_err(|_| TransactionError::LockError("recent_committed".to_string()))?;
        recent.clear();
        Ok(())
    }

    // === Recovery Methods ===

    /// Perform recovery after crash/restart
    ///
    /// Scans the transaction log to identify:
    /// - Committed transactions (preserved)
    /// - Pending transactions (marked for rollback)
    /// - Any inconsistencies
    ///
    /// This is a read-only operation; use `recover_and_apply` to actually
    /// mark pending transactions as aborted.
    pub fn recover(&self) -> Result<RecoveryReport, TransactionError> {
        use super::recovery::RecoveryManager;
        let recovery = RecoveryManager::new(&self.log);
        recovery.recover()
    }

    /// Perform recovery and apply rollbacks
    ///
    /// Like `recover()`, but also marks pending transactions as aborted.
    /// This is typically called on startup to ensure clean state.
    pub fn recover_and_apply(&self) -> Result<RecoveryReport, TransactionError> {
        use super::recovery::RecoveryManager;
        let recovery = RecoveryManager::new(&self.log);
        recovery.recover_and_apply()
    }

    /// Verify consistency of the transaction system
    ///
    /// Returns a list of any issues found. Empty list means consistent.
    pub fn verify_consistency(&self) -> Result<Vec<String>, TransactionError> {
        super::recovery::verify_consistency(&self.log)
    }

    // === Private helpers ===

    fn default_branch(&self) -> Result<String, TransactionError> {
        if let Some(ref bm) = self.branch_manager {
            bm.get_default()
                .map_err(|e| TransactionError::BranchError(e.to_string()))?
                .ok_or_else(|| TransactionError::BranchError("No default branch".to_string()))
        } else {
            Ok("main".to_string())
        }
    }

    fn capture_snapshot(&self, branch: &str) -> Result<HashMap<String, u64>, TransactionError> {
        let mut snapshot = HashMap::new();

        if let Some(ref bm) = self.branch_manager {
            // Use branch heads
            let branch_data = bm.get(branch)
                .map_err(|e| TransactionError::BranchError(e.to_string()))?;
            snapshot = branch_data.head;
        } else {
            // Use catalog latest versions
            let tables = self.catalog.list_tables()
                .map_err(|e| TransactionError::CatalogError(e.to_string()))?;
            for table in tables {
                if let Ok(version) = self.catalog.get_version(&table, None) {
                    snapshot.insert(table, version.version);
                }
            }
        }

        Ok(snapshot)
    }

    fn check_conflicts(&self, tx: &TransactionRecord) -> Result<(), TransactionError> {
        // Check against recently committed transactions
        let recent = self.recent_committed.read()
            .map_err(|_| TransactionError::LockError("recent_committed".to_string()))?;

        for committed_tx in recent.iter() {
            // Only check transactions that started before us and committed after
            if committed_tx.tx_id >= tx.tx_id {
                continue;
            }

            if let Some(conflict) = self.conflict_detector.detect(tx, committed_tx) {
                return Err(TransactionError::WriteConflict(conflict.tables));
            }
        }

        // Check against other active transactions that are committing
        let active = self.active_transactions.read()
            .map_err(|_| TransactionError::LockError("active_transactions".to_string()))?;

        for (_, other_tx) in active.iter() {
            if other_tx.tx_id == tx.tx_id {
                continue;
            }

            // Only check if other is preparing to commit
            if !other_tx.is_preparing() {
                continue;
            }

            if let Some(conflict) = self.conflict_detector.detect(tx, other_tx) {
                return Err(TransactionError::WriteConflict(conflict.tables));
            }
        }

        Ok(())
    }

    fn validate_snapshot(&self, tx: &TransactionRecord) -> Result<(), TransactionError> {
        for (table, read_version) in &tx.read_snapshot {
            let current_version = if let Some(ref bm) = self.branch_manager {
                bm.get_table_version(&tx.branch, table)
                    .map_err(|e| TransactionError::BranchError(e.to_string()))?
            } else {
                self.catalog.get_version(table, None)
                    .map(|v| Some(v.version))
                    .unwrap_or(None)
            };

            if let Some(current) = current_version {
                if current != *read_version {
                    return Err(TransactionError::SnapshotConflict {
                        table: table.clone(),
                        read_version: *read_version,
                        current_version: current,
                    });
                }
            }
        }

        Ok(())
    }

    fn apply_writes(&self, tx: &TransactionRecord) -> Result<HashMap<String, u64>, TransactionError> {
        let mut committed_versions = HashMap::new();

        for write in &tx.writes {
            // Use catalog-assigned versioning to prevent race conditions where
            // two transactions pre-computed the same next version number.
            let actual_version = self.catalog.commit_next_version(
                &write.table_name,
                write.chunk_hashes.clone(),
            ).map_err(|e| TransactionError::CatalogError(e.to_string()))?;

            committed_versions.insert(write.table_name.clone(), actual_version);
        }

        Ok(committed_versions)
    }

    fn update_branch_heads(
        &self,
        tx: &TransactionRecord,
        committed_versions: &HashMap<String, u64>,
    ) -> Result<(), TransactionError> {
        if let Some(ref bm) = self.branch_manager {
            for write in &tx.writes {
                let branch = write.branch.as_ref().unwrap_or(&tx.branch);
                // Use the actual committed version (from catalog), not the
                // pre-computed version, in case they differ.
                let version = committed_versions
                    .get(&write.table_name)
                    .copied()
                    .unwrap_or(write.new_version);
                bm.update_head(branch, &write.table_name, version)
                    .map_err(|e| TransactionError::BranchError(e.to_string()))?;
            }
        }

        Ok(())
    }

    // =========================================================================
    // Changelog Methods
    // =========================================================================

    /// Query changelog entries matching the given criteria.
    ///
    /// This provides the streaming interface for the unified batch/stream model.
    pub fn get_changelog(
        &self,
        query: crate::changelog::ChangelogQuery,
    ) -> Result<Vec<crate::changelog::ChangelogEntry>, TransactionError> {
        use crate::changelog::ChangelogEntry;
        use std::collections::HashMap;

        // Get committed transactions from log
        let committed = self.log.list_committed_transactions()?;

        let mut entries = Vec::new();
        let mut previous_versions: HashMap<String, u64> = HashMap::new();

        for tx in committed {
            // Filter by tx_id
            if let Some(since_tx) = query.since_tx_id {
                if tx.tx_id <= since_tx {
                    // Still need to track versions for later entries
                    for w in &tx.writes {
                        previous_versions.insert(w.table_name.clone(), w.new_version);
                    }
                    continue;
                }
            }

            // Filter by timestamp
            if let Some(since_ts) = query.since_timestamp {
                if let Some(committed_at) = tx.committed_at {
                    if committed_at < since_ts {
                        for w in &tx.writes {
                            previous_versions.insert(w.table_name.clone(), w.new_version);
                        }
                        continue;
                    }
                }
            }

            // Filter by branch
            if let Some(ref branch) = query.branch {
                if &tx.branch != branch {
                    // Track versions even for non-matching branches
                    for w in &tx.writes {
                        previous_versions.insert(w.table_name.clone(), w.new_version);
                    }
                    continue;
                }
            }

            // Filter by tables
            let include = if let Some(ref tables) = query.tables {
                tx.writes.iter().any(|w| tables.contains(&w.table_name))
            } else {
                true
            };

            if include {
                let entry = ChangelogEntry::from_transaction(&tx, &previous_versions);
                entries.push(entry);

                // Check limit
                if let Some(limit) = query.limit {
                    if entries.len() >= limit {
                        break;
                    }
                }
            }

            // Track versions for future entries
            for w in &tx.writes {
                previous_versions.insert(w.table_name.clone(), w.new_version);
            }
        }

        Ok(entries)
    }

    /// Get the latest committed transaction ID.
    pub fn latest_tx_id(&self) -> Result<Option<u64>, TransactionError> {
        self.log.latest_committed_tx_id()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::TempDir;

    fn create_test_manager() -> (TransactionManager, TempDir) {
        let temp_dir = TempDir::new().unwrap();
        let catalog_path = temp_dir.path().join("catalog");
        let catalog = Arc::new(FileCatalog::new(&catalog_path).unwrap());
        let manager = TransactionManager::new(temp_dir.path(), catalog, None).unwrap();
        (manager, temp_dir)
    }

    fn create_test_manager_with_branches() -> (TransactionManager, Arc<BranchManager>, TempDir) {
        let temp_dir = TempDir::new().unwrap();
        let catalog_path = temp_dir.path().join("catalog");
        let branches_path = temp_dir.path().join("branches");

        let catalog = Arc::new(FileCatalog::new(&catalog_path).unwrap());
        let branches = Arc::new(BranchManager::new(&branches_path).unwrap());

        let manager = TransactionManager::new(
            temp_dir.path(),
            catalog,
            Some(branches.clone()),
        ).unwrap();

        (manager, branches, temp_dir)
    }

    #[test]
    fn test_begin_transaction() {
        let (manager, _temp) = create_test_manager();

        let tx_id = manager.begin(None).unwrap();
        assert_eq!(tx_id, 1);

        let tx = manager.get_transaction(tx_id).unwrap();
        assert!(tx.is_active());
        assert_eq!(tx.branch, "main");
    }

    #[test]
    fn test_begin_multiple_transactions() {
        let (manager, _temp) = create_test_manager();

        let tx1 = manager.begin(None).unwrap();
        let tx2 = manager.begin(None).unwrap();
        let tx3 = manager.begin(None).unwrap();

        assert_eq!(tx1, 1);
        assert_eq!(tx2, 2);
        assert_eq!(tx3, 3);

        assert_eq!(manager.active_count().unwrap(), 3);
    }

    #[test]
    fn test_commit_transaction() {
        let (manager, _temp) = create_test_manager();

        let tx_id = manager.begin(None).unwrap();

        // Add a write
        let write = TableWrite::new("users", 1, vec!["chunk1".to_string()]);
        manager.add_write(tx_id, write).unwrap();

        // Commit
        manager.commit(tx_id).unwrap();

        // Transaction should be committed
        let tx = manager.get_transaction(tx_id).unwrap();
        assert!(tx.is_committed());
        assert!(tx.committed_at.is_some());

        // Active count should be 0
        assert_eq!(manager.active_count().unwrap(), 0);
    }

    #[test]
    fn test_abort_transaction() {
        let (manager, _temp) = create_test_manager();

        let tx_id = manager.begin(None).unwrap();
        manager.abort(tx_id, "Test abort").unwrap();

        let tx = manager.get_transaction(tx_id).unwrap();
        assert!(tx.is_aborted());

        assert_eq!(manager.active_count().unwrap(), 0);
    }

    #[test]
    fn test_add_write() {
        let (manager, _temp) = create_test_manager();

        let tx_id = manager.begin(None).unwrap();

        let write = TableWrite::new("users", 1, vec!["chunk1".to_string()]);
        manager.add_write(tx_id, write).unwrap();

        let tx = manager.get_transaction(tx_id).unwrap();
        assert_eq!(tx.writes.len(), 1);
        assert_eq!(tx.writes[0].table_name, "users");
    }

    #[test]
    fn test_record_read() {
        let (manager, _temp) = create_test_manager();

        let tx_id = manager.begin(None).unwrap();

        manager.record_read(tx_id, "users", 5).unwrap();
        manager.record_read(tx_id, "orders", 3).unwrap();

        let tx = manager.get_transaction(tx_id).unwrap();
        assert_eq!(tx.read_snapshot.get("users"), Some(&5));
        assert_eq!(tx.read_snapshot.get("orders"), Some(&3));
    }

    #[test]
    fn test_transaction_not_found() {
        let (manager, _temp) = create_test_manager();

        let result = manager.get_transaction(999);
        assert!(matches!(result, Err(TransactionError::TransactionNotFound(999))));
    }

    #[test]
    fn test_cannot_modify_committed_transaction() {
        let (manager, _temp) = create_test_manager();

        let tx_id = manager.begin(None).unwrap();
        manager.commit(tx_id).unwrap();

        // Try to add write
        let write = TableWrite::new("users", 1, vec!["chunk1".to_string()]);
        let result = manager.add_write(tx_id, write);
        assert!(matches!(result, Err(TransactionError::TransactionNotFound(_))));
    }

    #[test]
    fn test_cannot_abort_committed_transaction() {
        let (manager, _temp) = create_test_manager();

        let tx_id = manager.begin(None).unwrap();
        manager.commit(tx_id).unwrap();

        let result = manager.abort(tx_id, "Test");
        assert!(matches!(result, Err(TransactionError::TransactionNotFound(_))));
    }

    #[test]
    fn test_write_conflict_detection() {
        let (manager, _temp) = create_test_manager();

        // Start two transactions
        let tx1 = manager.begin(None).unwrap();
        let tx2 = manager.begin(None).unwrap();

        // Both write to same table
        let write1 = TableWrite::new("users", 1, vec!["chunk1".to_string()]);
        let write2 = TableWrite::new("users", 1, vec!["chunk2".to_string()]);

        manager.add_write(tx1, write1).unwrap();
        manager.add_write(tx2, write2).unwrap();

        // First commit succeeds
        manager.commit(tx1).unwrap();

        // Second commit should fail with conflict
        let result = manager.commit(tx2);
        assert!(matches!(result, Err(TransactionError::WriteConflict(_))));
    }

    #[test]
    fn test_no_conflict_different_tables() {
        let (manager, _temp) = create_test_manager();

        // Start two transactions
        let tx1 = manager.begin(None).unwrap();
        let tx2 = manager.begin(None).unwrap();

        // Write to different tables
        let write1 = TableWrite::new("users", 1, vec!["chunk1".to_string()]);
        let write2 = TableWrite::new("orders", 1, vec!["chunk2".to_string()]);

        manager.add_write(tx1, write1).unwrap();
        manager.add_write(tx2, write2).unwrap();

        // Both should commit successfully
        manager.commit(tx1).unwrap();
        manager.commit(tx2).unwrap();
    }

    #[test]
    fn test_with_branch_manager() {
        let (manager, _branches, _temp) = create_test_manager_with_branches();

        let tx_id = manager.begin(None).unwrap();
        let tx = manager.get_transaction(tx_id).unwrap();

        // Should use main branch
        assert_eq!(tx.branch, "main");
    }

    #[test]
    fn test_begin_on_specific_branch() {
        let (manager, branches, _temp) = create_test_manager_with_branches();

        // Create a feature branch
        branches.create("feature/test", None, None).unwrap();

        // Start transaction on feature branch
        let tx_id = manager.begin(Some("feature/test")).unwrap();
        let tx = manager.get_transaction(tx_id).unwrap();

        assert_eq!(tx.branch, "feature/test");
    }

    #[test]
    fn test_commit_updates_branch_heads() {
        let (manager, branches, _temp) = create_test_manager_with_branches();

        let tx_id = manager.begin(None).unwrap();

        let write = TableWrite::new("users", 1, vec!["chunk1".to_string()]);
        manager.add_write(tx_id, write).unwrap();

        manager.commit(tx_id).unwrap();

        // Branch should have updated head
        let version = branches.get_table_version("main", "users").unwrap();
        assert_eq!(version, Some(1));
    }

    #[test]
    fn test_transaction_persistence() {
        let temp_dir = TempDir::new().unwrap();
        let catalog_path = temp_dir.path().join("catalog");
        let catalog = Arc::new(FileCatalog::new(&catalog_path).unwrap());

        // Create manager and begin transaction
        {
            let manager = TransactionManager::new(temp_dir.path(), catalog.clone(), None).unwrap();
            let tx_id = manager.begin(None).unwrap();
            let write = TableWrite::new("users", 1, vec!["chunk1".to_string()]);
            manager.add_write(tx_id, write).unwrap();
            manager.commit(tx_id).unwrap();
        }

        // Create new manager and verify transaction is readable
        {
            let manager = TransactionManager::new(temp_dir.path(), catalog, None).unwrap();
            let tx = manager.get_transaction(1).unwrap();
            assert!(tx.is_committed());
            assert_eq!(tx.writes.len(), 1);
        }
    }

    #[test]
    fn test_active_transactions_list() {
        let (manager, _temp) = create_test_manager();

        manager.begin(None).unwrap();
        manager.begin(None).unwrap();

        let active = manager.active_transactions().unwrap();
        assert_eq!(active.len(), 2);
    }

    #[test]
    fn test_conflict_detection_after_epoch_boundary_clear() {
        // This test verifies that conflict detection works even when
        // recent_committed is cleared (simulating an epoch boundary).
        // The system catches conflicts via validate_snapshot: transactions
        // that read a table before writing detect when that table changed.

        let (manager, _temp) = create_test_manager();

        // Start two transactions (both see same initial state)
        let tx1 = manager.begin(None).unwrap();
        let tx2 = manager.begin(None).unwrap();

        // Both read users at version 0 (initial state) then write
        manager.record_read(tx1, "users", 0).unwrap();
        manager.record_read(tx2, "users", 0).unwrap();

        let write1 = TableWrite::new("users", 1, vec!["chunk1".to_string()]);
        let write2 = TableWrite::new("users", 1, vec!["chunk2".to_string()]);

        manager.add_write(tx1, write1).unwrap();
        manager.add_write(tx2, write2).unwrap();

        // First commit succeeds
        manager.commit(tx1).unwrap();

        // Clear recent_committed (simulating epoch boundary)
        manager.clear_recent_committed().unwrap();

        // Second commit should fail: validate_snapshot detects that
        // users changed from v0 (when TX2 read it) to v1 (after TX1 committed)
        let result = manager.commit(tx2);

        assert!(result.is_err(),
            "TX2 should fail after epoch boundary clear, but got: {:?}", result);

        // Verify it's a snapshot conflict
        let err = result.unwrap_err();
        let is_conflict = matches!(
            err,
            TransactionError::WriteConflict(_) |
            TransactionError::SnapshotConflict { .. } |
            TransactionError::CatalogError(_)
        );
        assert!(is_conflict, "Expected conflict error, got: {:?}", err);
    }
}
