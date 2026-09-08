"""Parse-time field exclusion for pydantic models, including third-party ones.

Requires the ``pydantic`` extra: ``pip install 'json-projection[pydantic]'``.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from functools import lru_cache
from types import MappingProxyType
from typing import Any, Union, cast

try:
    import pydantic
    from pydantic import BaseModel, Field
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
    """Normalise exclusion collections, rejecting malformed classes and field names."""
    entries = exclude.items() if isinstance(exclude, Mapping) else ((model, exclude),)
    normalized: dict[type, frozenset[str]] = {}
    for cls, names in entries:
        if not isinstance(cls, type) or not issubclass(cls, BaseModel):
            raise TypeError(f"exclude keys must be pydantic model classes, not {cls!r}")
        if isinstance(names, str):
            raise TypeError("exclude must contain collections of field names, not a str")
        try:
            values = list(names)
        except TypeError as exc:
            raise TypeError("exclude must contain iterable collections of field names") from exc
        if any(not isinstance(name, str) for name in values):
            raise TypeError("exclude field names must be strings")
        normalized[cls] = frozenset(values)
    return normalized


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


def _own_model_validators(cls: type) -> set[int]:
    """The identity of every function registered as a model validator of `cls`.

    `@classmethod`, `@staticmethod` and inherited validators all land here: the function the core schema
    carries is the decorator's `func` (bound or not), so comparing both after unwrapping `__func__`
    identifies the class's own validators and nothing else.
    """
    decorators = getattr(cls, "__pydantic_decorators__", None)
    validators = getattr(decorators, "model_validators", {})
    return {id(getattr(d.func, "__func__", d.func)) for d in validators.values()}


def _outer_wrappers(
    node: Any, definitions: dict[str, dict[str, Any]]
) -> tuple[dict[str, Any] | None, list[int]]:
    """The `model` node behind `node`'s outer wrappers, and the identity of each wrapper's function.

    Walks the same wrapper kinds as `_fields_node`; anything else means no model node sits here. A
    class's own before validators are *inside* its model node and never appear in this sequence; its
    wrap validators are outside it and always do.
    """
    functions: list[int] = []
    n: Any = node
    while isinstance(n, dict):
        t = n.get("type")
        if t == "model":
            return n, functions
        if t in _OPAQUE_WRAPPERS:
            fn = n.get("function", {}).get("function")
            functions.append(id(getattr(fn, "__func__", fn)))
        elif t == "definition-ref":
            n = definitions.get(n.get("schema_ref", ""))
            continue
        elif t not in _WRAPPERS:
            return None, functions
        n = n.get("schema")
    return None, functions


def _own_wrapper_chain(cls: type) -> list[int] | None:
    """The outer wrapper sequence pydantic emits for `cls` in `cls`'s own schema, or None if unclear."""
    schema = getattr(cls, "__pydantic_core_schema__", None)
    if schema is None:
        return None
    node, chain = _outer_wrappers(schema, _definitions(schema))
    return chain if node is not None and node["cls"] is cls else None


def _adapted_model_behind(
    node: dict[str, Any],
    definitions: dict[str, dict[str, Any]],
    adapters: Mapping[type, ProjectionAdapter],
    own_chains: dict[type, list[int] | None],
) -> tuple[dict[str, Any] | None, bool]:
    """The `model` node behind `node`'s outer wrappers when its class has an adapter, and whether the
    adapter vouches for those wrappers.

    The adapter speaks for exactly the wrappers pydantic emits for the class's own validators, and for
    nothing else. Two things must hold. Every wrapper must be a registered model validator of the class,
    so a function installed from outside -- through `__get_pydantic_core_schema__`, say -- is refused.
    And the sequence must be *equal* to the one the class's own schema carries, not merely drawn from
    it: an `Annotated[Cls, BeforeValidator(Cls.registered_validator)]` on somebody's field reuses a
    registered function but adds a wrapper, and that second pass reads a shape the adapter never
    described. Anything extra makes the node opaque.

    Returns `(None, False)` when there is no adapted model behind the wrappers, and `(node, False)` when
    there is one but the wrapper chain is not the class's own. `own_chains` caches the reference chain
    per class for the duration of one derivation.
    """
    behind, chain = _outer_wrappers(node, definitions)
    if behind is None or behind["cls"] not in adapters:
        return None, False
    cls = behind["cls"]
    if cls not in own_chains:
        own_chains[cls] = _own_wrapper_chain(cls)
    registered = _own_model_validators(cls)
    return behind, chain == own_chains[cls] and all(fn in registered for fn in chain)


