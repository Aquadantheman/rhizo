//! Recovery manager for crash recovery.
//!
//! This module handles recovery after system crashes or restarts,
//! ensuring consistency of the transaction system.

use super::types::*;
use super::log::TransactionLog;
use super::error::TransactionError;

/// Result of recovery process
#[derive(Debug, Clone)]
pub struct RecoveryReport {
    /// Last committed epoch found
    pub last_committed_epoch: Option<EpochId>,

    /// Transactions that were replayed (committed but effects not applied)
    pub replayed: Vec<TxId>,

    /// Transactions that were rolled back (pending at crash)
    pub rolled_back: Vec<TxId>,

    /// Transactions that were already aborted
    pub already_aborted: Vec<TxId>,

    /// Transactions found committed
    pub already_committed: Vec<TxId>,

    /// Any warnings encountered (non-fatal)
    pub warnings: Vec<String>,

    /// Any errors encountered
    pub errors: Vec<String>,

    /// Total epochs scanned
    pub epochs_scanned: usize,

    /// Total transactions scanned
    pub transactions_scanned: usize,
}

impl Default for RecoveryReport {
    fn default() -> Self {
        Self::new()
    }
}

impl RecoveryReport {
    pub fn new() -> Self {
        Self {
            last_committed_epoch: None,
            replayed: Vec::new(),
            rolled_back: Vec::new(),
            already_aborted: Vec::new(),
            already_committed: Vec::new(),
            warnings: Vec::new(),
            errors: Vec::new(),
            epochs_scanned: 0,
            transactions_scanned: 0,
        }
    }

    /// Check if recovery completed without errors
    pub fn is_clean(&self) -> bool {
        self.errors.is_empty()
    }

    /// Check if recovery had any warnings
    pub fn has_warnings(&self) -> bool {
        !self.warnings.is_empty()
    }

    /// Get total number of transactions affected
    pub fn total_affected(&self) -> usize {
        self.replayed.len() + self.rolled_back.len()
    }

    /// Add a warning
    pub fn warn(&mut self, msg: impl Into<String>) {
        self.warnings.push(msg.into());
    }

    /// Add an error
    pub fn error(&mut self, msg: impl Into<String>) {
        self.errors.push(msg.into());
    }
}

/// Recovery state for a transaction
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum RecoveryDecision {
    /// Transaction was committed and should be preserved
    Keep,
    /// Transaction was pending and should be rolled back
    Rollback,
    /// Transaction was already aborted
    AlreadyAborted,
    /// Transaction needs to be replayed (committed but effects not applied)
    Replay,
}

/// Handles crash recovery for the transaction system
pub struct RecoveryManager<'a> {
    log: &'a TransactionLog,
}

impl<'a> RecoveryManager<'a> {
    /// Create a new recovery manager
    pub fn new(log: &'a TransactionLog) -> Self {
        Self { log }
    }

    /// Perform recovery after crash/restart
    ///
    /// This scans all epochs and transactions to:
    /// 1. Find the last committed epoch
    /// 2. Identify pending transactions that need rollback
    /// 3. Identify committed transactions that need replay
    pub fn recover(&self) -> Result<RecoveryReport, TransactionError> {
        let mut report = RecoveryReport::new();

        // Find all epochs
        let epochs = self.log.list_epochs()?;
        report.epochs_scanned = epochs.len();

        if epochs.is_empty() {
            return Ok(report);
        }

        // Scan epochs from oldest to newest
        for epoch_id in &epochs {
            let is_committed = self.log.is_epoch_committed(*epoch_id)?;

            if is_committed {
                report.last_committed_epoch = Some(*epoch_id);
            }

            // Scan transactions in this epoch
            match self.scan_epoch(*epoch_id, is_committed, &mut report) {
                Ok(_) => {}
                Err(e) => {
                    report.error(format!("Error scanning epoch {}: {}", epoch_id, e));
                }
            }
        }

        Ok(report)
    }

    /// Determine the recovery decision for a transaction
    pub fn decide(&self, tx: &TransactionRecord) -> RecoveryDecision {
        match &tx.status {
            TransactionStatus::Committed => {
                // Check if effects were applied
                // For now, we assume committed = effects applied
                // In future, we could track this separately
                RecoveryDecision::Keep
            }
            TransactionStatus::Aborted { .. } => {
                RecoveryDecision::AlreadyAborted
            }
            TransactionStatus::Active | TransactionStatus::Preparing => {
                RecoveryDecision::Rollback
            }
        }
    }

