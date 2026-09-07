# json-projection

Strip the parts of a JSON document you do not want **before** a parser or validator sees them.
Kept members are copied byte-for-byte; unwanted values are scanned without constructing them.
Whole-buffer projection uses [jiter](https://github.com/pydantic/jiter)'s cursor; streaming projection uses an
incremental Rust scanner. Zero runtime dependencies. Optional [pydantic](https://docs.pydantic.dev) integration
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

### Streaming input

Use `Projection.apply_stream()` to project one JSON object from a binary file-like source:

```python
from json_projection import Projection

projection = Projection({"id", "scores"})
with open("sample.json", "rb") as source:
    projected = projection.apply_stream(source)
```

The source only needs a `read(size) -> bytes` method, so binary files, `BytesIO`, compressed files, and
custom readers work. The helper reads from the current position until `b""` signals EOF, allowing short
reads and leaving the source open. `chunk_size` is a positive integer and defaults to 65536 bytes;
`projection.apply_stream(source, chunk_size=4096)` changes the requested read size. Non-bytes read results
raise `TypeError`, including text streams and `None` from a nonblocking reader. Read errors propagate.

The helper uses the same strict scanner and buffered output as the session API below. JSON error offsets
start at zero for the bytes read by this call, even when the source begins at a nonzero position. On error,
the source remains at the position reached by its last read.

Use `Projection.stream()` to supply chunks yourself:

```python
from json_projection import Projection

projection = Projection({"id", "scores"})
session = projection.stream()
with open("sample.json", "rb") as source:
    while True:
        chunk = source.read(64 * 1024)
        if not chunk:
            break
        session.feed(chunk)
projected = session.finish()
```

`feed(chunk: bytes)` returns `None` and consumes the chunk without retaining it. Empty chunks are allowed;
other input types raise `TypeError`. `finish()` verifies that the complete document has arrived and returns
the projected `bytes`, ready for `json.loads()` or a model's ordinary `model_validate_json()` call. Callers
provide their own file, network, or async iteration; the session performs no I/O.

The same projection can create independent, interleaved sessions. A session owns its shared selection plan
and remains usable after the `Projection` is dropped. Each session handles exactly one root object and
cannot be reused after `finish()` or a JSON error. Construct sessions with `Projection.stream()`; the exported
`ProjectionStream` type has no public constructor.

Streaming always raises `ValueError` for malformed JSON, including malformed discarded values, non-object
roots, trailing non-whitespace input, or incomplete input at `finish()`. Errors include a zero-based byte
offset in the original input, independent of chunk boundaries. Subsequent `feed(bytes)` or `finish()` calls
on a finished or failed session raise `RuntimeError`. Passing a non-bytes chunk always raises `TypeError`
and leaves an active session usable.
There is no invalid-input passthrough: the original discarded bytes are no longer available.

Selection rules, duplicate-member order, raw key/value spelling, and `NaN`/`Infinity` support match
whole-buffer projection. Streaming limits nesting to **200 containers including the root**, across retained
and discarded data. Escapes, surrogate pairs, control characters, and number grammar are checked everywhere;
raw UTF-8 is checked when decoding keys for selection. Raw UTF-8 in value strings and in keys inside wholly
copied or discarded subtrees is left to the downstream parser, matching the whole-buffer scanner.

Discarded strings, numbers, and containers are never buffered in full. Memory is used for the caller's chunk,
the nesting stack, the longest lookup key, and the **retained JSON output**. The output is buffered until
`finish()`; creating the returned Python bytes briefly requires a second output buffer. Keeping most of a
large document therefore still takes substantial memory.

### With pydantic

```python
from json_projection.pydantic import Projected

thin = Projected(Log, exclude={Log: {"debug"}, Sample: {"events"}})
log = thin.validate_json(raw)  # real Log/Sample instances; excluded fields are never parsed
```

Excluded fields that have a default keep their default. Excluded required fields are absent from the instance
and from `model_fields_set`. `exclude` is keyed by class so nested third-party models can be targeted; a plain
set means the root model.

### Migration validators

A model whose fields sit behind a `model_validator(mode='before')` or `mode='wrap'` reads keys the schema
does not declare: an old field name, a legacy `transcript` object, a `version` it dispatches on. The
projection cannot know those keys, so such a class is kept whole -- or refused at the root -- unless you
register a *projection adapter* that translates the fields retained after validation into the JSON inputs
the validators need:

```python
from json_projection.pydantic import Projected, migration_adapter

sample_adapter = migration_adapter(
    inputs={  # retained field -> JSON paths a migration may read for it
        "scores": ["score"],  # legacy single score
        "events": [("transcript", "events")],  # legacy transcript held events and attachments
        "attachments": [("transcript", "content")],
    },
    controls=["version"],  # keys a validator dispatches on: kept whenever present
    requires={"timelines": ["events"]},  # retained field -> retained fields it depends on
)

thin = Projected(
    Log, exclude={Sample: {"events", "attachments"}}, projection_adapters={Sample: sample_adapter}
)
```

`inputs` are merged only for retained fields, so excluding `events` and `attachments` also drops
`transcript`. `controls` are kept whenever present. Retaining a field whose dependency is excluded makes
`Projected(...)` raise `ValueError`. With the inspect_ai adapters that means `timelines` must go whenever
`events` does. Anything the helper cannot express is a plain callable
`adapter(ctx: AdapterContext) -> Mapping[str, Any]`: `ctx.fields` are the retained field names and
`ctx.spec` the projection derived for them, to modify and return -- a fresh dict per occurrence. Adapters
run once per occurrence of the class, including occurrences inside subtrees the projection has to keep
whole (there the derived spec is discarded, but a `requires` conflict is still reported), and must be
pure. A bare string where a collection of paths or names is expected -- `inputs={"name": "old_name"}`,
`controls="version"`, `requires={"a": "b"}` -- is a `TypeError`: it would iterate as characters. The
validators themselves still perform the migration; the projection only makes sure they see what they need.

Adapters are looked up by **exact class**: a subclass needs its own entry, and registering one for a class
that does not appear in the root model's schema raises `ValueError` rather than going silently unused. The
same adapter object can of course be registered under several classes.

Adapted classes keep the config guards on (see "Refused configurations") because a migration can recreate
an excluded key from the inputs it was given. `examples/inspect_adapters.py` holds adapters for inspect_ai's
`EvalLog`.

## What you give up

- **Error payloads show the projected document.** A `ValidationError` on a whole object reports the input
  without the dropped members.
- **UTF-8 inside dropped data is not validated.** Escapes, control characters, numbers and nesting still are.
- **Whole-buffer nesting depth is not accounted globally.** Every dropped or kept member starts a fresh nesting budget
  of 200 below the member that holds it, and the containers the projector descends through cost nothing,
  so a document can be accepted at any total depth where pydantic alone rejects it for exceeding its
  recursion limit. What comes out is still bounded by pydantic's own parse of the kept members. Streaming
  instead enforces its global limit of 200 containers.
- **Kept data is copied.** When little can be dropped, or a huge string sits under a dropped key, the copy
  costs more than it saves (see the last benchmark row).
- **Partial instances:** a model with excluded required fields should not also appear inside a union; the
  serializer warns about the missing fields there.
- The pydantic integration relies on the core-schema layout and on `SchemaValidator(..., _use_prebuilt=False)`,
  which pydantic does not promise to keep. CI tests the latest release and pre-release; a `RuntimeError` is
  raised if pydantic-core ignores the schema edit.
- **Refused configurations.** `projected_validator` on its own refuses, with `ValueError`, models whose config would still consume an excluded key: `extra='forbid'` or `extra='allow'`, and `populate_by_name`/`validate_by_name` when an excluded field has a default. `Projected` lifts that restriction only when its derived projection provably reaches every occurrence of every excluded class; where it cannot (see "Kept-whole subtrees"), the guards stay on.
- **Before/wrap/plain validators are not projected through.** Their input is not the shape the schema they
  wrap describes, so a model or field behind one is kept whole and nothing inside it is projected. A root
  model whose own fields sit behind a `model_validator(mode='before')`, `'wrap'` or `'plain'` cannot be
  projected at all: `Projected` and `projection_spec` raise `TypeError`. Register a projection adapter for
  the class (see "Migration validators") to project through a before or wrap validator; a plain validator
  cannot be adapted, so use `projected_validator` alone. An adapter vouches only for the class's *own
  registered model validators* -- `@classmethod`, `@staticmethod` and inherited ones alike. A validator
  installed from outside the class stays opaque even when it is a bound method of the adapted class: a
  field validator on the enclosing model, or an `Annotated[Cls, BeforeValidator(...)]` on a field. At the
  root such a wrapper raises `TypeError` saying so, rather than asking for the adapter you already
  registered.
- **After validators and excluded required fields.** A `model_validator(mode='after')` that touches an excluded
  required field raises `AttributeError` from `validate_json`, not a `ValidationError`.
- **Extras.** A model with `extra='allow'` that has no excluded field of its own is kept whole (nothing below it
  is projected). A model with `extra='allow'` that does have excluded fields loses all its extras: the
  projection keeps only declared fields.
- **Standalone `projected_validator`.** Excluded fields with a default are redirected to the alias
  `\x00excluded:<name>`; a document that contains that literal key still populates the field. `Projected`
  strips it. Validating with `by_name=True` or `by_alias=False` on the returned `SchemaValidator` looks
  the field up under its own name again, so a document that carries the excluded key populates the field
  and the default is not applied. `Projected.validate_json` refuses both flags with `ValueError` whenever
  its projection is incomplete, and forwards them when it is not (the projection stripped alias and name
  alike). Projection adapters do not affect it: it never reads JSON.
- **Kept-whole subtrees.** The derived projection describes models, lists, sets and variable-length tuples;
  everything else is kept whole -- dict values, unions, fixed tuples, dataclasses, TypedDicts, `Any`, a class
  that appears inside itself, an `extra='allow'` model with no excluded fields of its own, the object named by
  a multi-segment alias path, a JSON key that two fields describe differently. Nothing inside a kept subtree
  is projected, so when an excluded class is reachable in one, `Projected` keeps the standalone guards on for
  every class in the schema.
- **RootModel is unsupported** in 0.1: `projection_spec` and `Projected` raise `TypeError`, and
  `projected_validator` reports the excluded names as not found. A `RootModel` declares one field, `root`,
  and the class it wraps is reached through it like any other nested model.
- **Custom `__init__`.** A model that defines its own `__init__` is refused with `ValueError` when exclusion
  would touch it or anything below it: pydantic-core calls that `__init__`, which validates through the
  class's original validator and ignores the exclusion entirely.
- **A field aliased `__all__`** cannot be expressed: `__all__` is the array wildcard in a mapping spec, so a
  derived spec containing it is rejected with `TypeError`. In a `migration_adapter` path the segment means
  "keep that container whole", so the path is truncated there; a path that *starts* with `__all__` would ask
  for the whole root object, which no spec can say, and raises `TypeError` at construction.
- **Data-dependent defaults and computed fields.** A `default_factory` that takes the validated data raises a
  plain `KeyError` from `validate_json` when it reads an excluded field, and a computed field that reads an
  excluded attribute raises `AttributeError` from `model_dump`. Neither becomes a `ValidationError`.

## Errors

On invalid JSON, or a root that is not an object, `project` returns the input unchanged so that whatever
parses it next reports the error at the original position. Pass `strict=True` to raise `ValueError` instead.
Streaming sessions always raise on errors, as described above.

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