def _subtree_models(node: Any, definitions: dict[str, dict[str, Any]]) -> Iterator[dict[str, Any]]:
    """Every `model` node at or below `node`, following each `definition-ref` once.

    Lazy, so a caller that only asks whether some class is down there stops at the first hit.
    """
    refs: set[str] = set()
    pending = [node]
    found: list[dict[str, Any]] = []

    def look(n: dict[str, Any]) -> None:
        if n.get("type") == "model":
            found.append(n)
        ref = n.get("schema_ref")
        if isinstance(ref, str) and ref not in refs:
            refs.add(ref)
            if ref in definitions:
                pending.append(definitions[ref])

    while pending:
        _walk(pending.pop(), look)
        yield from found
        found.clear()


def _reaches_excluded(
    node: dict[str, Any], definitions: dict[str, dict[str, Any]], excluded: dict[type, frozenset[str]]
) -> bool:
    """Whether an excluded class has a `model` node at or below `node`, following each ref once."""
    return any(excluded.get(n["cls"]) for n in _subtree_models(node, definitions))


def _validate_excluded(
    model: type[BaseModel],
    excluded: dict[type, frozenset[str]],
    schema: dict[str, Any],
    definitions: dict[str, dict[str, Any]],
) -> None:
    """Check names against the unedited schema, including children of excluded fields."""
    present: set[tuple[type, str]] = set()
    classes: set[type] = set()
    for node in _subtree_models(schema, definitions):
        cls = node["cls"]
        classes.add(cls)
        names = excluded.get(cls)
        if names:
            fields, _ = _fields_node(node, definitions)
            if fields is not None:
                present.update((cls, name) for name in names if name in fields["fields"])
    absent = excluded.keys() - classes
    if absent:
        desc = ", ".join(sorted(cls.__qualname__ for cls in absent))
        raise ValueError(f"model classes not found in the schema of {model.__qualname__}: {desc}")
    missing = {(cls, name) for cls, names in excluded.items() for name in names} - present
    if missing:
        desc = ", ".join(sorted(f"{cls.__qualname__}.{name}" for cls, name in missing))
        raise ValueError(f"fields not found in the schema of {model.__qualname__}: {desc}")


@lru_cache(maxsize=1)
def _check_prebuilt_support(validator_type: type[SchemaValidator]) -> None:
    """Verify core-schema edits behaviorally once per validator implementation.

    Formatting a user validator can expand shared schema references into megabytes of text. A small
    repeated nested model instead checks that disabling prebuilt validators applies both required-field
    removal and default-field alias edits, without depending on pydantic-core's debug representation.
    """

    class Probe(BaseModel):
        required: int
        defaulted: int = Field(default=1, validation_alias="legacy")

    class Parent(BaseModel):
        left: Probe
        right: Probe

    schema = _copy(Parent.__pydantic_core_schema__)

    def edit(node: dict[str, Any]) -> None:
        if node.get("type") == "model-fields" and "required" in node["fields"]:
            node["fields"].pop("required")
            node["fields"]["defaulted"]["validation_alias"] = _EXCLUDED_ALIAS_PREFIX + "defaulted"

    _walk(schema, edit)
    message = "pydantic-core reused a prebuilt validator; the exclusion would be ignored"
    try:
        validator = validator_type(schema, _use_prebuilt=False)
        result = validator.validate_python({"left": {"legacy": 9}, "right": {"required": 2, "legacy": 3}})
    except (pydantic.ValidationError, TypeError) as exc:
        raise RuntimeError(message) from exc
    if any(child.defaulted != 1 or "required" in child.__dict__ for child in (result.left, result.right)):
        raise RuntimeError(message)


