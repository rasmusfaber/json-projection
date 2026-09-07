"""Parse-time field exclusion for pydantic models, including third-party ones.

Requires the ``pydantic`` extra: ``pip install 'json-projection[pydantic]'``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any, Union, cast

try:
    import pydantic
    from pydantic import BaseModel
    from pydantic_core import SchemaValidator
except ImportError as e:  # pragma: no cover
    raise ImportError(
        "json_projection.pydantic requires pydantic>=2.13: pip install 'json-projection[pydantic]'"
    ) from e

_m = re.match(r"(\d+)\.(\d+)", pydantic.VERSION)
if _m is None or (int(_m.group(1)), int(_m.group(2))) < (2, 13):
    raise ImportError(f"json_projection.pydantic requires pydantic>=2.13, found {pydantic.VERSION}")

Exclude = Union[Mapping[type, Iterable[str]], Iterable[str]]  # noqa: UP007  (runtime alias must work on 3.9)
"""Fields to exclude: a mapping from model class to field names, or a set of names for the root model."""

_EXCLUDED_ALIAS_PREFIX = "\x00excluded:"


def normalize_exclude(model: type[BaseModel], exclude: Exclude) -> dict[type, frozenset[str]]:
    if isinstance(exclude, str):  # an iterable of characters, never what the caller meant
        raise TypeError("exclude must be a set of field names or a mapping {model: field names}, not a str")
    if isinstance(exclude, Mapping):
        return {cls: frozenset(names) for cls, names in cast("Mapping[type, Iterable[str]]", exclude).items()}
    return {model: frozenset(exclude)}


def _copy(node: Any) -> Any:
    """Structural copy: dicts and lists are copied, leaves (classes, functions) are shared."""
    if isinstance(node, dict):
        return {k: _copy(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_copy(v) for v in node]
    return node


def _walk(node: Any, fn: Any) -> None:
    if isinstance(node, dict):
        fn(node)
        for v in node.values():
            _walk(v, fn)
    elif isinstance(node, list):
        for v in node:
            _walk(v, fn)


def _extra(cls: type) -> Any:
    """The effective `extra` setting of `cls`; the only place either the guards or `_derive_spec` read it."""
    return getattr(cls, "model_config", {}).get("extra")


def _fields_node(model_node: dict[str, Any]) -> dict[str, Any] | None:
    """`model` -> ... -> `model-fields`, looking through function-validator wrappers."""
    inner = model_node.get("schema")
    while isinstance(inner, dict) and inner.get("type") != "model-fields":
        inner = inner.get("schema")
    return inner if isinstance(inner, dict) else None


def projected_validator(
    model: type[BaseModel], exclude: Exclude, *, assume_projected: bool = False
) -> SchemaValidator:
    """A validator for `model` that never reads the excluded fields.

    Excluded fields with a default keep their default: their key becomes the alias
    ``\\x00excluded:<name>``, which no ordinary document contains, so a document that does contain that
    literal key still populates the field (`Projected` strips it). Excluded required fields are absent
    from the resulting instances (and from ``model_fields_set``). Instances are of the original classes.

    With ``assume_projected=False`` (validating raw input) classes whose configuration would still let
    pydantic read an excluded key are refused with ``ValueError``: ``extra='forbid'`` or ``extra='allow'``,
    and ``populate_by_name``/``validate_by_name`` when a field with a default is excluded. `Projected`
    passes ``assume_projected=True`` when its derived projection covers every occurrence of every class
    (no cut-off), because the byte projection then removes those keys before validation.
    """
    excluded = normalize_exclude(model, exclude)
    schema = _copy(model.__pydantic_core_schema__)
    config: list[Any] = []
    hit: set[tuple[type, str]] = set()

    def visit(node: dict[str, Any]) -> None:
        if node.get("type") != "model":
            return
        cls: type = node["cls"]  # a 'model' node always carries its class
        if cls is model and not config:
            config.append(node.get("config"))
        names = excluded.get(cls)
        if not names:
            return
        fields = _fields_node(node)
        if fields is None:
            raise TypeError(
                f"{cls.__qualname__}: no 'model-fields' under its 'model' node; core schema shape changed"
            )
        for name in names:
            field = fields["fields"].get(name)
            if field is None:
                continue
            hit.add((cls, name))
            has_default = field["schema"].get("type") == "default"
            if not assume_projected:
                extra = _extra(cls)
                if extra == "forbid":
                    raise ValueError(
                        f"{cls.__qualname__} uses extra='forbid': the excluded key {name!r} would be "
                        "rejected as an extra"
                    )
                if extra == "allow":
                    raise ValueError(
                        f"{cls.__qualname__} uses extra='allow': the excluded key {name!r} would be "
                        "captured as an extra"
                    )
                cfg = getattr(cls, "model_config", {})
                if has_default and (cfg.get("populate_by_name") or cfg.get("validate_by_name")):
                    raise ValueError(
                        f"{cls.__qualname__} populates fields by name: the excluded field {name!r} would "
                        "still be read under its name"
                    )
            if has_default:
                # keep the field but look for it under an alias no ordinary document contains, so that
                # pydantic applies the default
                field["validation_alias"] = _EXCLUDED_ALIAS_PREFIX + name
            else:
                fields["fields"].pop(name)

    _walk(schema, visit)
    missing = {(cls, n) for cls, names in excluded.items() for n in names} - hit
    if missing:
        desc = ", ".join(f"{cls.__qualname__}.{n}" for cls, n in sorted(missing, key=str))
        raise ValueError(f"fields not found in the schema of {model.__qualname__}: {desc}")

    validator = SchemaValidator(schema, config[0] if config else None, _use_prebuilt=False)
    if "PrebuiltValidator" in repr(validator):
        raise RuntimeError("pydantic-core reused a prebuilt validator; the exclusion would be ignored")
    return validator


from json_projection import Projection  # noqa: E402  (after the import guard on purpose)


def _field_keys(name: str, alias: Any) -> list[tuple[str, bool]]:
    """JSON keys under which pydantic may look for this field: aliases first, then the name.

    Each key comes with `direct`: whether the value under it is the field's own value. A multi-segment
    alias path is not direct — its first segment holds an enclosing object the field sits somewhere
    inside, so the field's sub-spec does not describe it.
    """
    keys: list[tuple[str, bool]] = []
    if isinstance(alias, str):
        keys.append((alias, True))
    elif isinstance(alias, list):
        if alias and all(isinstance(seg, (str, int)) for seg in alias):  # AliasPath
            if isinstance(alias[0], str):
                keys.append((alias[0], len(alias) == 1))
        else:  # AliasChoices: list of paths
            for choice in alias:
                if isinstance(choice, str):
                    keys.append((choice, True))
                elif isinstance(choice, list) and choice and isinstance(choice[0], str):
                    keys.append((choice[0], len(choice) == 1))
    keys.append((name, True))
    return list(dict.fromkeys(keys))


def _derive_spec(model: type[BaseModel], excluded: dict[type, frozenset[str]]) -> tuple[dict[str, Any], bool]:
    """The keep-spec for `model` minus `excluded`, and whether it covers every occurrence of every class.

    The spec is cut off at a class that appears inside itself (a spec is a finite tree) and at an
    `extra='allow'` class with no excluded fields of its own (its undeclared keys are data). Below such a
    cut-off the projection keeps whole values and cannot remove excluded keys; `complete` is False then.
    """
    schema: dict[str, Any] = cast("dict[str, Any]", model.__pydantic_core_schema__)
    definitions: dict[str, dict[str, Any]] = {}

    def _record_ref(n: dict[str, Any]) -> None:
        if "ref" in n and n.get("type") != "definition-ref":
            definitions[n["ref"]] = n

    _walk(schema, _record_ref)
    complete = [True]

    def spec_for(node: dict[str, Any], seen: frozenset[type]) -> Any:
        t = node.get("type")
        if t == "definitions":  # root of every model with recursive references
            return spec_for(node["schema"], seen)
        if t == "definition-ref":
            target = definitions.get(node["schema_ref"])
            return True if target is None else spec_for(target, seen)
        if t in (
            "nullable",
            "default",
            "function-before",
            "function-after",
            "function-wrap",
            "function-plain",
        ):
            return spec_for(node["schema"], seen) if isinstance(node.get("schema"), dict) else True
        if t in ("list", "set", "frozenset"):
            if not isinstance(node.get("items_schema"), dict):
                return True
            return {"__all__": spec_for(node["items_schema"], seen)}
        if (
            t == "tuple"
            and node.get("variadic_item_index") is not None
            and len(node.get("items_schema", [])) == 1
        ):
            return {"__all__": spec_for(node["items_schema"][0], seen)}  # tuple[X, ...]
        if t == "model":
            cls = node["cls"]
            if cls in seen:
                complete[0] = False
                return True
            if _extra(cls) == "allow" and not excluded.get(cls):
                # undeclared keys are data on this class, so keep the whole object. An excluded class
                # may sit below it, whose keys the projection then cannot remove: a cut-off like recursion.
                complete[0] = False
                return True
            fields = _fields_node(node)
            if fields is None:
                return True
            out: dict[str, Any] = {}
            for name, field in fields["fields"].items():
                if name in excluded.get(cls, ()):
                    continue
                sub = spec_for(field["schema"], seen | {cls})
                for key, direct in _field_keys(name, field.get("validation_alias")):
                    value = sub if direct else True
                    # two fields under one JSON key describe it differently: keep it whole
                    out[key] = value if out.get(key, value) == value else True
            return out
        return True  # dict, Any, unions, scalars: keep the value as-is

    root = spec_for(schema, frozenset())
    if not isinstance(root, dict):
        if _extra(model) == "allow" and not excluded.get(model):
            raise TypeError(
                f"cannot derive a projection for {model.__qualname__}: it allows extra fields and none "
                "of its own fields are excluded; exclude a field on it or use projected_validator alone"
            )
        raise TypeError(f"{model.__qualname__}: could not derive an object spec from its core schema")
    return root, complete[0]


def projection_spec(model: type[BaseModel], exclude: Exclude) -> dict[str, Any]:
    """Derive the keep-spec for `model` minus the excluded fields from its core schema."""
    return _derive_spec(model, normalize_exclude(model, exclude))[0]


class Projected:
    """`validate_json` for `model` that never parses the excluded fields or unknown keys.

    Combines a byte projection (unwanted members are skipped by the Rust cursor) with a validator built
    from the model's core schema minus the excluded fields. Results are instances of the original classes.

    The projection stops at a model that appears inside itself and at an `extra='allow'` model with no
    excluded fields of its own (whose undeclared keys are data), so excluded keys below such a cut-off
    still reach the validator; every excluded class must then use `extra='ignore'`, and an excluded field
    with a default must not be populated by name. `projected_validator` raises `ValueError` otherwise.
    """

    def __init__(self, model: type[BaseModel], exclude: Exclude) -> None:
        self.model = model
        self.exclude = normalize_exclude(model, exclude)
        spec, complete = _derive_spec(model, self.exclude)
        # the guards in projected_validator are only unnecessary when the projection removes every occurrence
        self.validator = projected_validator(model, self.exclude, assume_projected=complete)
        self.spec = Projection(spec)

    def validate_json(
        self,
        data: bytes | bytearray | memoryview | str,
        *,
        strict: bool | None = None,
        context: Any = None,
        by_alias: bool | None = None,
        by_name: bool | None = None,
    ) -> Any:
        return self.validator.validate_json(
            self.spec(data), strict=strict, context=context, by_alias=by_alias, by_name=by_name
        )

    def __repr__(self) -> str:
        ex = ", ".join(f"{c.__qualname__}: {sorted(n)}" for c, n in self.exclude.items())
        return f"Projected({self.model.__qualname__}, exclude={{{ex}}})"