    /// Scan transactions in an epoch
    fn scan_epoch(
        &self,
        epoch_id: EpochId,
        epoch_committed: bool,
        report: &mut RecoveryReport,
    ) -> Result<(), TransactionError> {
        let tx_ids = self.log.list_transactions_in_epoch(epoch_id)?;

        for tx_id in tx_ids {
            report.transactions_scanned += 1;

            let tx = match self.log.read_transaction_from_epoch(tx_id, epoch_id) {
                Ok(tx) => tx,
                Err(e) => {
                    report.error(format!("Failed to read tx {}: {}", tx_id, e));
                    continue;
                }
            };

            let decision = self.decide(&tx);

            match decision {
                RecoveryDecision::Keep => {
                    report.already_committed.push(tx_id);
                }
                RecoveryDecision::AlreadyAborted => {
                    report.already_aborted.push(tx_id);
                }
                RecoveryDecision::Rollback => {
                    if epoch_committed {
                        // Epoch is committed but tx is not - this is inconsistent
                        report.warn(format!(
                            "Transaction {} in committed epoch {} is not committed",
                            tx_id, epoch_id
                        ));
                    }
                    report.rolled_back.push(tx_id);
                }
                RecoveryDecision::Replay => {
                    report.replayed.push(tx_id);
                }
            }
        }

        Ok(())
    }

    /// Perform recovery and apply necessary rollbacks
    ///
    /// This is a more aggressive recovery that actually marks
    /// pending transactions as aborted.
    pub fn recover_and_apply(&self) -> Result<RecoveryReport, TransactionError> {
        let mut report = self.recover()?;

        // Clone the list to avoid borrow issues
        let to_rollback = report.rolled_back.clone();

        // For each transaction that needs rollback, mark it as aborted
        for tx_id in to_rollback {
            match self.log.read_transaction(tx_id) {
                Ok(mut tx) => {
                    if tx.is_active() || tx.is_preparing() {
                        tx.mark_aborted("Recovery: pending transaction rolled back");
                        if let Err(e) = self.log.write_transaction(&tx) {
                            report.error(format!(
                                "Failed to mark tx {} as aborted: {}",
                                tx_id, e
                            ));
                        }
                    }
                }
                Err(e) => {
                    report.error(format!("Failed to read tx {} for rollback: {}", tx_id, e));
                }
            }
        }

        Ok(report)
    }
}