def projected_validator(
    model: type[BaseModel], exclude: Exclude, *, assume_projected: bool = False
) -> SchemaValidator:
    """A validator for `model` that never reads the excluded fields.

    Excluded fields with a default keep their default: their key becomes the alias
    ``\\x00excluded:<name>``, which no ordinary document contains, so a document that does contain that
    literal key still populates the field. Excluded required fields are absent
    from the resulting instances (and from ``model_fields_set``). Instances are of the original classes.
    A retained field using an excluded defaulted field's synthetic alias is refused with ``ValueError``:
    preserving its input would also populate the excluded field.

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
    _validate_excluded(model, excluded, schema, definitions)

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
        retained_keys: dict[str, str] = {}
        for kept_name, kept_field in fields["fields"].items():
            if kept_name not in names:
                for key, _ in _field_keys(kept_name, kept_field.get("validation_alias")):
                    retained_keys.setdefault(key, kept_name)
        for name in names:
            field = fields["fields"].get(name)
            if field is None:
                continue
            has_default = field["schema"].get("type") == "default"
            if has_default:
                kept_name = retained_keys.get(_EXCLUDED_ALIAS_PREFIX + name)
                if kept_name is not None:
                    raise ValueError(
                        f"{cls.__qualname__}: retained field {kept_name!r} uses the synthetic alias "
                        f"of excluded field {name!r}; preserving its input would populate the "
                        "excluded field"
                    )
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
    _check_prebuilt_support(SchemaValidator)
    return SchemaValidator(schema, config[0] if config else None, _use_prebuilt=False)


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


@dataclass(frozen=True)
class AdapterContext:
    """What a projection adapter is told about one JSON object.

    `fields` are the declared fields retained after exclusion, by field name. `spec` is the projection
    derived for them -- keyed by JSON key, aliases applied -- and is a fresh dict the adapter may modify
    and return.
    """

    model: type[BaseModel]
    fields: frozenset[str]
    spec: dict[str, Any]


ProjectionAdapter = Callable[[AdapterContext], Mapping[str, Any]]
"""Translates the fields retained after validation into the JSON inputs the class's validators need.

Returns the spec for the object in the usual grammar (JSON key -> True | nested mapping | {"__all__": spec}),
or raises ValueError for an exclusion set the migration cannot support. Called once per occurrence of the
class in the schema; must be a pure function of its context.
"""


_MAX_SPEC_DEPTH = 256
"""The compiler's nesting cap (`MAX_DEPTH` in `src/spec.rs`), counted the same way it counts."""


def _is_spec_mapping(value: Any) -> bool:
    """Whether the compiler will read `value` as a mapping.

    It accepts more than `Mapping`: anything with `keys()` that can be indexed is copied through a
    `dict`, so `MappingProxyType`, `UserDict` and any hand-rolled mapping protocol object qualify.
    Materialisation has to recognise exactly what the compiler recognises, or a lazy value nested in
    one of those escapes it.
    """
    return isinstance(value, Mapping) or (hasattr(value, "keys") and hasattr(value, "__getitem__"))


def _materialise(cls: type, value: Any, depth: int, seen: frozenset[int]) -> Any:
    """A spec with every lazy iterable in it spent, exactly once.

    An adapter's output is compiled twice -- once here to validate it, once with the rest of the
    projection -- so a generator or iterator left inside would come back empty the second time and its
    keys would be dropped from the bytes without a word.

    `depth` and `seen` (the `id()` of every container on the path here) stand in for the bounds the
    compiler would have applied: without them a cyclic or absurdly deep output raises `RecursionError`
    from this walk instead of the compiler's own message.
    """

    def invalid(what: str) -> TypeError:
        return TypeError(f"projection adapter for {cls.__qualname__} returned an invalid spec: {what}")

    if depth > _MAX_SPEC_DEPTH:
        raise invalid(f"nesting exceeds {_MAX_SPEC_DEPTH} levels")
    mapping = _is_spec_mapping(value)
    if not mapping and (isinstance(value, (bool, str)) or not isinstance(value, Iterable)):
        return value  # a `str` is iterable but is a leaf here; the compile rejects it where invalid
    if id(value) in seen:
        raise invalid("it contains itself")
    inner = seen | {id(value)}
    if mapping:
        source = cast("Mapping[Any, Any]", value)  # duck-typed too: only keys() and indexing are used
        out: dict[str, Any] = {}
        for key in source.keys():  # noqa: SIM118  (a protocol object need not be iterable itself)
            if not isinstance(key, str):
                raise TypeError(
                    f"projection adapter for {cls.__qualname__} returned a mapping with non-str keys, "
                    "expected a mapping of JSON keys to specs"
                )
            out[key] = _materialise(cls, source[key], depth + 1, inner)
        return out
    # the compiler reads a non-mapping iterable as a flat list of leaf keys, so its members sit at this
    # level, not one below it: only a mapping's values go deeper
    return [_materialise(cls, item, depth, inner) for item in value]


