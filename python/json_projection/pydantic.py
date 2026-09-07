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
    raise ImportError("json_projection.pydantic requires pydantic>=2.13: pip install 'json-projection[pydantic]'") from e

_m = re.match(r"(\d+)\.(\d+)", pydantic.VERSION)
if _m is None or (int(_m.group(1)), int(_m.group(2))) < (2, 13):
    raise ImportError(f"json_projection.pydantic requires pydantic>=2.13, found {pydantic.VERSION}")

Exclude = Union[Mapping[type, Iterable[str]], Iterable[str]]  # noqa: UP007  (runtime alias must work on 3.9)
"""Fields to exclude: a mapping from model class to field names, or a set of names for the root model."""

_EXCLUDED_ALIAS_PREFIX = "\x00excluded:"


def normalize_exclude(model: type[BaseModel], exclude: Exclude) -> dict[type, frozenset[str]]:
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


def _fields_node(model_node: dict[str, Any]) -> dict[str, Any] | None:
    """`model` -> ... -> `model-fields`, looking through function-validator wrappers."""
    inner = model_node.get("schema")
    while isinstance(inner, dict) and inner.get("type") != "model-fields":
        inner = inner.get("schema")
    return inner if isinstance(inner, dict) else None


def projected_validator(model: type[BaseModel], exclude: Exclude) -> SchemaValidator:
    """A validator for `model` that never reads the excluded fields.

    Excluded fields with a default keep their default. Excluded required fields are absent from the
    resulting instances (and from ``model_fields_set``). Instances are of the original classes.
    """
    excluded = normalize_exclude(model, exclude)
    for cls in excluded:
        if getattr(cls, "model_config", {}).get("extra") == "forbid":
            raise ValueError(f"{cls.__qualname__} uses extra='forbid'; an excluded key would be rejected as extra")
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
            raise TypeError(f"{cls.__qualname__}: no 'model-fields' under its 'model' node; core schema shape changed")
        for name in names:
            field = fields["fields"].get(name)
            if field is None:
                continue
            hit.add((cls, name))
            if field["schema"].get("type") == "default":
                # keep the field but make its JSON key unreachable so pydantic applies the default
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
