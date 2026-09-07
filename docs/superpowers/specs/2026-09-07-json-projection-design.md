# json-projection: design

Date: 2026-09-07
Status: approved in discussion, awaiting written review

## Problem

`Model.model_validate_json(raw)` in pydantic v2 parses the entire JSON document
into a Rust `JsonValue` tree before validating. Fields the model does not
declare (`extra='ignore'`) and fields the caller does not want are fully
materialised and then discarded. For payloads whose bulk sits under unwanted
keys this dominates both CPU time and peak memory: measured on a 1 MB document
with 50k small dicts under one unwanted key, parsing costs about 3 ms and
15 to 25 MiB of transient RSS for a result that is a single float.

No pydantic API skips fields at parse time. Excluding known fields on
third-party models today requires writing a second model. An upstream fix
exists as a prototype (root-level only) but is not merged.

## Goal

A small library that removes unwanted JSON before pydantic sees it:

1. A zero-dependency byte projector: given bytes and a keep-spec, return the
   bytes with every unwanted member skipped, never parsed, kept members copied
   verbatim.
2. An optional pydantic integration that derives the keep-spec from a model,
   excludes named fields (including on third-party models) and returns real
   instances of the original classes.

Measured on the prototypes this design is based on (stock pydantic 2.13.5):
a nested exclusion case went from 13.6 ms to 0.8 ms and a root-level case from
12.1 ms to 0.6 ms, with peak RSS growth from 25 MiB to under 1 MiB. Where
nothing can be dropped the projector costs a full copy of the document, and
that trade-off is documented rather than hidden.

## Decisions taken

| Question | Decision |
|---|---|
| Scope | One package. Core has zero runtime dependencies; `[pydantic]` extra adds the integration. |
| Name | Distribution `json-projection`, import `json_projection`. |
| Home | github.com/rasmusfaber/json-projection, local `~/src/personal/json-projection`. |
| jiter | crates.io release, `jiter = "0.16"` with `default-features = false`. Bump to 0.17 when released. |
| pydantic support | `pydantic>=2.13`. Only the `_use_prebuilt=False` path ships. |
| Shape | Compiled spec (`Projection` class) with a single `project()` entry point; lazy `json_projection.pydantic` submodule. |
| License | MIT. |
| First version | 0.1.0. |

## Public API

```python
import json_projection as jp

jp.project(data, spec, *, strict=False) -> bytes
p = jp.Projection(spec)
p(data, *, strict=False) -> bytes      # alias: p.apply(data)
```

`data` is `bytes`, `bytearray`, `memoryview` or `str` (encoded as UTF-8).
The result is always `bytes`.

### Spec

A spec is one of:

- an iterable of strings: root keys to keep, whole values copied raw;
- a mapping from key to one of `True` (keep whole value), a nested mapping
  (descend into an object), or `{"__all__": <spec>}` (apply to each array
  element).

Keys match JSON object keys after unescaping. Anything not named is dropped,
whether container, string or scalar. Wildcards over object keys, negative
specs and JSONPath are out of scope for 0.1.

### Semantics

- Kept members are copied as raw bytes in document order. All occurrences of a
  duplicated key are kept, so last-wins behaviour downstream is unchanged.
  NaN, Infinity, escapes and number spellings survive verbatim.
- Dropped members are skipped with jiter's `known_skip`. Escapes, control
  characters, number syntax and nesting are still validated; UTF-8 inside
  dropped strings is not.
- Nesting depth inside a dropped or kept member gets jiter's fresh budget of
  200 while pydantic counts the root object too, so a document whose total
  depth is exactly 201 (202 when the innermost container is empty) is accepted
  where pydantic alone rejects it. Deeper input is rejected by both; shallower
  input is accepted by both.
- On any JSON syntax error, or when the root is not an object, `project`
  returns the input bytes unchanged so the downstream parser reports the
  identical error. With `strict=True` it raises `ValueError` instead.
- Output that keeps nothing is `b"{}"`.

## Architecture

```
json-projection/
├── Cargo.toml                      # crate json_projection; jiter 0.16, pyo3 0.29 abi3-py39
├── pyproject.toml                  # maturin backend; optional-dependencies.pydantic = ["pydantic>=2.13"]
├── src/lib.rs                      # pyo3 module json_projection._core: Projection pyclass, project()
├── src/spec.rs                     # Python spec -> Rust tree: Keep | Object(map) | Array(Box<Spec>)
├── src/walk.rs                     # cursor walk over the compiled tree, raw-byte copying
├── python/json_projection/
│   ├── __init__.py                 # project, Projection, __version__
│   ├── _core.pyi                   # hand-written stubs
│   ├── py.typed
│   └── pydantic.py                 # Projected, projection_spec, projected_validator
├── tests/
├── benchmarks/                     # shape harness and committed results; not run in CI
├── .github/workflows/ci.yml, release.yml
├── README.md, LICENSE, CHANGELOG.md
```

### Rust core (`_core`)

`spec.rs` converts the Python spec once into a Rust tree. Object nodes hold a
map from key bytes to child spec; lookups during the walk are Rust string
comparisons, not Python dict probes. This is the main change from the
prototype, motivated by the wide-object shapes where per-key Python lookups
dominated.