def _adapter_spec(cls: type, result: Any) -> dict[str, Any]:
    """An adapter's return value as a spec, or `TypeError` naming the class."""
    if not _is_spec_mapping(result):
        raise TypeError(
            f"projection adapter for {cls.__qualname__} returned {type(result).__name__}, "
            "expected a mapping of JSON keys to specs"
        )
    spec = cast("dict[str, Any]", _materialise(cls, result, 0, frozenset()))
    try:
        Projection(spec)  # compile now, so a grammar error names the class instead of surfacing later
    except TypeError as e:
        raise TypeError(f"projection adapter for {cls.__qualname__} returned an invalid spec: {e}") from e
    return spec


KeyPath = Union[str, tuple[str, ...]]  # noqa: UP007  (runtime alias must work on 3.9)
"""One JSON key, or a nested key path, as given to `migration_adapter`."""


def _key_path(path: KeyPath) -> tuple[str, ...]:
    """One path, validated. A segment named `__all__` means "keep that container whole".

    `__all__` is the array wildcard of the spec grammar, so it cannot also be an ordinary key inside a
    path: `("old", "__all__")` becomes `("old",)`, which keeps `old` whole however the paths are
    ordered. A path that starts with it would ask for the root object, which no spec can express.
    """
    if isinstance(path, str):
        path = (path,)
    elif not (isinstance(path, tuple) and path and all(isinstance(seg, str) for seg in path)):
        raise TypeError(f"a path is a JSON key or a tuple of keys, not {path!r}")
    if path[0] == "__all__":
        raise TypeError('a path cannot start with "__all__": the root object cannot be kept whole')
    return path[: path.index("__all__")] if "__all__" in path else path


def _keep_path(spec: dict[str, Any], path: tuple[str, ...]) -> None:
    """Merge `path` into `spec` as a kept path.

    Every conflict widens to `True`: keeping more is always safe.
    """
    node = spec
    for seg in path[:-1]:
        child = node.get(seg)
        if child is True:
            return  # already kept whole
        if child is None:
            child = node[seg] = {}
        elif not isinstance(child, dict) or "__all__" in child:
            node[seg] = True  # an array spec (or anything else) where the path expects an object
            return
        node = child
    node[path[-1]] = True


def _collection(value: Any, what: str) -> Any:
    """A bare `str` here is an iterable of characters, never what the caller meant."""
    if isinstance(value, str):
        raise TypeError(f"{what} must be a collection of names or paths, not a str: {value!r}")
    return value


def migration_adapter(
    *,
    inputs: Mapping[str, Iterable[KeyPath]] | None = None,
    controls: Iterable[KeyPath] = (),
    requires: Mapping[str, Iterable[str]] | None = None,
) -> ProjectionAdapter:
    """An adapter built from what a class's migrations read.

    `inputs`: retained field -> JSON paths a migration may read to produce it (legacy locations and
    fallbacks alike); merged into the derived spec only when that field is retained. `controls`: paths kept
    whenever present, regardless of exclusions -- keys a validator dispatches on or checks for mutual
    exclusion. `requires`: retained field -> retained fields it depends on; a dependency that is excluded
    is a `ValueError` at construction of the projection. Field names that the class does not declare are
    a `ValueError` too.

    A bare `str` where a collection of paths or names is expected is a `TypeError`: it would iterate as
    characters. A path segment named `__all__` truncates the path there, keeping that container whole.
    """
    input_paths = {
        field: [_key_path(p) for p in _collection(paths, f"the input paths for {field!r}")]
        for field, paths in (inputs or {}).items()
    }
    control_paths = [_key_path(p) for p in _collection(controls, "controls")]
    dependencies = {
        field: list(_collection(deps, f"the dependencies of {field!r}"))
        for field, deps in (requires or {}).items()
    }

    def adapter(ctx: AdapterContext) -> dict[str, Any]:
        declared = set(ctx.model.model_fields)
        named = set(input_paths) | set(dependencies) | {d for deps in dependencies.values() for d in deps}
        unknown = sorted(named - declared)
        if unknown:
            raise ValueError(
                f"{ctx.model.__qualname__}: migration_adapter names fields the class does not declare: "
                f"{unknown}"
            )
        for field, deps in dependencies.items():
            if field in ctx.fields:
                for dep in deps:
                    if dep not in ctx.fields:
                        raise ValueError(
                            f"{ctx.model.__qualname__}: retaining {field!r} requires {dep!r}, "
                            f"which is excluded"
                        )
        for field, paths in input_paths.items():
            if field in ctx.fields:
                for path in paths:
                    _keep_path(ctx.spec, path)
        for path in control_paths:
            _keep_path(ctx.spec, path)
        return ctx.spec

    return adapter


