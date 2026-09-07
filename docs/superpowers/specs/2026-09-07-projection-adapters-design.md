# Projection adapters for models with migration validators: design

Date: 2026-09-07
Status: approved in discussion, awaiting written review
Extends: `2026-09-07-json-projection-design.md` (json-projection 0.1.0)
Target version: 0.2.0

## Problem

json-projection 0.1.0 treats a model whose fields sit behind a `model_validator(mode='before')` or
`mode='wrap')` as opaque: nested, it is kept whole and nothing inside it is projected; at the root,
`Projected` raises `TypeError`. The reason is sound. Such a validator reads the raw input dict, and the
current schema does not say which keys it reads. A migration that turns a legacy `transcript` object into
`events` and `attachments`, or a legacy `score` into `scores`, needs those legacy keys to survive
projection, and the schema never mentions them.

inspect_ai's `EvalLog` and `EvalSample` are exactly this case: several before validators migrate old log
formats by field presence and value type, one wrap validator resolves references between `timelines` and
`events`, and the models are third-party, so they cannot be annotated. Excluding the per-sample bulk
fields (`events`, `messages`, `store`, `attachments`) while still loading both old and new logs is the
motivating use.

The knowledge that is missing is a relationship, per class, between "fields retained after validation"
and "JSON inputs the validators need before validation". Nothing in the schema carries it; it has to come
from the caller.

## Goal

1. Let a caller register a *projection adapter* per class. The adapter translates the set of retained
   fields into the JSON input spec for that object, including legacy paths, control keys and
   dependencies. The model's own validators still perform the migration; the projection only makes sure
   they see what they need.
2. Provide a declarative helper that builds an adapter from relationships (alternate input paths,
   control keys, dependencies) so the common cases need no code. Anything else uses a hand-written adapter.
3. Keep exclusion enforcement conservative: adapted classes keep the 0.1 config guards. No Rust change.

Correctness statement, which the test suite must exercise as a property:

> For every document that ordinary validation accepts, projected validation produces the same values for
> every retained field.

## Decisions taken

| Question | Decision |
|---|---|
| Where the knowledge lives | A caller-supplied adapter per class, passed as `projection_adapters`. No heuristic "keep undeclared keys" mode and no "allow before validators" flag: neither carries enough information to be correct. |
| Adapter input | A frozen `AdapterContext(model, fields, spec)`. |
| Adapter output | A mapping in the existing spec grammar, the JSON input spec for that object. |
| Dependencies | Retaining a field that depends on an excluded one is a construction-time `ValueError`. Reading a dependency without retaining it is out of scope. |
| Exclusion enforcement | An adapted class whose exclusions could be recreated by a migration marks the derivation incomplete, so the guards and the runtime `by_name` refusal stay active. Filtering after before-validators is future work. |
| Rust | Unchanged. Presence of emptied objects is already preserved: a kept object whose members are all dropped is emitted as `{}`. |
| Version | 0.2.0 (new public API). |

## Public API

```python
from json_projection.pydantic import (
    AdapterContext, ProjectionAdapter, migration_adapter,   # new
    Projected, projection_spec, projected_validator,        # existing
)

Projected(model, exclude, *, projection_adapters=None)
projection_spec(model, exclude, *, projection_adapters=None)
```

`projection_adapters: Mapping[type, ProjectionAdapter] | None`. `projected_validator` is unchanged:
adapters affect only the byte projection.

### `AdapterContext`

```python
@dataclass(frozen=True)
class AdapterContext:
    model: type[BaseModel]      # the class this JSON object validates into
    fields: frozenset[str]      # declared fields retained after exclusion, by field name
    spec: dict[str, Any]        # the derived spec for those fields; keys are JSON keys (aliases applied)
```

`spec` is a fresh dict per call; the adapter may modify and return it.

### `ProjectionAdapter`

```python
ProjectionAdapter = Callable[[AdapterContext], Mapping[str, Any]]
```

The return value is the JSON input spec for the object: a mapping from JSON key to `True`, a nested
mapping, or `{"__all__": spec}`, exactly the 0.1 grammar. The adapter may raise `ValueError` to report an
exclusion set the migration cannot support; the error propagates from `Projected(...)` and
`projection_spec(...)`. Adapters are called once per occurrence of the class in the schema and must be
pure functions of their context.

### `migration_adapter`

