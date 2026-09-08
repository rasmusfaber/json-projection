# Changelog

## Unreleased

- Added `Projection.excluding()` to remove named JSON members while retaining all unspecified members, including during streaming.
- Avoided formatting complete Pydantic validators during construction by using a cached schema-edit compatibility probe.
- Standalone projection planning now rejects unknown exclusion fields and classes; `projection_plan()` reports incomplete coverage and its causes.
- Fixed alias-collision coverage reporting and rejected retained aliases that would populate an excluded field through its hidden alias.

## 0.2.0 (unreleased)

- Added streaming JSON projection from binary readers or byte chunks without buffering the complete input.
- Projection adapters: `Projected(..., projection_adapters=...)` and `projection_spec(..., projection_adapters=...)`
  project through models with `model_validator(mode='before')`/`'wrap'` once an adapter says which JSON
  inputs the migration reads. New: `AdapterContext`, `ProjectionAdapter`, `migration_adapter` (alternate
  input paths, control keys, dependencies), `KeyPath`.
- The `TypeError` for a root behind such a validator now says how to proceed, and distinguishes a
  missing adapter from a wrapper the adapter cannot vouch for: an outer before/wrap validator belongs to
  the class only when it is one of the class's own registered model validators (`@classmethod`,
  `@staticmethod` and inherited ones alike), not merely bound to it.
- Fixed a 0.1 completeness hole: when three or more fields shared one JSON key and a later field widened
  it to "kept whole", only the most recent field behind that key was accounted for, so an excluded key
  could stay in the kept bytes while `complete` reported True and the standalone guards were dropped.
- `Projected.complete` is a read-only property: a conservative proof that the projection removes every
  excluded key everywhere, which is what permits dropping the standalone config guards.
- `projection_adapters` must name only classes that appear in the root model's schema; anything else is a
  `ValueError` naming the offending classes, so a shared registry has to be filtered when rooting at a
  subtree class: `{c: a for c, a in REGISTRY.items() if c in {EvalSample, ...}}`.
- `examples/inspect_adapters.py`: adapters for inspect_ai's `EvalLog`.

## 0.1.0 (2026-09-07)

- `project()` and `Projection`: keep-spec driven byte projection over jiter's cursor.
- `json_projection.pydantic`: `Projected`, `projected_validator`, `projection_spec` for pydantic >= 2.13.
