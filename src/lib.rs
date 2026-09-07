mod spec;
mod stream;
mod walk;

use std::sync::{Arc, OnceLock};

use pyo3::exceptions::{PyRuntimeError, PyTypeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyByteArray, PyBytes, PyMemoryView, PyString};

use crate::spec::Spec;
use crate::stream::{Engine, Error as StreamError, StreamPlan};
use crate::walk::{project_bytes, WalkError};

/// Coerce the accepted input types to `bytes`: `bytes` as-is (so passthrough returns the identical object),
/// `str` UTF-8 encoded, `bytearray`/`memoryview` via `bytes(data)`. The limited API (abi3-py39) has no
/// buffer protocol and no `PyString::to_str`, hence this shape.
fn to_bytes<'py>(data: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyBytes>> {
    if let Ok(b) = data.cast::<PyBytes>() {
        return Ok(b.clone());
    }
    if let Ok(s) = data.cast::<PyString>() {
        return s.encode_utf8();
    }
    if data.is_instance_of::<PyByteArray>() || data.is_instance_of::<PyMemoryView>() {
        return Ok(data
            .py()
            .get_type::<PyBytes>()
            .call1((data,))?
            .cast_into::<PyBytes>()?);
    }
    Err(PyTypeError::new_err(
        "data must be bytes, bytearray, memoryview or str",
    ))
}

fn run<'py>(
    py: Python<'py>,
    data: &Bound<'py, PyAny>,
    spec: &Spec,
    strict: bool,
) -> PyResult<Bound<'py, PyBytes>> {
    let input = to_bytes(data)?;
    match project_bytes(input.as_bytes(), spec) {
        Ok(out) => Ok(PyBytes::new(py, &out)),
        Err(e) if strict => Err(PyValueError::new_err(match e {
            WalkError::NotAnObject => "the JSON root is not an object".to_string(),
            WalkError::Json(err) => format!(
                "invalid JSON: {}",
                err.description(&jiter::Jiter::new(input.as_bytes()))
            ),
        })),
        Err(_) => Ok(input),
    }
}

/// A compiled keep-spec. Build once, apply to many documents.
#[pyclass(frozen, module = "json_projection")]
pub struct Projection {
    spec: Spec,
    repr: String,
    stream_plan: OnceLock<Arc<StreamPlan>>,
}

#[pymethods]
impl Projection {
    #[new]
    fn new(spec: &Bound<'_, PyAny>) -> PyResult<Self> {
        Ok(Projection {
            spec: Spec::from_py_root(spec)?,
            repr: format!("Projection({})", spec.repr()?),
            stream_plan: OnceLock::new(),
        })
    }

    /// Return `data` with every member not named in the spec removed.
    #[pyo3(signature = (data, *, strict = false))]
    fn apply<'py>(
        &self,
        py: Python<'py>,
        data: &Bound<'py, PyAny>,
        strict: bool,
    ) -> PyResult<Bound<'py, PyBytes>> {
        run(py, data, &self.spec, strict)
    }

    #[pyo3(signature = (data, *, strict = false))]
    fn __call__<'py>(
        &self,
        py: Python<'py>,
        data: &Bound<'py, PyAny>,
        strict: bool,
    ) -> PyResult<Bound<'py, PyBytes>> {
        run(py, data, &self.spec, strict)
    }

    fn __repr__(&self) -> &str {
        &self.repr
    }

    /// Start an independent, strict streaming projection of one JSON object.
    fn stream(&self) -> ProjectionStream {
        let plan = self
            .stream_plan
            .get_or_init(|| Arc::new(StreamPlan::new(&self.spec)));
        ProjectionStream {
            engine: Engine::new(plan.clone()),
        }
    }
}

/// A single JSON document, fed in binary chunks. Construct with `Projection.stream()`.
#[pyclass(module = "json_projection")]
pub struct ProjectionStream {
    engine: Engine,
}

#[pymethods]
impl ProjectionStream {
    /// Consume a binary chunk without retaining it. Malformed JSON raises ValueError.
    fn feed(&mut self, chunk: &Bound<'_, PyBytes>) -> PyResult<()> {
        self.engine.feed(chunk.as_bytes()).map_err(stream_error)
    }

    /// Finish the document and return its projected bytes. The session cannot be reused.
    fn finish<'py>(&mut self, py: Python<'py>) -> PyResult<Bound<'py, PyBytes>> {
        let output = self.engine.finish().map_err(stream_error)?;
        Ok(PyBytes::new(py, &output))
    }
}

fn stream_error(error: StreamError) -> PyErr {
    match error {
        StreamError::Parse { offset, message } => {
            PyValueError::new_err(format!("invalid JSON at byte {offset}: {message}"))
        }
        StreamError::Closed => {
            PyRuntimeError::new_err("projection stream is already finished or failed")
        }
    }
}

/// One-shot projection: compile `spec` and apply it to `data`.
#[pyfunction]
#[pyo3(signature = (data, spec, *, strict = false))]
fn project<'py>(
    py: Python<'py>,
    data: &Bound<'py, PyAny>,
    spec: &Bound<'py, PyAny>,
    strict: bool,
) -> PyResult<Bound<'py, PyBytes>> {
    run(py, data, &Spec::from_py_root(spec)?, strict)
}

#[pymodule]
fn _core(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("__version__", env!("CARGO_PKG_VERSION"))?;
    m.add_class::<Projection>()?;
    m.add_class::<ProjectionStream>()?;
    m.add_function(wrap_pyfunction!(project, m)?)?;
    Ok(())
}
