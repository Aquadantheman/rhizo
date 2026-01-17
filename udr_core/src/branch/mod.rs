#![allow(clippy::module_inception)] // branch::branch is intentional for types

pub mod error;
pub mod branch;
pub mod manager;

pub use error::BranchError;
pub use branch::{Branch, BranchDiff};
pub use manager::BranchManager;