@dataclass(frozen=True)
class ProjectionFallback:
    """A boundary where exclusion cannot be proved, without running validation.

    `path` uses model field names from the root, with `__all__` for array items; an empty path is the
    root. It identifies the boundary, not every occurrence beneath it or a literal JSON path. Alias
    reasons name the JSON key involved. `exclusions` is a read-only mapping of affected model classes
    to their excluded field names. `reason` describes either a subtree kept whole or a migration that
    may recreate excluded input keys before field validation. Boundaries unrelated to requested
    exclusions are omitted.
    """

    path: tuple[str, ...]
    reason: str
    exclusions: Mapping[type, frozenset[str]]


@dataclass(frozen=True)
class ProjectionPlan:
    """A derived keep-spec and conservative proof of exclusion, without a validator.

    `spec` is a fresh dict suitable for `Projection`. `complete` describes JSON input-key coverage:
    excluded keys are provably removed at every occurrence, and no migration can recreate them before
    field validation. It does not guarantee final attribute values: after validators can assign any
    attribute, including an excluded one. `fallbacks` explains the boundaries that prevent the input-key
    proof. The record is frozen; its spec remains an editable dict, but coverage is not recomputed after
    edits. Coverage does not prove that arbitrary model validators accept the input.
    """

    spec: dict[str, Any]
    complete: bool
    fallbacks: tuple[ProjectionFallback, ...]


