#![allow(clippy::module_inception)] // branch::branch is intentional for types

pub mod branch;
pub mod error;
pub mod manager;
pub mod merge;

pub use branch::{Branch, BranchDiff};
pub use error::BranchError;
pub use manager::BranchManager;
pub use merge::{MergeAnalysis, MergeAnalyzer, MergeOutcome};
