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


_DATA_KEYS = frozenset({"default", "metadata", "serialization", "json_schema_extra"})
"""Keys of a schema NODE whose values are user data or output-side settings, not schemas.

A field default is arbitrary Python: it may be a dict that happens to look like a schema node, and it
may even contain itself. Neither traversal has any business inside one. These are node keys only: the
same strings are perfectly ordinary field names, TypedDict keys and union tags, and the maps that hold
those (`model-fields.fields`, a tagged union's `choices`) must be descended into whole.
"""


def _is_node(d: dict[Any, Any]) -> bool:
    """Whether `d` is a schema node rather than a name -> schema map.

    Every core-schema node carries its kind under a string `type`. A map keyed by field name, TypedDict
    key or union tag holds schemas (or tag aliases) as its values, so it has no such key -- and a field
    genuinely named `type` maps to a schema, not to a string.
    """
    return isinstance(d.get("type"), str)


def _copy(node: Any) -> Any:
    """Structural copy along schema edges; leaves (classes, functions) and `_DATA_KEYS` are shared."""
    if isinstance(node, dict):
        if _is_node(node):
            return {k: v if k in _DATA_KEYS else _copy(v) for k, v in node.items()}
        return {k: _copy(v) for k, v in node.items()}  # a name -> schema map: every value is a schema
    if isinstance(node, list):
        return [_copy(v) for v in node]
    if isinstance(node, tuple):  # a tagged union's (schema, tag) choices
        return tuple(_copy(v) for v in node)
    return node


def _walk(node: Any, fn: Any) -> None:
    """Call `fn` on every schema node reachable along schema edges."""
    if isinstance(node, dict):
        if _is_node(node):
            fn(node)
            for k, v in node.items():
                if k not in _DATA_KEYS:
                    _walk(v, fn)
        else:
            for v in node.values():  # a name -> schema map: every value is a schema
                _walk(v, fn)
    elif isinstance(node, (list, tuple)):
        for v in node:
            _walk(v, fn)


def _definitions(schema: Any) -> dict[str, dict[str, Any]]:
    """Every node that defines a `ref`, keyed by it, so that a `definition-ref` can be resolved."""
    found: dict[str, dict[str, Any]] = {}

    def record(node: dict[str, Any]) -> None:
        # `_walk` only offers nodes, so a `ref` here is a reference and not a field of that name
        if isinstance(node.get("ref"), str) and node["type"] != "definition-ref":
            found[node["ref"]] = node

    _walk(schema, record)
    return found


def _extra(cls: type) -> Any:
    """The effective `extra` setting of `cls`; the only place either the guards or `_derive_spec` read it."""
    return getattr(cls, "model_config", {}).get("extra")


_OPAQUE_WRAPPERS = ("function-before", "function-wrap", "function-plain")
"""Validators whose input shape is unknown: the schema they wrap does not describe what they are given."""

_WRAPPERS = ("default", "nullable", "definitions", "definition-ref", "function-after", *_OPAQUE_WRAPPERS)


def _fields_node(
    model_node: dict[str, Any], definitions: dict[str, dict[str, Any]]
) -> tuple[dict[str, Any] | None, str | None]:
    """`model` -> `model-fields`, through wrapper nodes only, stopping at any nested `model`.

    Returns the `model-fields` node -- None when the class declares no fields of its own, as for a
    `RootModel`, whose `model` node wraps the type it is a root for -- and the kind of the first
    before/wrap/plain wrapper passed on the way, which makes the fields useless for deriving a
    projection (the validator's input is not the shape they describe).
    """
    node: Any = model_node.get("schema")
    opaque_kind: str | None = None
    while isinstance(node, dict):
        t = node.get("type")
        if t == "model-fields":
            return node, opaque_kind
        if t == "definition-ref":
            node = definitions.get(node.get("schema_ref", ""))
            continue
        if t not in _WRAPPERS:
            return None, opaque_kind  # a nested model, or any other schema: not this class's fields
        if t in _OPAQUE_WRAPPERS and opaque_kind is None:
            opaque_kind = t
        node = node.get("schema")
    return None, opaque_kind


