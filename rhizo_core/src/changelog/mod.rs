//! Changelog module for unified batch/stream access to committed changes.
//!
//! This module provides:
//! - `ChangelogEntry` - A committed transaction viewed as changelog entry
//! - `TableChange` - A single table modification within a commit
//! - `ChangelogQuery` - Builder for filtering changelog queries
//!
//! The changelog is built on top of the TransactionLog, providing a
//! streaming-friendly view of committed transactions. This enables
//! the unified batch/stream model:
//! - Batch: "What is the state at version V?" (via QueryEngine.query())
//! - Stream: "What changed since version V?" (via changelog)

mod entry;
mod query;

pub use entry::{ChangelogEntry, TableChange};
pub use query::ChangelogQuery;

#[cfg(test)]
mod tests;
