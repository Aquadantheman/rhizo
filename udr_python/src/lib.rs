use pyo3::prelude::*;
use pyo3::exceptions::{PyIOError, PyValueError};
use udr_core::{ChunkStore, ChunkStoreError, FileCatalog, CatalogError, TableVersion};
use std::collections::HashMap;

/// Convert ChunkStoreError to appropriate Python exception
fn chunk_err_to_py(e: ChunkStoreError) -> PyErr {
    match e {
        ChunkStoreError::NotFound(h) => PyIOError::new_err(format!("Chunk not found: {}", h)),
        ChunkStoreError::InvalidHash(msg) => PyValueError::new_err(format!("Invalid hash: {}", msg)),
        ChunkStoreError::HashMismatch { expected, actual } => {
            PyValueError::new_err(format!("Hash mismatch: expected {}, got {}", expected, actual))
        }
        ChunkStoreError::Io(e) => PyIOError::new_err(e.to_string()),
    }
}

/// Convert CatalogError to appropriate Python exception
fn catalog_err_to_py(e: CatalogError) -> PyErr {
    match e {
        CatalogError::TableNotFound(t) => PyIOError::new_err(format!("Table not found: {}", t)),
        CatalogError::VersionNotFound(t, v) => {
            PyIOError::new_err(format!("Version not found: {} v{}", t, v))
        }
        CatalogError::InvalidVersion { expected, got } => {
            PyValueError::new_err(format!("Invalid version: expected {}, got {}", expected, got))
        }
        CatalogError::LatestPointerCorrupted(t) => {
            PyIOError::new_err(format!("Latest pointer corrupted for table: {}", t))
        }
        CatalogError::Io(e) => PyIOError::new_err(e.to_string()),
        CatalogError::Json(e) => PyValueError::new_err(format!("JSON error: {}", e)),
    }
}

#[pyclass]
struct PyChunkStore {
    inner: ChunkStore,
}

#[pymethods]
impl PyChunkStore {
    #[new]
    fn new(path: &str) -> PyResult<Self> {
        let inner = ChunkStore::new(path).map_err(chunk_err_to_py)?;
        Ok(Self { inner })
    }

    fn put(&self, data: &[u8]) -> PyResult<String> {
        self.inner.put(data).map_err(chunk_err_to_py)
    }

    fn get(&self, hash: &str) -> PyResult<Vec<u8>> {
        self.inner.get(hash).map_err(chunk_err_to_py)
    }

    /// Get chunk data with integrity verification.
    /// Raises ValueError if the data doesn't match the expected hash.
    fn get_verified(&self, hash: &str) -> PyResult<Vec<u8>> {
        self.inner.get_verified(hash).map_err(chunk_err_to_py)
    }

    fn exists(&self, hash: &str) -> PyResult<bool> {
        self.inner.exists(hash).map_err(chunk_err_to_py)
    }

    fn delete(&self, hash: &str) -> PyResult<()> {
        self.inner.delete(hash).map_err(chunk_err_to_py)
    }
}

#[pyclass]
#[derive(Clone)]
struct PyTableVersion {
    #[pyo3(get)]
    table_name: String,
    #[pyo3(get)]
    version: u64,
    #[pyo3(get)]
    chunk_hashes: Vec<String>,
    #[pyo3(get)]
    schema_hash: Option<String>,
    #[pyo3(get)]
    created_at: i64,
    #[pyo3(get)]
    parent_version: Option<u64>,
    #[pyo3(get)]
    metadata: HashMap<String, String>,
}

#[pymethods]
impl PyTableVersion {
    #[new]
    fn new(table_name: String, version: u64, chunk_hashes: Vec<String>) -> Self {
        Self {
            table_name,
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
}

impl From<TableVersion> for PyTableVersion {
    fn from(tv: TableVersion) -> Self {
        Self {
            table_name: tv.table_name,
            version: tv.version,
            chunk_hashes: tv.chunk_hashes,
            schema_hash: tv.schema_hash,
            created_at: tv.created_at,
            parent_version: tv.parent_version,
            metadata: tv.metadata,
        }
    }
}

impl From<PyTableVersion> for TableVersion {
    fn from(ptv: PyTableVersion) -> Self {
        Self {
            table_name: ptv.table_name,
            version: ptv.version,
            chunk_hashes: ptv.chunk_hashes,
            schema_hash: ptv.schema_hash,
            created_at: ptv.created_at,
            parent_version: ptv.parent_version,
            metadata: ptv.metadata,
        }
    }
}

#[pyclass]
struct PyCatalog {
    inner: FileCatalog,
}

#[pymethods]
impl PyCatalog {
    #[new]
    fn new(path: &str) -> PyResult<Self> {
        let inner = FileCatalog::new(path).map_err(catalog_err_to_py)?;
        Ok(Self { inner })
    }

    fn commit(&self, version: PyTableVersion) -> PyResult<u64> {
        self.inner.commit(version.into()).map_err(catalog_err_to_py)
    }

    #[pyo3(signature = (table_name, version=None))]
    fn get_version(&self, table_name: &str, version: Option<u64>) -> PyResult<PyTableVersion> {
        self.inner
            .get_version(table_name, version)
            .map(|tv| tv.into())
            .map_err(catalog_err_to_py)
    }

    fn list_versions(&self, table_name: &str) -> PyResult<Vec<u64>> {
        self.inner.list_versions(table_name).map_err(catalog_err_to_py)
    }

    fn list_tables(&self) -> PyResult<Vec<String>> {
        self.inner.list_tables().map_err(catalog_err_to_py)
    }
}

#[pymodule]
fn udr(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<PyChunkStore>()?;
    m.add_class::<PyTableVersion>()?;
    m.add_class::<PyCatalog>()?;
    Ok(())
}