def _reaches_excluded(
    node: dict[str, Any], definitions: dict[str, dict[str, Any]], excluded: dict[type, frozenset[str]]
) -> bool:
    """Whether an excluded class has a `model` node at or below `node`, following each ref once."""
    hit = [False]
    refs: set[str] = set()
    pending = [node]

    def look(n: dict[str, Any]) -> None:
        if n.get("type") == "model" and excluded.get(n["cls"]):
            hit[0] = True
        ref = n.get("schema_ref")
        if isinstance(ref, str) and ref not in refs:
            refs.add(ref)
            if ref in definitions:
                pending.append(definitions[ref])

    while pending and not hit[0]:
        _walk(pending.pop(), look)
    return hit[0]


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
    passes ``assume_projected=True`` only when its derived projection provably covers every occurrence
    of every excluded class, because the byte projection then removes those keys before validation.

    A class with its own ``__init__`` is refused with ``ValueError`` whenever exclusion would touch it
    or anything below it, whatever ``assume_projected`` says: pydantic-core calls that ``__init__``,
    which validates through the class's original validator and ignores the edit.
    """
    excluded = normalize_exclude(model, exclude)
    schema = _copy(model.__pydantic_core_schema__)
    definitions = _definitions(schema)
    config: list[Any] = []

    # the names are validated against the unedited schema: removing a required field would hide the
    # class it holds from the rest of the walk, and its own valid exclusions would look like typos
    present: set[tuple[type, str]] = set()

    def find(node: dict[str, Any]) -> None:
        names = excluded.get(node["cls"]) if node.get("type") == "model" else None
        if names:
            fields, _ = _fields_node(node, definitions)
            if fields is not None:
                present.update((node["cls"], n) for n in names if n in fields["fields"])

    _walk(schema, find)
    missing = {(cls, n) for cls, names in excluded.items() for n in names} - present
    if missing:
        desc = ", ".join(f"{cls.__qualname__}.{n}" for cls, n in sorted(missing, key=str))
        raise ValueError(f"fields not found in the schema of {model.__qualname__}: {desc}")

    def visit(node: dict[str, Any]) -> None:
        if node.get("type") != "model":
            return
        cls: type = node["cls"]  # a 'model' node always carries its class
        if cls is model and not config:
            config.append(node.get("config"))
        if node.get("custom_init") and _reaches_excluded(node, definitions, excluded):
            # pydantic-core calls the class's __init__, which validates through the class's own
            # __pydantic_validator__: the edit below would simply not be used
            raise ValueError(
                f"{cls.__qualname__} defines its own __init__, which re-enters the original validator; "
                "exclusion cannot be applied"
            )
        names = excluded.get(cls)
        if not names:
            return
        fields, _ = _fields_node(node, definitions)
        if fields is None:
            return  # no fields of its own: a RootModel wraps another schema
        for name in names:
            field = fields["fields"].get(name)
            if field is None:
                continue
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

    Every shape the derivation cannot describe is kept whole instead: a class that appears inside itself
    (a spec is a finite tree), an `extra='allow'` class with no excluded fields of its own (its
    undeclared keys are data), a before/wrap/plain validator (its input is not the shape its inner
    schema describes), and everything unknown (dicts, unions, fixed tuples, dataclasses, TypedDicts,
    `Any`, enclosing objects named by a multi-segment alias path, colliding keys). Nothing below a kept
    subtree is projected, so `complete` is False as soon as an excluded class is reachable in one.
    """
    schema: dict[str, Any] = cast("dict[str, Any]", model.__pydantic_core_schema__)
    definitions = _definitions(schema)
    complete = [True]
    wrapper_kind: list[str] = []

    def opaque(node: dict[str, Any]) -> bool:
        """Keep this subtree whole, and say so: `True`.

        Every fallback goes through here. Below a kept subtree nothing is projected, so an excluded
        class reachable in it keeps its keys and the derivation is no longer complete.
        """
        if _reaches_excluded(node, definitions, excluded):
            complete[0] = False
        return True

    def spec_for(node: dict[str, Any], seen: frozenset[type]) -> Any:
        t = node.get("type")
        if t == "definitions":  # root of every model with recursive references
            return spec_for(node["schema"], seen)
        if t == "definition-ref":
            target = definitions.get(node["schema_ref"])
            return opaque(node) if target is None else spec_for(target, seen)
        if t in _OPAQUE_WRAPPERS:
            wrapper_kind.append(t)
            return opaque(node)
        if t in ("nullable", "default", "function-after"):
            # function-after runs on what the inner schema already validated, so that shape is known
            return spec_for(node["schema"], seen) if isinstance(node.get("schema"), dict) else opaque(node)
        if t in ("list", "set", "frozenset"):
            if not isinstance(node.get("items_schema"), dict):
                return opaque(node)
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
                return opaque(node)  # a class inside itself; a spec is a finite tree
            if _extra(cls) == "allow" and not excluded.get(cls):
                # undeclared keys are data on this class, so keep the whole object
                return opaque(node)
            fields, kind = _fields_node(node, definitions)
            if kind is not None:
                wrapper_kind.append(kind)
                return opaque(node)
            if fields is None:
                return opaque(node)
            out: dict[str, Any] = {}
            source: dict[str, dict[str, Any]] = {}  # the field schema each kept key came from
            for name, field in fields["fields"].items():
                if name in excluded.get(cls, ()):
                    continue
                fschema = field["schema"]
                sub = spec_for(fschema, seen | {cls})
                for key, direct in _field_keys(name, field.get("validation_alias")):
                    # a multi-segment alias path names an enclosing object, not the field's own value
                    value = sub if direct else opaque(fschema)
                    if out.get(key, value) != value:
                        # two fields under one JSON key describe it differently: keep it whole
                        value = True
                        opaque(fschema)
                        opaque(source[key])
                    out[key] = value
                    source[key] = fschema
            return out
        return opaque(node)  # dict, unions, fixed tuples, dataclasses, TypedDicts, Any, scalars

    root = spec_for(schema, frozenset())
    if not isinstance(root, dict):
        if _extra(model) == "allow" and not excluded.get(model):
            raise TypeError(
                f"cannot derive a projection for {model.__qualname__}: it allows extra fields and none "
                "of its own fields are excluded; exclude a field on it or use projected_validator alone"
            )
        if wrapper_kind:
            raise TypeError(
                f"cannot derive a projection for {model.__qualname__}: a {wrapper_kind[0]} validator "
                "wraps its fields, so the shape of its input is unknown; use projected_validator alone"
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

    The projection keeps whole every subtree it cannot describe (see `_derive_spec`). An excluded class
    inside one still sees its keys, so it must use `extra='ignore'`, and an excluded field with a default
    must not be populated by name; `projected_validator` raises `ValueError` otherwise. `validate_json`
    refuses `by_name=True` and `by_alias=False` for the same reason while `complete` is False.
    """

    def __init__(self, model: type[BaseModel], exclude: Exclude) -> None:
        self.model = model
        self.exclude = normalize_exclude(model, exclude)
        spec, self.complete = _derive_spec(model, self.exclude)
        # the guards in projected_validator are only unnecessary when the projection removes every occurrence
        self.validator = projected_validator(model, self.exclude, assume_projected=self.complete)
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
        if not self.complete and (by_name or by_alias is False):
            # an excluded field with a default keeps its key inside every subtree the projection had to
            # keep whole; validating by name reads it there and the default is never applied
            raise ValueError(
                f"{self.model.__qualname__}: the projection could not remove every excluded key (it "
                "keeps some subtrees whole), so validating by name would read the excluded keys that "
                "are left. Drop by_name/by_alias, or exclude nothing inside those subtrees."
            )
        return self.validator.validate_json(
            self.spec(data), strict=strict, context=context, by_alias=by_alias, by_name=by_name
        )

    def __repr__(self) -> str:
        ex = ", ".join(f"{c.__qualname__}: {sorted(n)}" for c, n in self.exclude.items())
        return f"Projected({self.model.__qualname__}, exclude={{{ex}}})"