def _derive_spec(
    model: type[BaseModel],
    excluded: dict[type, frozenset[str]],
    adapters: Mapping[type, ProjectionAdapter],
) -> ProjectionPlan:
    """The keep-spec for `model` minus `excluded`, and whether it covers every occurrence of every class.

    Every shape the derivation cannot describe is kept whole instead: a class that appears inside itself
    (a spec is a finite tree), an `extra='allow'` class with no excluded fields of its own (its
    undeclared keys are data), a before/wrap/plain validator (its input is not the shape its inner
    schema describes), and everything unknown (dicts, unions, fixed tuples, dataclasses, TypedDicts,
    `Any`, enclosing objects named by a multi-segment alias path, colliding keys).

    A class with a registered adapter is the exception for before/wrap wrappers: its retained fields are
    derived as usual and the adapter says what the validator reads; a migration may recreate an excluded
    key from those inputs, so such a class leaves `complete` False whenever an excluded class is at or
    below it. The adapter speaks only for the class's own registered model validators (see
    `_adapted_model_behind`); any other wrapper keeps the class opaque.
    """
    schema: dict[str, Any] = cast("dict[str, Any]", model.__pydantic_core_schema__)
    definitions = _definitions(schema)
    _validate_excluded(model, excluded, schema, definitions)
    fallbacks: list[ProjectionFallback] = []
    wrapper_kind: list[str] = []
    foreign: list[tuple[str, type]] = []  # (wrapper kind, adapted class) for wrappers it does not own
    derived: set[type] = set()  # classes this call has already derived, for real or to discard
    own_chains: dict[type, list[int] | None] = {}  # each adapted class's own outer wrapper sequence

    def record(node: dict[str, Any], path: tuple[str, ...], reason: str) -> None:
        affected = {
            n["cls"]: excluded[n["cls"]] for n in _subtree_models(node, definitions) if excluded.get(n["cls"])
        }
        if affected:
            fallbacks.append(
                ProjectionFallback(path=path, reason=reason, exclusions=MappingProxyType(affected))
            )

    def opaque(
        node: dict[str, Any], seen: frozenset[type], path: tuple[str, ...], reason: str | None = None
    ) -> bool:
        """Keep this subtree whole, and derive the adapted classes in it for their errors alone.

        Every fallback goes through here. Below a kept subtree nothing is projected, so an excluded
        class reachable in it keeps its keys and the derivation is no longer complete.

        An adapter has to see every occurrence of its class: keeping the bytes whole answers "what
        survives projection", not "can this migration live with these exclusions", and a `requires`
        conflict or a misspelt field name must be reported wherever the class appears, not only where
        the projection happens to reach. The spec that comes back is thrown away.

        `derived` makes that pass linear. A class is derived once per call, and deriving it already
        walked its own descendants, so a nested subtree's classes are all in `derived` by the time an
        enclosing subtree gets to them -- without that, each level would redo every level below it,
        which costs 2**N adapter calls for N nested layers. It subsumes the `seen` ancestry check too:
        the model branch adds a class to `derived` before it adds it to `seen`. It is also why calling
        this on a schema the derivation has just walked is cheap rather than free: the classes it
        reached are skipped, and only the ones it stepped over -- below an excluded field -- are run.
        """
        record(node, path, reason or f"opaque {node.get('type', 'schema')} subtree kept whole")
        for model_node in _subtree_models(node, definitions):
            if model_node["cls"] in adapters and model_node["cls"] not in derived:
                checkpoint = len(fallbacks)
                spec_for(model_node, seen, path)  # marks it derived on the way in
                del fallbacks[checkpoint:]  # this derivation checks adapter errors, not projected boundaries
        return True

    def spec_for(node: dict[str, Any], seen: frozenset[type], path: tuple[str, ...]) -> Any:
        t = node.get("type")
        if t == "definitions":  # root of every model with recursive references
            return spec_for(node["schema"], seen, path)
        if t == "definition-ref":
            target = definitions.get(node["schema_ref"])
            return opaque(node, seen, path) if target is None else spec_for(target, seen, path)
        if t in _OPAQUE_WRAPPERS:
            behind, owned = _adapted_model_behind(node, definitions, adapters, own_chains)
            if owned and behind is not None:
                # one of the class's own registered model validators; its adapter says what it reads
                return spec_for(behind, seen, path)
            if behind is not None:
                foreign.append((t, behind["cls"]))  # adapted, but this wrapper is somebody else's
            wrapper_kind.append(t)
            return opaque(node, seen, path)
        if t in ("nullable", "default", "function-after"):
            # function-after runs on what the inner schema already validated, so that shape is known
            return (
                spec_for(node["schema"], seen, path)
                if isinstance(node.get("schema"), dict)
                else opaque(node, seen, path)
            )
        if t in ("list", "set", "frozenset"):
            if not isinstance(node.get("items_schema"), dict):
                return opaque(node, seen, path)
            return {"__all__": spec_for(node["items_schema"], seen, (*path, "__all__"))}
        if (
            t == "tuple"
            and node.get("variadic_item_index") is not None
            and len(node.get("items_schema", [])) == 1
        ):
            return {"__all__": spec_for(node["items_schema"][0], seen, (*path, "__all__"))}  # tuple[X, ...]
        if t == "model":
            cls = node["cls"]
            derived.add(cls)  # before anything below can ask for it again
            inner = seen | {cls}  # a fallback on this class must not derive it again
            if cls in seen:
                return opaque(node, inner, path, "recursive model subtree kept whole")
            if _extra(cls) == "allow" and not excluded.get(cls):
                # undeclared keys are data on this class, so keep the whole object
                return opaque(node, inner, path, "extra='allow' model subtree kept whole")
            adapter = adapters.get(cls)
            fields, kind = _fields_node(node, definitions)
            if kind is not None and (adapter is None or fields is None):
                # a before/wrap validator's input is not the shape its fields describe: only an adapter
                # can say what it reads -- and a plain validator has no fields to retain at all
                wrapper_kind.append(kind)
                return opaque(node, inner, path, f"opaque {kind} validator subtree kept whole")
            if fields is None:
                return opaque(node, inner, path)
            out: dict[str, Any] = {}
            sources: dict[str, dict[str, dict[str, Any]]] = {}  # every field schema a kept key came from
            for name, field in fields["fields"].items():
                if name in excluded.get(cls, ()):
                    continue
                fschema = field["schema"]
                sub = spec_for(fschema, inner, (*path, name))
                for key, direct in _field_keys(name, field.get("validation_alias")):
                    # a multi-segment alias path names an enclosing object, not the field's own value.
                    # `opaque`, even though `spec_for` has just walked this: it skipped the field's
                    # excluded fields, and an adapted class under one of those has had no adapter call
                    value = (
                        sub
                        if direct
                        else opaque(
                            fschema, inner, (*path, name), f"alias path through JSON key {key!r} kept whole"
                        )
                    )
                    sources.setdefault(key, {})[name] = fschema
                    if out.get(key, value) != value:
                        # two fields under one JSON key describe it differently: keep it whole. Every
                        # field that has described this key so far is now inside a kept subtree, not
                        # just this one and the previous: two of them can agree while a third differs.
                        # Clearing the contributors keeps that linear -- once the key is `True`, the fields
                        # already walked stay walked, and a later collision only brings its own
                        value = True
                        for field_name, contributor in sources[key].items():
                            opaque(
                                contributor,
                                inner,
                                (*path, field_name),
                                f"colliding aliases for JSON key {key!r} kept whole",
                            )
                        sources[key] = {}
                    out[key] = value
            for name in sorted(excluded.get(cls, ())):
                field = fields["fields"].get(name)
                if field is None:
                    continue
                keys = {key for key, _ in _field_keys(name, field.get("validation_alias"))}
                if field["schema"].get("type") == "default":
                    keys.add(_EXCLUDED_ALIAS_PREFIX + name)
                collisions = sorted(keys.intersection(out))
                if collisions:
                    fallbacks.append(
                        ProjectionFallback(
                            path=(*path, name),
                            reason=f"colliding aliases retain excluded field keys: {collisions!r}",
                            exclusions=MappingProxyType({cls: frozenset({name})}),
                        )
                    )
            if adapter is None:
                return out
            retained = frozenset(n for n in fields["fields"] if n not in excluded.get(cls, ()))
            spec = _adapter_spec(cls, adapter(AdapterContext(cls, retained, out)))
            record(node, path, "migration may recreate excluded fields from retained inputs")
            return spec
        return opaque(node, seen, path)  # dict, unions, fixed tuples, dataclasses, TypedDicts, Any, scalars

    root = spec_for(schema, frozenset(), ())
    in_schema = {n["cls"] for n in _subtree_models(schema, definitions)}
    absent = [cls for cls in adapters if cls not in in_schema]
    if absent:
        names = ", ".join(sorted(cls.__qualname__ for cls in absent))
        raise ValueError(
            f"projection adapter registered for {names}, which does not appear in the schema of "
            f"{model.__qualname__}; adapters are looked up by exact class, so a subclass needs its "
            "own entry"
        )
    if not isinstance(root, dict):
        if _extra(model) == "allow" and not excluded.get(model):
            raise TypeError(
                f"cannot derive a projection for {model.__qualname__}: it allows extra fields and none "
                "of its own fields are excluded; exclude a field on it or use projected_validator alone"
            )
        if foreign:
            kind, cls = foreign[0]
            raise TypeError(
                f"cannot derive a projection for {model.__qualname__}: a {kind} validator that is not "
                f"one of {cls.__qualname__}'s own model validators wraps it, so the adapter for "
                f"{cls.__qualname__} cannot vouch for what it reads; use projected_validator alone"
            )
        if wrapper_kind:
            kind = wrapper_kind[0]
            hint = (
                "it replaces validation, so there are no fields to project"
                if kind == "function-plain"
                else f"register a projection adapter for {model.__qualname__} (projection_adapters=...) "
                "to say what it reads"
            )
            raise TypeError(
                f"cannot derive a projection for {model.__qualname__}: a {kind} validator wraps its "
                f"fields, so the shape of its input is unknown; {hint}, or use projected_validator alone"
            )
        raise TypeError(f"{model.__qualname__}: could not derive an object spec from its core schema")
    return ProjectionPlan(spec=root, complete=not fallbacks, fallbacks=tuple(fallbacks))