```python
Path = str | tuple[str, ...]      # one JSON key, or a nested key path

def migration_adapter(
    *,
    inputs: Mapping[str, Iterable[Path]] | None = None,
    controls: Iterable[Path] = (),
    requires: Mapping[str, Iterable[str]] | None = None,
) -> ProjectionAdapter
```

- `inputs`: retained field name -> JSON paths a migration may read to produce it. Covers alternate legacy
  locations (`"events": [("transcript", "events")]`) and fallbacks (`"task_args_passed": ["task_args"]`)
  alike. A path is merged into the derived spec only when its field is retained.
- `controls`: JSON paths kept whenever present, regardless of exclusions: version keys a validator
  dispatches on, and keys a validator checks for mutual exclusion (`score` next to `scores`).
- `requires`: retained field name -> field names it depends on. If a retained field's dependency is not
  retained, construction fails with `ValueError` naming class, field and dependency.

Merging a path into the spec, segment by segment: a missing segment is created; a segment already `True`
stays `True` (the subtree is kept whole); the leaf becomes `True`; any conflict (a segment that meets a
`{"__all__": ...}` spec, or a leaf that meets a nested mapping) widens that node to `True`. Widening only
ever keeps more, so it is always safe.

Field names given to `inputs` or `requires` (keys and dependency values) that are not declared on
`ctx.model` raise `ValueError` when the adapter runs, so typos surface at construction.

## Derivation semantics

In `_derive_spec`, in the `model` branch, after the recursion and `extra='allow'` checks and before the
opaque fallback:

1. Look up `adapters.get(cls)`. Without an adapter the 0.1 behaviour stands: before/wrap/plain wrappers
   make the class opaque, and a root that is opaque raises `TypeError`. The message gains: "register a
   projection adapter for `<Cls>` (`projection_adapters=...`) to project through the validator".
2. With an adapter, find the `model-fields` node through the wrappers, ignoring their kind (the adapter
   vouches for what the validator reads). A class with no fields node (a `RootModel`) is still opaque:
   an adapter cannot help there.
3. Build the derived spec for the retained fields exactly as for any model (nested classes are derived
   recursively and may have their own adapters), then call the adapter with
   `AdapterContext(cls, retained_names, derived_spec)`.
4. The result must be a `Mapping` with `str` keys, otherwise `TypeError` naming the class and the type
   returned. It is compiled once with `Projection(result)` to validate the grammar; a compile error is
   re-raised as `TypeError` prefixed with the class name.
5. The result replaces the derived spec for this object. Keys the adapter added are kept as it said; keys it
   removed are dropped; the excluded fields remain absent unless an input path or control re-adds their key.
6. Completeness: the class marks the derivation incomplete when it has excluded fields of its own or when
   `_reaches_excluded` finds an excluded class below it. In either case a migration could recreate an
   excluded key from data the adapter kept, so the guards must stay on. An adapted class unrelated to any
   exclusion leaves completeness untouched.

`function-plain` stays opaque even with an adapter: it replaces validation entirely, so there are no
fields to retain.

An input path or control that names an excluded field's key re-adds that key to the JSON. The field is
still excluded from the result by the schema edit (popped if required, defaulted through the alias
redirect otherwise); the config guards handle `extra='forbid'`/`'allow'` and by-name classes because the
derivation is incomplete in that situation.

`Projected` stores `projection_adapters` and shows the adapted class names in its `repr`.

## Example: inspect_ai

Shipped as documentation and as `examples/inspect_ai.py` (not part of the package):

```python
from json_projection.pydantic import migration_adapter

sample_adapter = migration_adapter(
    inputs={
        "scores": ["score"],                       # legacy single score
        "events": [("transcript", "events")],      # legacy transcript holds events and attachments
        "attachments": [("transcript", "content")],
    },
    controls=["score"],                            # the validator rejects score next to scores
    requires={"timelines": ["events"]},            # timelines reference events by id
)

log_adapter = migration_adapter(
    inputs={
        "reductions": [("results", "sample_reductions")],
        "results": ["reductions"],                 # the reverse fill
    },
    controls=["version"],
)

spec_adapter = migration_adapter(
    inputs={"task_args_passed": ["task_args"], "solver_args_passed": ["solver_args"]},
)

INSPECT_ADAPTERS = {EvalSample: sample_adapter, EvalLog: log_adapter, EvalSpec: spec_adapter}

thin = Projected(
    EvalLog,
    exclude={EvalSample: {"events", "messages", "store", "attachments"}},
    projection_adapters=INSPECT_ADAPTERS,
)
```

