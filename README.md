# json-projection

Strip the parts of a JSON document you do not want **before** a parser or validator sees them.
Kept members are copied byte-for-byte; everything else is skipped by [jiter](https://github.com/pydantic/jiter)'s
cursor without ever being parsed. Zero runtime dependencies. Optional [pydantic](https://docs.pydantic.dev) integration
that excludes fields on any model, including models from libraries you do not control.

## Why

pydantic parses the whole document into a tree before validating, then discards what the model does not need.
For payloads whose bulk sits under unwanted keys that is most of the cost:

| shape (~1 MB) | `Model.model_validate_json` | with json-projection |
|---|---:|---:|
| big array under a key the model ignores (S1) | 4.363 ms / 25.90 MiB | 0.364 ms / 0.01 MiB |
| big nested field excluded inside a list (S3) | 13.223 ms / 41.33 MiB | 0.744 ms / 0.24 MiB |
| kubernetes-like list, drop spec/status (S12) | 9.794 ms / 18.52 MiB | 1.363 ms / 0.51 MiB |
| everything kept, nothing to skip (S7) | 5.922 ms / 8.50 MiB | 7.006 ms / 9.91 MiB |

Full table and method in [`benchmarks/results.md`](benchmarks/results.md).

## Install

    pip install json-projection              # projector only
    pip install 'json-projection[pydantic]'  # plus the pydantic integration (pydantic >= 2.13)

## Usage

```python
import json_projection as jp

jp.project(raw, {"name", "id"})  # keep two root keys
p = jp.Projection({"name": True, "items": {"__all__": {"id": True}}})  # compile once
p(raw)  # keep items[*].id and name
```

A spec is a set of root keys, or a mapping where a key maps to `True` (keep the whole value), a nested mapping
(descend into an object) or `{"__all__": spec}` (apply to each array element). Everything not named is dropped.

### With pydantic

```python
from json_projection.pydantic import Projected

thin = Projected(Log, exclude={Log: {"debug"}, Sample: {"events"}})
log = thin.validate_json(raw)  # real Log/Sample instances; excluded fields are never parsed
```

Excluded fields that have a default keep their default. Excluded required fields are absent from the instance
and from `model_fields_set`. `exclude` is keyed by class so nested third-party models can be targeted; a plain
set means the root model.

## What you give up

- **Error payloads show the projected document.** A `ValidationError` on a whole object reports the input
  without the dropped members.
- **UTF-8 inside dropped data is not validated.** Escapes, control characters, numbers and nesting still are.
- **Nesting depth:** a document exactly 201 levels deep is accepted where pydantic alone rejects it.
- **Kept data is copied once.** When little can be dropped, or a huge string sits under a dropped key, the copy
  costs more than it saves (see the last benchmark row).
- **Partial instances:** a model with excluded required fields should not also appear inside a union; the
  serializer warns about the missing fields there.
- The pydantic integration relies on the core-schema layout and on `SchemaValidator(..., _use_prebuilt=False)`,
  which pydantic does not promise to keep. CI tests the latest release and pre-release; a `RuntimeError` is
  raised if pydantic-core ignores the schema edit.
- **Refused configurations.** `projected_validator` on its own refuses, with `ValueError`, models whose config would still consume an excluded key: `extra='forbid'` or `extra='allow'`, and `populate_by_name`/`validate_by_name` when an excluded field has a default. `Projected` lifts that restriction because its byte projection removes the key first, except for recursive models where the derived spec cannot reach every occurrence; those stay refused.
- **Before/wrap model validators.** A `model_validator(mode='before')` or `mode='wrap'` that reads keys the
  model does not declare will not see them: the projection keeps only declared fields. Exclude nothing on such
  a model or use `projected_validator` alone.
- **After validators and excluded required fields.** A `model_validator(mode='after')` that touches an excluded
  required field raises `AttributeError` from `validate_json`, not a `ValidationError`.
- **Extras.** A model with `extra='allow'` that has no excluded field of its own is kept whole (nothing below it
  is projected). A model with `extra='allow'` that does have excluded fields loses all its extras: the
  projection keeps only declared fields.
- **Standalone `projected_validator`.** Excluded fields with a default are redirected to the alias
  `\x00excluded:<name>`; a document that contains that literal key still populates the field. `Projected`
  strips it.
- **Recursion.** When any model in the schema recurses, the derived projection stops at the first repetition and
  `Projected` keeps the standalone guards on for every class in the schema, so a non-recursive `extra='forbid'`
  root is refused if an unrelated recursive class is nested under it.

## Errors

On invalid JSON, or a root that is not an object, `project` returns the input unchanged so that whatever
parses it next reports the error at the original position. Pass `strict=True` to raise `ValueError` instead.

## Supported

CPython 3.9 to 3.14 (one abi3 wheel per platform); Linux x86_64 and aarch64, macOS, Windows. Free-threaded
builds are not supported yet.

## Releasing

1. Bump `version` in `Cargo.toml`, move the changelog entry from "unreleased" to the date, commit.
2. `git tag vX.Y.Z && git push --tags`. The release workflow builds wheels for Linux x86_64/aarch64, macOS
   arm64/x86_64 and Windows x64 plus the sdist, and publishes via PyPI trusted publishing.

One-time setup before the first tag: create the `json-projection` project on PyPI, add a trusted publisher for
`rasmusfaber/json-projection` with workflow `release.yml` and environment `pypi`, and create the `pypi`
environment in the GitHub repository settings.