def projection_plan(
    model: type[BaseModel],
    exclude: Exclude,
    *,
    projection_adapters: Mapping[type, ProjectionAdapter] | None = None,
) -> ProjectionPlan:
    """Plan field exclusion and report any boundaries where removal cannot be proved.

    No validator is constructed. Unknown model classes or field names in `exclude` raise `ValueError`,
    including classes beneath excluded parent fields; malformed exclusion collections raise `TypeError`.
    Adapters are applied as in `projection_spec`, including their field and dependency checks. A plan
    can describe incomplete coverage even when `Projected` would refuse the model's configuration.
    Root shapes that cannot be expressed as an object keep-spec still raise `TypeError`.
    """
    return _derive_spec(model, normalize_exclude(model, exclude), dict(projection_adapters or {}))


def projection_spec(
    model: type[BaseModel],
    exclude: Exclude,
    *,
    projection_adapters: Mapping[type, ProjectionAdapter] | None = None,
) -> dict[str, Any]:
    """Derive the keep-spec for `model` minus the excluded fields from its core schema.

    `projection_adapters` maps classes to adapters that say what their before/wrap validators read
    (see `ProjectionAdapter` and `migration_adapter`). Lookup is by exact class, and an adapter for a
    class absent from the schema is a `ValueError`. Unknown excluded classes or field names are also
    a `ValueError`. Use `projection_plan` to inspect conservative exclusion coverage and its fallbacks.
    """
    return projection_plan(model, exclude, projection_adapters=projection_adapters).spec