With this exclusion set `transcript` is not kept, because neither `events` nor `attachments` is retained,
so old logs get the memory saving too. The legacy `sandbox` array needs no adapter: it sits under a
declared key, and a kept value whose shape does not match its sub-spec is copied raw.

## Testing

All in `tests/test_pydantic.py` unless noted.

- **Property test (the correctness statement).** A synthetic model family in `tests/migration_models.py`:
  a root with a `mode='before'` validator that renames a legacy key and dispatches on a `version`
  control key; a nested class with a before validator that unnests a legacy `transcript` into `events`
  and `attachments`, migrates `score` to `scores`, and rejects `score` next to `scores`; a `mode='wrap'`
  validator that resolves `timelines` references to `events` by id; a nested class with no validators.
  A generator emits documents in legacy and current format at random, including `transcript: {}`.
  For three seeds, a few hundred documents and random exclusion sets drawn from a fixed candidate list:
  when `Projected(...)` construction raises `ValueError`, the exclusion set must violate a declared
  dependency; otherwise, for every document ordinary validation accepts, `retained_dump(plain) ==
  retained_dump(projected)`, where `retained_dump` dumps an instance without the excluded fields,
  recursing into nested models and lists of models. Documents ordinary validation rejects are skipped.
- Adapter on a root class with a before validator works where 0.1 raised `TypeError`; the same class
  without an adapter still raises, with the message naming `projection_adapters`.
- Adapter on a nested class inside a validator-free root; adapter on a class with no validators is applied.
- `migration_adapter`: inputs merged only for retained fields; controls merged regardless; path merging
  onto `True`, onto a nested mapping, onto an `__all__` spec (widens), and a leaf onto a mapping (widens);
  unknown field names raise `ValueError`; `requires` conflict raises `ValueError` naming class, field and
  dependency.
- Presence: a kept legacy object emptied by the projection is still present as `{}` and the migration's
  overwrite semantics hold.
- Malformed adapter output: a non-mapping raises `TypeError` naming the class; a grammar error (for
  example `__all__` next to another key) raises `TypeError` naming the class.
- Completeness: an adapted class with excluded fields marks the derivation incomplete
  (`Projected.complete is False`, guards refuse a forbid variant, `by_name=True` refused at runtime);
  an adapted class unrelated to any exclusion leaves `complete` True.
- A `function-plain` class with an adapter is still opaque.
- `repr(Projected(...))` lists adapted classes.
- **Optional inspect_ai round-trip** in `tests/test_inspect_ai.py`, skipped unless `inspect_ai` imports:
  build a minimal current-format `EvalLog` with the real classes, dump it, hand-craft the legacy variant
  (events and attachments moved under `transcript`, `scores` collapsed to `score`), and check both load
  through `Projected(EvalLog, exclude={EvalSample: {"events", "messages", "store", "attachments"}},
  projection_adapters=INSPECT_ADAPTERS)` with `retained_dump` equal to plain validation. inspect_ai is
  not added to the dev dependency group; the test is a local check.
- Typing: `tests/test_typing.py` keeps passing (mypy `--strict`, basedpyright) with the new exports.

## Documentation

- README: a "Migration validators" section with the inspect_ai example, the adapter contract in a few
  lines, and the `migration_adapter` relationships table (retained field -> inputs; controls;
  requires). The 0.1 bullet "Before/wrap/plain validators are not projected through" gains "unless a
  projection adapter is registered for the class". The "Standalone `projected_validator`" bullet notes
  that adapters do not affect it.
- CHANGELOG: 0.2.0 entry (adapters, `migration_adapter`, `AdapterContext`, message change).
- `Cargo.toml` version 0.2.0.

## Out of scope (recorded for later)

- Reading a dependency without retaining it (parse `events` for `timelines`, then drop them).
- Exclusion filtering after before-validators run and before field validation, which would let adapted
  classes drop the guards; and a contract for wrap validators that assign excluded attributes after their
  handler returns.
- Adapter-declared completeness.
- A "keep undeclared keys" spec mode in Rust, which a generic adapter could use for models whose
  migrations are unknown.
