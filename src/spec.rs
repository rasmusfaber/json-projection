use std::collections::HashMap;

use pyo3::exceptions::PyTypeError;
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyDict, PyString};

/// What to do with a JSON value at some position in the document.
#[derive(Debug, Clone, PartialEq)]
pub enum Spec {
    /// Copy the value as raw bytes.
    Keep,
    /// The value is an object: keep listed members (each with its own spec), drop the rest.
    Object(HashMap<String, Spec>),
    /// The value is an array: apply the inner spec to every element.
    Array(Box<Spec>),
}

/// Deepest spec nesting accepted. The conversion below and the walk both recurse per level, so the cap
/// keeps a pathological spec (a self-referential mapping, say) from overflowing the stack. jiter refuses
/// documents nested deeper than its own recursion limit of 200 (201 at the edge), so no spec deeper than
/// that can ever match input; 256 leaves headroom while bounding the walk's worst-case stack to about a
/// quarter of what 1000 needed.
const MAX_DEPTH: usize = 256;

impl Spec {
    /// Compile a root spec: an iterable of `str`, or a mapping. The root always describes an object.
    pub fn from_py_root(obj: &Bound<'_, PyAny>) -> PyResult<Spec> {
        match Self::from_py(obj, true, 0)? {
            spec @ Spec::Object(_) => Ok(spec),
            _ => Err(PyTypeError::new_err(
                "the root spec must be a set of keys or a mapping describing an object",
            )),
        }
    }

