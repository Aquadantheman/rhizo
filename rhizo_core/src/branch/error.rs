use thiserror::Error;

#[derive(Error, Debug)]
pub enum BranchError {
    #[error("IO error: {0}")]
    Io(#[from] std::io::Error),

    #[error("JSON error: {0}")]
    Json(#[from] serde_json::Error),

    #[error("Branch not found: {0}")]
    BranchNotFound(String),

    #[error("Branch already exists: {0}")]
    BranchAlreadyExists(String),

    #[error("Cannot delete default branch: {0}")]
    CannotDeleteDefault(String),

    #[error("Invalid branch name: {0}")]
    InvalidBranchName(String),

    #[error("Merge conflict: branches have diverged on tables")]
    MergeConflict(Vec<String>),

    #[error("Cannot fast-forward: {source_branch} is not ahead of {target_branch}")]
    CannotFastForward { source_branch: String, target_branch: String },
}
