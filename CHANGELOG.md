# Changelog

## 0.2.0 (unreleased)

- Added streaming JSON projection from binary readers or byte chunks without buffering the complete input.
- Projection adapters: `Projected(..., projection_adapters=...)` and `projection_spec(..., projection_adapters=...)`
  project through models with `model_validator(mode='before')`/`'wrap'` once an adapter says which JSON
  inputs the migration reads. New: `AdapterContext`, `ProjectionAdapter`, `migration_adapter` (alternate
  input paths, control keys, dependencies), `KeyPath`.
- The `TypeError` for a root behind such a validator now says how to proceed.
- `examples/inspect_adapters.py`: adapters for inspect_ai's `EvalLog`.

## 0.1.0 (unreleased)

- `project()` and `Projection`: keep-spec driven byte projection over jiter's cursor.
- `json_projection.pydantic`: `Projected`, `projected_validator`, `projection_spec` for pydantic >= 2.13.
