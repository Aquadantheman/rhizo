use serde::{Deserialize, Serialize};
use std::collections::HashMap;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TableVersion {
    pub table_name: String,
    pub version: u64,
    pub chunk_hashes: Vec<String>,
    pub schema_hash: Option<String>,
    pub created_at: i64,
    pub parent_version: Option<u64>,
    pub metadata: HashMap<String, String>,
}

impl TableVersion {
    pub fn new(table_name: impl Into<String>, version: u64, chunk_hashes: Vec<String>) -> Self {
        Self {
            table_name: table_name.into(),
            version,
            chunk_hashes,
            schema_hash: None,
            created_at: std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_secs() as i64,
            parent_version: if version > 1 { Some(version - 1) } else { None },
            metadata: HashMap::new(),
        }
    }

    pub fn with_metadata(mut self, key: impl Into<String>, value: impl Into<String>) -> Self {
        self.metadata.insert(key.into(), value.into());
        self
    }

    pub fn with_schema_hash(mut self, hash: impl Into<String>) -> Self {
        self.schema_hash = Some(hash.into());
        self
    }
}