class Projected:
    """`validate_json` for `model` that never parses the excluded fields or unknown keys.

    Combines a byte projection (unwanted members are skipped by the Rust cursor) with a validator built
    from the model's core schema minus the excluded fields. Results are instances of the original classes.
    Exclusion controls input keys, not final attribute values: an after validator may assign an excluded
    attribute from retained data.

    The projection keeps whole every subtree it cannot describe (see `_derive_spec`). An excluded class
    inside one still sees its keys, so it must use `extra='ignore'`, and an excluded field with a default
    must not be populated by name; `projected_validator` raises `ValueError` otherwise. `validate_json`
    refuses `by_name=True` and `by_alias=False` for the same reason while `complete` is False.
    A retained field using an excluded defaulted field's synthetic alias is refused too.

    Classes behind a ``model_validator(mode='before')`` or ``'wrap'`` are projected only when
    ``projection_adapters`` holds an adapter for them (see `migration_adapter`); the adapter says which
    JSON inputs the migration reads. An adapter vouches only for the class's own registered model
    validators: a validator installed from outside -- a field validator on the enclosing model, an
    ``Annotated`` validator, even one that is a bound method of the adapted class -- keeps the class
    opaque. Such classes keep the guards on when exclusion touches them.

    Adapters are looked up by exact class, so a subclass needs its own entry, and an adapter for a class
    that has no ``model`` node in ``model``'s schema is a ``ValueError``: silently unused knowledge is
    how a projection quietly stops matching the validators it was written for.
    """

    def __init__(
        self,
        model: type[BaseModel],
        exclude: Exclude,
        *,
        projection_adapters: Mapping[type, ProjectionAdapter] | None = None,
    ) -> None:
        self.model = model
        self.exclude = normalize_exclude(model, exclude)
        self.projection_adapters: dict[type, ProjectionAdapter] = dict(projection_adapters or {})
        plan = _derive_spec(model, self.exclude, self.projection_adapters)
        self._complete = plan.complete
        # the guards in projected_validator are only unnecessary when the projection removes every occurrence
        self.validator = projected_validator(model, self.exclude, assume_projected=self._complete)
        self.spec = Projection(plan.spec)

    @property
    def complete(self) -> bool:
        """Whether every excluded JSON input key is provably removed before field validation.

        A conservative proof, not a measurement: `True` only when every occurrence of every excluded
        class is described by the derived spec, so no excluded key can survive projection and no
        pre-validation migration can recreate one from retained inputs. This says nothing about final
        attribute values: after validators may assign excluded attributes. `False` whenever an excluded
        class sits inside a kept-whole subtree, or below a class whose adapter kept inputs a migration could
        rebuild it from, or an alias collision retains an excluded key. Read-only: it decides whether
        the `projected_validator` config guards are dropped and whether `validate_json` accepts `by_name`,
        and lowering the bar after construction would not make either safe.
        """
        return self._complete

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
                f"{self.model.__qualname__}: the projection cannot prove that every excluded key is "
                "gone -- it keeps some subtrees whole or retains colliding alias keys, or a migration "
                "validator could recreate a key "
                "from the inputs an adapter kept -- so validating by name would read an excluded key "
                "that is left. Drop by_name/by_alias, or exclude nothing inside those subtrees."
            )
        return self.validator.validate_json(
            self.spec(data), strict=strict, context=context, by_alias=by_alias, by_name=by_name
        )

    def __repr__(self) -> str:
        ex = ", ".join(f"{c.__qualname__}: {sorted(n)}" for c, n in self.exclude.items())
        adapted = ", ".join(c.__qualname__ for c in self.projection_adapters)
        tail = f", projection_adapters=[{adapted}]" if adapted else ""
        return f"Projected({self.model.__qualname__}, exclude={{{ex}}}{tail})"
