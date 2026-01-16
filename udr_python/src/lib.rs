use pyo3::prelude::*;
use pyo3::exceptions::PyIOError;
use udr_core::ChunkStore;

#[pyclass]
struct PyChunkStore {
    inner: ChunkStore,
}

#[pymethods]
impl PyChunkStore {
    #[new]
    fn new(path: &str) -> PyResult<Self> {
        let inner = ChunkStore::new(path)
            .map_err(|e| PyIOError::new_err(e.to_string()))?;
        Ok(Self { inner })
    }

    fn put(&self, data: &[u8]) -> PyResult<String> {
        self.inner
            .put(data)
            .map_err(|e| PyIOError::new_err(e.to_string()))
    }

    fn get(&self, hash: &str) -> PyResult<Vec<u8>> {
        self.inner
            .get(hash)
            .map_err(|e| PyIOError::new_err(e.to_string()))
    }

    fn exists(&self, hash: &str) -> bool {
        self.inner.exists(hash)
    }

    fn delete(&self, hash: &str) -> PyResult<()> {
        self.inner
            .delete(hash)
            .map_err(|e| PyIOError::new_err(e.to_string()))
    }
}

#[pymodule]
fn udr(_py: Python, m: &PyModule) -> PyResult<()> {
    m.add_class::<PyChunkStore>()?;
    Ok(())
}
