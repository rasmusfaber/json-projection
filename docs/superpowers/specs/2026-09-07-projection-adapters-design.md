# Projection adapters for models with migration validators: design

Date: 2026-09-07
Status: approved; implemented in 0.2.0
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
    model: type[BaseModel]  # the class this JSON object validates into
    fields: frozenset[str]  # declared fields retained after exclusion, by field name
    spec: dict[str, Any]  # the derived spec for those fields; keys are JSON keys (aliases applied)
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
KeyPath = str | tuple[str, ...]      # one JSON key, or a nested key path

def migration_adapter(
    *,
    inputs: Mapping[str, Iterable[KeyPath]] | None = None,
    controls: Iterable[KeyPath] = (),
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

A path segment named `__all__` truncates the path there, keeping that container whole, so the merged
result does not depend on the order the paths are given in; a path that starts with `__all__` asks for
the root object and is a `TypeError` at construction. A bare `str` where a collection of paths or names
is expected (`inputs` values, `controls`, `requires` values) is a `TypeError` too, the same policy
`normalize_exclude` applies to `exclude`: it would iterate as characters.

Field names given to `inputs` or `requires` (keys and dependency values) that are not declared on
`ctx.model` raise `ValueError` when the adapter runs, so typos surface at construction. Adapters are
looked up by exact class; one registered for a class with no `model` node in the root's schema is a
`ValueError`, so a subclass needs its own entry rather than silently inheriting the base class's.

## Derivation semantics

In `_derive_spec`, in the `model` branch, after the recursion and `extra='allow'` checks and before the
opaque fallback:

1. Look up `adapters.get(cls)`. Without an adapter the 0.1 behaviour stands: before/wrap/plain wrappers
   make the class opaque, and a root that is opaque raises `TypeError`. The message gains: "register a
   projection adapter for `<Cls>` (`projection_adapters=...`) to say what it reads".
2. pydantic applies a `mode='wrap'` model validator *outside* the model node it wraps
   (`function-wrap -> model -> function-before -> model-fields`, for a class with both a wrap and a
   before validator), not inside it like `mode='before'`. So the derivation also looks through outer
   `function-wrap`/`function-before` wrappers to a `model` node beneath them when that class has an
   adapter and every wrapper passed on the way is one of the class's *own registered model validators*
   -- its function, unwrapped through `__func__`, is the `func` of an entry in
   `cls.__pydantic_decorators__.model_validators`, which covers `@classmethod`, `@staticmethod` and
   inherited validators alike: the adapter vouches for those too. Ownership is registration, not
   binding. A validator that belongs to something else -- a field validator on the enclosing model, an
   `Annotated` validator, even one that is a bound method of the adapted class -- is registered on
   nothing, and stays opaque. At the root such a wrapper raises `TypeError` naming the class and saying
   the adapter cannot vouch for the wrapper, instead of asking for an adapter that is already there.
3. With an adapter, find the `model-fields` node through the wrappers, ignoring their kind (the adapter
   vouches for what the validator reads). A class with no fields node (a `RootModel`) is still opaque:
   an adapter cannot help there.
4. Build the derived spec for the retained fields exactly as for any model (nested classes are derived
   recursively and may have their own adapters), then call the adapter with
   `AdapterContext(cls, retained_names, derived_spec)`.
5. The result must be a `Mapping` with `str` keys, otherwise `TypeError` naming the class and the type
   returned. It is compiled once with `Projection(result)` to validate the grammar; a compile error is
   re-raised as `TypeError` prefixed with the class name.
6. The result replaces the derived spec for this object. Keys the adapter added are kept as it said; keys it
   removed are dropped; the excluded fields remain absent unless an input path or control re-adds their key.
7. Completeness: the class marks the derivation incomplete when it has excluded fields of its own or when
   `_reaches_excluded` finds an excluded class below it. In either case a migration could recreate an
   excluded key from data the adapter kept, so the guards must stay on. An adapted class unrelated to any
   exclusion leaves completeness untouched.

`function-plain` stays opaque even with an adapter: it replaces validation entirely, so there are no
fields to retain.

Every kept-whole subtree is still searched for adapted classes (following each `definition-ref` once,
and skipping classes the derivation is already inside), and each one found is derived once and the
result discarded. Keeping the bytes whole answers "what survives projection", not "can this migration
live with these exclusions": an adapter's `requires` conflict, unknown field name or hand-written
refusal must be reported wherever its class occurs, not only where the projection reaches. The discarded
derivation cannot add keys to the spec; it can only make `complete` more conservative.

An input path or control that names an excluded field's key re-adds that key to the JSON. The field is
still excluded from the result by the schema edit (popped if required, defaulted through the alias
redirect otherwise); the config guards handle `extra='forbid'`/`'allow'` and by-name classes because the
derivation is incomplete in that situation.

`Projected` stores `projection_adapters` and shows the adapted class names in its `repr`.
`Projected.complete` is a read-only property: a conservative proof that the projection removes every
excluded key everywhere, so no migration can recreate one either. It is what permits dropping the 0.1
config guards, which is why it cannot be assigned after construction.

## Example: inspect_ai

Shipped as documentation and as `examples/inspect_adapters.py` (not part of the package):

```python
from json_projection.pydantic import migration_adapter

sample_adapter = migration_adapter(
    inputs={
        "scores": ["score"],  # legacy single score
        "events": [("transcript", "events")],  # legacy transcript holds events and attachments
        "attachments": [("transcript", "content")],
    },
    requires={"timelines": ["events"]},  # timelines reference events by id
)

log_adapter = migration_adapter(inputs={"reductions": [("results", "sample_reductions")]})

spec_adapter = migration_adapter(
    inputs={"task_args_passed": ["task_args"], "solver_args_passed": ["solver_args"]},
)

INSPECT_ADAPTERS = {EvalSample: sample_adapter, EvalLog: log_adapter, EvalSpec: spec_adapter}

BULK_FIELDS = frozenset({"events", "messages", "store", "attachments", "timelines"})

thin = Projected(EvalLog, exclude={EvalSample: BULK_FIELDS}, projection_adapters=INSPECT_ADAPTERS)
```

`BULK_FIELDS` includes `timelines` alongside `events`: `sample_adapter` declares that retaining `timelines`
requires `events`, so `requires={"timelines": ["events"]}` refuses excluding `events` while keeping
`timelines`.

With this exclusion set `transcript` is not kept, because neither `events` nor `attachments` is retained,
so old logs get the memory saving too. The legacy `sandbox` array needs no adapter: it sits under a
declared key, and a kept value whose shape does not match its sub-spec is copied raw.

## Testing

Adapter tests live in `tests/test_adapters.py`, the correctness property in `tests/test_migrations.py`
(models, generator and `retained_dump` in `tests/migration_models.py`), and the optional inspect_ai
round-trip in `tests/test_inspect_ai.py`; `tests/test_pydantic.py` is unchanged.

- **Property test (the correctness statement)**, in `tests/test_migrations.py`. A synthetic model family
  in `tests/migration_models.py`:
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
- **Optional inspect_ai round-trip** in `tests/test_inspect_ai.py`, skipped unless `inspect_ai` imports and
  its deserializing-context helper is importable: round-trip `tests/fixtures/inspect_legacy_log.json` (and
  the same log re-dumped to current format) through `Projected(EvalLog, exclude={EvalSample: BULK_FIELDS},
  projection_adapters=INSPECT_ADAPTERS)`, checking `retained_dump` equal to plain validation. Both parses
  pass inspect_ai's deserializing context -- `get_deserializing_context()` from the private module
  `inspect_ai._util.constants`, which the test module skips itself if that import fails -- because
  inspect_ai regenerates ids (`EvalSpec.eval_id`, message ids) on every parse otherwise, which would make
  even two plain parses of the same bytes disagree. A third case additionally excludes `EvalSpec.task_args`
  to prove `task_args_passed`'s fallback survives losing its primary input. inspect_ai is not added to the
  dev dependency group; the test is a local check.
- Typing: `tests/test_typing.py` keeps passing (mypy `--strict`, basedpyright) with the new exports.

## Documentation

- README: a "Migration validators" section with the adapter contract in a few lines, a `migration_adapter`
  example with `inputs`, `controls` and `requires` commented inline, the dependency-conflict sentence, and
  a pointer to `examples/inspect_adapters.py`. The 0.1 bullet "Before/wrap/plain validators are not
  projected through" gains "unless a projection adapter is registered for the class". The "Standalone
  `projected_validator`" bullet notes that adapters do not affect it.
- CHANGELOG: 0.2.0 entry (adapters, `migration_adapter`, `AdapterContext`, message change).
- `Cargo.toml` version 0.2.0.

## Out of scope (recorded for later)

- Migrations that construct model instances. A before validator that does
  `data["child"] = Child.model_validate(legacy)` hands pydantic a finished instance, which pydantic does
  not re-validate, so the edited validator for `Child` never runs and exclusions on `Child` do not apply
  to that object. Only raw dicts pass through the edited validator, and nothing in the library can
  intercept the construction. Documented in the README's "What you give up".
- Reading a dependency without retaining it (parse `events` for `timelines`, then drop them).
- Exclusion filtering after before-validators run and before field validation, which would let adapted
  classes drop the guards; and a contract for wrap validators that assign excluded attributes after their
  handler returns.
- Adapters on `extra='allow'` classes. A model with `extra='allow'` and no excluded fields of its own is
  kept whole, and an adapter registered for it is not consulted, so its `requires` are not checked.
- Adapter-declared completeness.
- A "keep undeclared keys" spec mode in Rust, which a generic adapter could use for models whose
  migrations are unknown.