`walk.rs` is the prototype's walk: `Jiter::new(data).with_allow_inf_nan()`,
`peek`, `next_object`/`next_key` for keys (decided against the spec before the
next cursor call, because keys borrow jiter's tape), `known_skip` plus
`current_index` slicing to copy kept values raw, `next_array`/`array_step` for
`__all__`, `finish` at the end. Only jiter 0.16.0 public API is used; the
signatures have been stable since v0.5.0.

Errors: any `JiterError` maps to passthrough (return input) or `ValueError`
under `strict`. Python errors from spec handling propagate.

### Python package

`__init__.py` re-exports `project` and `Projection` from `_core`. The
`pydantic` submodule imports pydantic lazily and raises `ImportError` with the
version requirement if pydantic is missing or older than 2.13.

## pydantic integration (`json_projection.pydantic`)

```python
from json_projection.pydantic import Projected, projection_spec, projected_validator

thin = Projected(Log, exclude={Log: {"debug"}, Sample: {"events"}})
log = thin.validate_json(raw)
thin.spec        # Projection
thin.validator   # pydantic_core.SchemaValidator
```

- `exclude` is a mapping from model class to field names, so nested third-party
  models can be targeted wherever they occur. A plain set means the root model.
- `projected_validator(model, exclude)`: structurally copies
  `model.__pydantic_core_schema__`, and for each excluded field on a matching
  `model` node either removes it from `model-fields` (required field; the
  attribute will be absent from the instance and from `model_fields_set`) or,
  when the field's schema is a `default` wrapper, keeps it and sets
  `validation_alias` to an unreachable key so pydantic applies the default.
  Builds `SchemaValidator(schema, config, _use_prebuilt=False)`. Raises
  `RuntimeError` if `"PrebuiltValidator"` appears in the validator's repr,
  because pydantic-core's prebuilt reuse otherwise ignores the edit silently.
  Raises `ValueError` if an excluded field is not on the model, and if the
  model's effective `extra` is `forbid` (an excluded key would become an
  `extra_forbidden` error).
- `projection_spec(model, exclude)`: derives the keep-spec from the same
  schema. `model` -> mapping of kept fields; list, set, frozenset and
  variable tuple -> `{"__all__": ...}`; `nullable`, `default` and function
  wrappers are looked through; `definition-ref` is resolved; a string
  `validation_alias` or `AliasChoices` becomes the JSON key(s); anything else
  (dict, union, Any, recursion) becomes `True`, which is always safe.
- `Projected` composes the two and exposes `validate_json(data, **kwargs)`
  forwarding `strict`, `context`, `by_alias`, `by_name`.

Dependencies on pydantic internals, stated in the README: the core schema
shape (`model` -> `model-fields` -> `fields`, unchanged since 2.0), the
underscore-private `_use_prebuilt` kwarg (added 2.13.0b1), and the prebuilt
reuse gate. The `RuntimeError` guard turns the known silent failure mode into a
loud one. CI against the newest pre-release is the early-warning system.

## Testing

- Rust unit tests: spec compilation from set, mapping, `__all__`, invalid
  types; walk on tricky keys, escapes, duplicates, empty containers,
  whitespace, depth boundary, error passthrough and `strict`.
- `tests/test_core.py`: a pure-Python reference projection applied to
  `json.loads` output, compared with `project()` on a generated corpus of a few
  thousand documents (NaN, escapes, unicode, duplicate keys, nesting); explicit
  edge cases for non-object roots, syntax errors and invalid UTF-8 in dropped
  data.
- `tests/test_pydantic.py`: defaults kept, required absent, aliases,
  `AliasChoices`, nested lists, recursive models, generic models, prebuilt
  guard (negative test), `extra='forbid'` refusal, `model_dump` on partial
  instances, error message parity with unprojected input, import guard.
- `tests/test_typing.py`: mypy and basedpyright over the stubs.

## CI and release

`ci.yml` on push and pull request: ubuntu, macos, windows; Python 3.9 and
3.13; the abi3 wheel built once per OS; pydantic pinned to the oldest 2.13.x
and to latest, plus one job with pre-releases allowed; ruff, cargo fmt, cargo
clippy with warnings denied.

`release.yml` on a `v*` tag: `PyO3/maturin-action` builds wheels for Linux
x86_64 and aarch64 (manylinux2014), macOS arm64 and x86_64, Windows x64, plus
the sdist, then publishes through PyPI trusted publishing. Version is read
from `Cargo.toml`. Free-threaded CPython wheels are out of scope for 0.1 and
noted in the README.

## Documentation

README: problem statement, install, two-line usage, pydantic example, the
benchmark table (12 shapes, naive vs projector vs pydantic-core patch), a
"when it helps and when it costs" paragraph, and a "what you give up" section
covering: error payloads show the projected document; UTF-8 in dropped data is
unchecked; the depth-200 edge; kept data is copied once; partial instances and
union-member models. CHANGELOG from 0.1.0.

## Out of scope for 0.1

Object-key wildcards, negative specs, JSONPath, streaming input, free-threaded
wheels, pydantic below 2.13, exposing the `Projection` tree for
introspection, and any upstream pydantic-core changes.