    fn from_py(obj: &Bound<'_, PyAny>, root: bool, depth: usize) -> PyResult<Spec> {
        if depth > MAX_DEPTH {
            return Err(PyTypeError::new_err(format!(
                "spec nesting exceeds {MAX_DEPTH} levels"
            )));
        }
        if let Ok(b) = obj.cast::<PyBool>() {
            if b.is_true() && !root {
                return Ok(Spec::Keep);
            }
            return Err(PyTypeError::new_err(
                "spec values must be True, a mapping, or {\"__all__\": spec}",
            ));
        }
        if !obj.is_instance_of::<PyDict>() && obj.hasattr("keys")? {
            // any other Mapping (MappingProxyType, custom mappings): compile once through a dict copy
            let as_dict = obj.py().get_type::<PyDict>().call1((obj,))?;
            return Self::from_py(&as_dict, root, depth);
        }
        if let Ok(dict) = obj.cast::<PyDict>() {
            if dict.len() == 1 {
                if let Some(inner) = dict.get_item("__all__")? {
                    if root {
                        return Err(PyTypeError::new_err(
                            "\"__all__\" is not allowed at the root; the root must be an object",
                        ));
                    }
                    return Ok(Spec::Array(Box::new(Self::from_py(
                        &inner,
                        false,
                        depth + 1,
                    )?)));
                }
            }
            let mut map = HashMap::with_capacity(dict.len());
            for (k, v) in dict.iter() {
                let key = k
                    .cast::<PyString>()
                    .map_err(|_| PyTypeError::new_err("spec keys must be str"))?
                    .to_cow()? // to_str() is unavailable under the abi3-py39 limited API
                    .into_owned();
                if key == "__all__" {
                    return Err(PyTypeError::new_err(
                        "\"__all__\" cannot be combined with other keys",
                    ));
                }
                map.insert(key, Self::from_py(&v, false, depth + 1)?);
            }
            return Ok(Spec::Object(map));
        }
        if obj.cast::<PyString>().is_ok() {
            return Err(PyTypeError::new_err(
                "a spec must be an iterable of keys or a mapping, not a single str",
            ));
        }
        // any other iterable: a collection of root keys, each kept whole
        let iter = obj
            .try_iter()
            .map_err(|_| PyTypeError::new_err("a spec must be an iterable of keys or a mapping"))?;
        let mut map = HashMap::new();
        for item in iter {
            let item = item?;
            let key = item
                .cast::<PyString>()
                .map_err(|_| PyTypeError::new_err("spec keys must be str"))?
                .to_cow()?
                .into_owned();
            map.insert(key, Spec::Keep);
        }
        Ok(Spec::Object(map))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use pyo3::types::PyList;

    fn compile(py: Python<'_>, code: &str) -> PyResult<Spec> {
        let obj = py.eval(&std::ffi::CString::new(code).unwrap(), None, None)?;
        Spec::from_py_root(&obj)
    }

    #[test]
    fn set_of_keys_keeps_each_raw() {
        Python::attach(|py| {
            let spec = compile(py, r#"{"a", "b"}"#).unwrap();
            let mut expected = HashMap::new();
            expected.insert("a".to_string(), Spec::Keep);
            expected.insert("b".to_string(), Spec::Keep);
            assert_eq!(spec, Spec::Object(expected));
        });
    }

    #[test]
    fn list_of_keys_also_accepted() {
        Python::attach(|py| {
            let spec = compile(py, r#"["only"]"#).unwrap();
            assert_eq!(
                spec,
                Spec::Object(HashMap::from([("only".to_string(), Spec::Keep)]))
            );
        });
    }

    #[test]
    fn nested_mapping_with_all() {
        Python::attach(|py| {
            let spec =
                compile(py, r#"{"name": True, "items": {"__all__": {"id": True}}}"#).unwrap();
            let inner = Spec::Object(HashMap::from([("id".to_string(), Spec::Keep)]));
            let expected = Spec::Object(HashMap::from([
                ("name".to_string(), Spec::Keep),
                ("items".to_string(), Spec::Array(Box::new(inner))),
            ]));
            assert_eq!(spec, expected);
        });
    }

    #[test]
    fn all_combined_with_other_keys_is_an_error() {
        Python::attach(|py| {
            let err = compile(py, r#"{"items": {"__all__": True, "x": True}}"#).unwrap_err();
            assert!(err.is_instance_of::<PyTypeError>(py));
        });
    }

    #[test]
    fn root_must_be_object_spec() {
        Python::attach(|py| {
            assert!(compile(py, "True")
                .unwrap_err()
                .is_instance_of::<PyTypeError>(py));
            assert!(compile(py, r#"{"__all__": True}"#)
                .unwrap_err()
                .is_instance_of::<PyTypeError>(py));
            assert!(compile(py, "42")
                .unwrap_err()
                .is_instance_of::<PyTypeError>(py));
        });
    }

    #[test]
    fn bad_leaf_values_are_errors() {
        Python::attach(|py| {
            assert!(compile(py, r#"{"a": False}"#)
                .unwrap_err()
                .is_instance_of::<PyTypeError>(py));
            assert!(compile(py, r#"{"a": 1}"#)
                .unwrap_err()
                .is_instance_of::<PyTypeError>(py));
            assert!(compile(py, r#"{1: True}"#)
                .unwrap_err()
                .is_instance_of::<PyTypeError>(py));
        });
    }

    #[test]
    fn non_dict_mappings_are_accepted() {
        Python::attach(|py| {
            let spec = compile(
                py,
                r#"__import__("types").MappingProxyType({"a": {"x": True}})"#,
            )
            .unwrap();
            let inner = Spec::Object(HashMap::from([("x".to_string(), Spec::Keep)]));
            assert_eq!(
                spec,
                Spec::Object(HashMap::from([("a".to_string(), inner)]))
            );
        });
    }

    #[test]
    fn deeper_than_the_cap_is_an_error() {
        Python::attach(|py| {
            let nest = |n: usize| {
                format!("__import__('functools').reduce(lambda d,_: {{'k': d}}, range({n}), True)")
            };
            assert!(compile(py, &nest(MAX_DEPTH)).is_ok());
            assert!(compile(py, &nest(2000))
                .unwrap_err()
                .is_instance_of::<PyTypeError>(py));
        });
    }

    #[test]
    fn empty_specs_are_valid_and_keep_nothing() {
        Python::attach(|py| {
            assert_eq!(compile(py, "set()").unwrap(), Spec::Object(HashMap::new()));
            assert_eq!(compile(py, "{}").unwrap(), Spec::Object(HashMap::new()));
            let _ = PyList::empty(py);
        });
    }
}