/// Verify consistency of transaction state
pub fn verify_consistency(log: &TransactionLog) -> Result<Vec<String>, TransactionError> {
    let mut issues = Vec::new();

    let epochs = log.list_epochs()?;

    for epoch_id in &epochs {
        let epoch_meta = match log.get_epoch(*epoch_id) {
            Ok(meta) => meta,
            Err(e) => {
                issues.push(format!("Cannot read epoch {} metadata: {}", epoch_id, e));
                continue;
            }
        };

        let tx_ids = log.list_transactions_in_epoch(*epoch_id)?;

        // Verify transaction count matches
        if tx_ids.len() != epoch_meta.transactions.len() {
            issues.push(format!(
                "Epoch {}: transaction count mismatch (files: {}, metadata: {})",
                epoch_id,
                tx_ids.len(),
                epoch_meta.transactions.len()
            ));
        }

        // Verify each transaction
        for tx_id in &tx_ids {
            match log.read_transaction_from_epoch(*tx_id, *epoch_id) {
                Ok(tx) => {
                    if tx.epoch_id != *epoch_id {
                        issues.push(format!(
                            "Transaction {} claims epoch {} but found in epoch {}",
                            tx_id, tx.epoch_id, epoch_id
                        ));
                    }
                }
                Err(e) => {
                    issues.push(format!("Cannot read transaction {}: {}", tx_id, e));
                }
            }
        }

        // Check committed marker consistency
        let is_committed = log.is_epoch_committed(*epoch_id)?;
        let meta_committed = epoch_meta.is_committed();

        if is_committed != meta_committed {
            issues.push(format!(
                "Epoch {}: commit marker ({}) doesn't match metadata ({})",
                epoch_id, is_committed, meta_committed
            ));
        }
    }

    Ok(issues)
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
    fn test_recovery_empty() {
        let (log, _temp) = create_test_log();
        let recovery = RecoveryManager::new(&log);

        let report = recovery.recover().unwrap();

        assert!(report.is_clean());
        assert_eq!(report.epochs_scanned, 0);
        assert_eq!(report.transactions_scanned, 0);
    }

    #[test]
    fn test_recovery_with_committed_transactions() {
        let (log, _temp) = create_test_log();

        // Create epoch and committed transaction
        log.create_epoch(1).unwrap();
        let mut tx = TransactionRecord::new(1, 1, "main".to_string());
        tx.mark_committed();
        log.write_transaction(&tx).unwrap();
        log.mark_epoch_committed(1).unwrap();

        let recovery = RecoveryManager::new(&log);
        let report = recovery.recover().unwrap();

        assert!(report.is_clean());
        assert_eq!(report.last_committed_epoch, Some(1));
        assert_eq!(report.already_committed, vec![1]);
    }

    #[test]
    fn test_recovery_with_pending_transactions() {
        let (log, _temp) = create_test_log();

        // Create epoch with pending transaction
        log.create_epoch(1).unwrap();
        let tx = TransactionRecord::new(1, 1, "main".to_string());
        log.write_transaction(&tx).unwrap();

        let recovery = RecoveryManager::new(&log);
        let report = recovery.recover().unwrap();

        assert!(report.is_clean());
        assert_eq!(report.rolled_back, vec![1]);
    }

    #[test]
    fn test_recovery_with_aborted_transactions() {
        let (log, _temp) = create_test_log();

        // Create epoch with aborted transaction
        log.create_epoch(1).unwrap();
        let mut tx = TransactionRecord::new(1, 1, "main".to_string());
        tx.mark_aborted("Test abort");
        log.write_transaction(&tx).unwrap();

        let recovery = RecoveryManager::new(&log);
        let report = recovery.recover().unwrap();

        assert!(report.is_clean());
        assert_eq!(report.already_aborted, vec![1]);
    }

    #[test]
    fn test_recovery_decision() {
        let (log, _temp) = create_test_log();
        let recovery = RecoveryManager::new(&log);

        // Active transaction
        let tx1 = TransactionRecord::new(1, 1, "main".to_string());
        assert_eq!(recovery.decide(&tx1), RecoveryDecision::Rollback);

        // Committed transaction
        let mut tx2 = TransactionRecord::new(2, 1, "main".to_string());
        tx2.mark_committed();
        assert_eq!(recovery.decide(&tx2), RecoveryDecision::Keep);

        // Aborted transaction
        let mut tx3 = TransactionRecord::new(3, 1, "main".to_string());
        tx3.mark_aborted("Test");
        assert_eq!(recovery.decide(&tx3), RecoveryDecision::AlreadyAborted);

        // Preparing transaction
        let mut tx4 = TransactionRecord::new(4, 1, "main".to_string());
        tx4.mark_preparing();
        assert_eq!(recovery.decide(&tx4), RecoveryDecision::Rollback);
    }

    #[test]
    fn test_recovery_and_apply() {
        let (log, _temp) = create_test_log();

        // Create epoch with pending transaction
        log.create_epoch(1).unwrap();
        let tx = TransactionRecord::new(1, 1, "main".to_string());
        log.write_transaction(&tx).unwrap();

        let recovery = RecoveryManager::new(&log);
        let report = recovery.recover_and_apply().unwrap();

        assert!(report.is_clean());
        assert_eq!(report.rolled_back, vec![1]);

        // Verify transaction is now aborted
        let tx = log.read_transaction(1).unwrap();
        assert!(tx.is_aborted());
    }

    #[test]
    fn test_verify_consistency_clean() {
        let (log, _temp) = create_test_log();

        // Create consistent state
        let mut meta = log.create_epoch(1).unwrap();
        meta.add_transaction(1);
        log.write_epoch_metadata(&meta).unwrap();

        let tx = TransactionRecord::new(1, 1, "main".to_string());
        log.write_transaction(&tx).unwrap();

        let issues = verify_consistency(&log).unwrap();
        assert!(issues.is_empty());
    }

    #[test]
    fn test_verify_consistency_mismatch() {
        let (log, _temp) = create_test_log();

        // Create inconsistent state (meta has tx, but file doesn't exist for another)
        let mut meta = log.create_epoch(1).unwrap();
        meta.add_transaction(1);
        meta.add_transaction(2); // Claim tx 2 exists but don't create file
        log.write_epoch_metadata(&meta).unwrap();

        let tx = TransactionRecord::new(1, 1, "main".to_string());
        log.write_transaction(&tx).unwrap();

        let issues = verify_consistency(&log).unwrap();
        assert!(!issues.is_empty());
        assert!(issues[0].contains("mismatch"));
    }

    #[test]
    fn test_recovery_report() {
        let mut report = RecoveryReport::new();

        assert!(report.is_clean());
        assert!(!report.has_warnings());
        assert_eq!(report.total_affected(), 0);

        report.warn("Test warning");
        assert!(report.has_warnings());

        report.error("Test error");
        assert!(!report.is_clean());

        report.replayed.push(1);
        report.rolled_back.push(2);
        assert_eq!(report.total_affected(), 2);
    }

    #[test]
    fn test_multiple_epochs() {
        let (log, _temp) = create_test_log();

        // Create multiple epochs
        log.create_epoch(1).unwrap();
        let mut tx1 = TransactionRecord::new(1, 1, "main".to_string());
        tx1.mark_committed();
        log.write_transaction(&tx1).unwrap();
        log.mark_epoch_committed(1).unwrap();

        log.create_epoch(2).unwrap();
        let tx2 = TransactionRecord::new(2, 2, "main".to_string());
        log.write_transaction(&tx2).unwrap();
        // Epoch 2 not committed

        let recovery = RecoveryManager::new(&log);
        let report = recovery.recover().unwrap();

        assert_eq!(report.last_committed_epoch, Some(1));
        assert_eq!(report.already_committed, vec![1]);
        assert_eq!(report.rolled_back, vec![2]);
    }
}
