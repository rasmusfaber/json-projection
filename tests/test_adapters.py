"""Projection adapters: projecting through models with migration (before/wrap) validators."""

import json
import time
from collections import Counter
from typing import Annotated, Any, Literal, Optional, Union

import pytest
from pydantic import (
    AliasPath,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    create_model,
    field_validator,
    model_validator,
)
from pydantic_core import core_schema

from json_projection.pydantic import AdapterContext, Projected, _keep_path, migration_adapter, projection_spec
from migration_models import ADAPTERS as MIGRATION_ADAPTERS
from migration_models import SAMPLE_ADAPTER, Item, Log, Sample

SAMPLE_ONLY = {Sample: SAMPLE_ADAPTER}  # adapters are refused for classes a root's schema lacks


class Legacy(BaseModel):
    """`name` used to be called `old_name`; the before validator renames it."""

    name: str
    items: list[Item] = []
    payload: dict[str, Any] = {}

    @model_validator(mode="before")
    @classmethod
    def migrate(cls, data: Any) -> Any:
        if isinstance(data, dict) and "old_name" in data:
            data = dict(data)
            data["name"] = data.pop("old_name")
        return data


class Holder(BaseModel):
    legacy: Legacy
    junk: dict[str, Any] = {}


class FieldValidated(BaseModel):
    """A before validator on the FIELD belongs to this class, not to Legacy.

    Legacy's adapter cannot vouch for it.
    """

    legacy: Legacy
    junk: dict[str, Any] = {}

    @field_validator("legacy", mode="before")
    @classmethod
    def unwrap(cls, value: Any) -> Any:
        return value["inner"] if isinstance(value, dict) and "inner" in value else value


def legacy_adapter(ctx: AdapterContext) -> dict[str, Any]:
    if "name" in ctx.fields:
        ctx.spec["old_name"] = True  # the migration reads the legacy key when name is wanted
    return ctx.spec


ADAPTERS = {Legacy: legacy_adapter}
LEGACY_DOC = b'{"old_name": "n", "items": [{"id": 1, "blob": {"big": [1, 2, 3]}}], "payload": {"big": true}}'
CURRENT_DOC = b'{"name": "n", "items": [{"id": 1, "blob": {"big": [1, 2, 3]}}], "payload": {"big": true}}'


def test_a_root_with_a_before_validator_needs_an_adapter():
    with pytest.raises(TypeError, match="function-before validator wraps its fields.*projection_adapters"):
        Projected(Legacy, {"payload"})


def test_an_adapter_projects_through_the_validator():
    assert projection_spec(Legacy, {"payload"}, projection_adapters=ADAPTERS) == {
        "name": True,
        "items": {"__all__": {"id": True, "blob": True}},
        "old_name": True,
    }
    thin = Projected(Legacy, {"payload"}, projection_adapters=ADAPTERS)
    for doc in (LEGACY_DOC, CURRENT_DOC):
        got = thin.validate_json(doc)
        assert (got.name, got.items[0].id, got.payload) == ("n", 1, {})
        assert b"payload" not in thin.spec(doc)
    assert b"old_name" in thin.spec(LEGACY_DOC)


def test_an_adapter_on_a_nested_class():
    exclude = {Holder: {"junk"}, Legacy: {"payload"}}
    doc = b'{"legacy": ' + LEGACY_DOC + b', "junk": {"x": 1}}'
    thin = Projected(Holder, exclude, projection_adapters=ADAPTERS)
    got = thin.validate_json(doc)
    assert (got.legacy.name, got.legacy.payload, got.junk) == ("n", {}, {})
    assert b"payload" not in thin.spec(doc)
    # without the adapter the nested class is kept whole: still correct, nothing inside it is dropped
    whole = Projected(Holder, exclude)
    assert whole.validate_json(doc).legacy.payload == {}
    assert b"payload" in whole.spec(doc)


def test_an_adapter_on_a_class_without_validators_is_applied():
    def drop_blob(ctx: AdapterContext) -> dict[str, Any]:
        assert ctx.model is Item and ctx.fields == {"id", "blob"}
        return {"id": True}

    thin = Projected(Legacy, {"payload"}, projection_adapters={Legacy: legacy_adapter, Item: drop_blob})
    assert thin.validate_json(CURRENT_DOC).items[0].blob == {}  # never in the bytes; the default applied


@pytest.mark.parametrize(
    "bad",
    [["name"], {"__all__": True, "name": True}, {1: True}],
    ids=["not-a-mapping", "all-mixed-with-keys", "non-str-key"],
)
def test_malformed_adapter_output_names_the_class(bad: Any):
    with pytest.raises(TypeError, match="projection adapter for Legacy"):
        Projected(Legacy, {"payload"}, projection_adapters={Legacy: lambda ctx: bad})


def test_an_adapter_may_refuse_an_exclusion_set():
    def refuse(ctx: AdapterContext) -> dict[str, Any]:
        raise ValueError("boom: name cannot be derived without payload")

    with pytest.raises(ValueError, match="boom"):
        Projected(Legacy, {"payload"}, projection_adapters={Legacy: refuse})


def test_an_adapted_class_with_exclusions_keeps_the_guards_on():
    thin = Projected(Legacy, {"payload"}, projection_adapters=ADAPTERS)
    assert thin.complete is False
    with pytest.raises(ValueError, match="by name"):
        thin.validate_json(CURRENT_DOC, by_name=True)

    class Forbid(Legacy):
        model_config = ConfigDict(extra="forbid")

    with pytest.raises(ValueError, match="extra='forbid'"):
        Projected(Forbid, {"payload"}, projection_adapters={Forbid: legacy_adapter})


def test_an_adapted_class_unrelated_to_exclusions_stays_complete():
    thin = Projected(Holder, {Holder: {"junk"}}, projection_adapters=ADAPTERS)
    assert thin.complete is True
    assert thin.validate_json(b'{"legacy": ' + LEGACY_DOC + b"}").legacy.name == "n"


def test_repr_lists_adapted_classes():
    assert "projection_adapters=[Legacy]" in repr(
        Projected(Legacy, {"payload"}, projection_adapters=ADAPTERS)
    )
    assert "projection_adapters" not in repr(Projected(Holder, {Holder: {"junk"}}))


def test_a_field_validator_on_an_adapted_class_stays_opaque():
    doc = b'{"legacy": {"inner": ' + LEGACY_DOC + b'}, "junk": {"x": 1}}'
    thin = Projected(
        FieldValidated, {FieldValidated: {"junk"}, Legacy: {"payload"}}, projection_adapters=ADAPTERS
    )
    assert (
        thin.complete is False
    )  # Legacy is kept whole behind the field validator, so its exclusion is not stripped
    got = thin.validate_json(doc)
    assert (got.legacy.name, got.legacy.payload, got.junk) == ("n", {}, {})
    assert b"inner" in thin.spec(doc) and b"payload" in thin.spec(doc)


def test_inputs_are_kept_only_for_retained_fields():
    spec = projection_spec(Sample, {"events", "timelines"}, projection_adapters=SAMPLE_ONLY)
    assert spec["score"] is True and spec["transcript"] == {"content": True}
    assert "events" not in spec and "timelines" not in spec
    spec = projection_spec(Sample, {"events", "timelines", "attachments"}, projection_adapters=SAMPLE_ONLY)
    assert "transcript" not in spec and spec["score"] is True
    spec = projection_spec(Sample, {"scores"}, projection_adapters=SAMPLE_ONLY)
    assert "score" not in spec and spec["transcript"] == {"events": True, "content": True}


def test_controls_are_kept_even_when_excluded():
    spec = projection_spec(Log, {"version"}, projection_adapters=MIGRATION_ADAPTERS)
    assert spec["version"] is True and "title" in spec  # name is retained, so its legacy input is too
    assert "title" not in projection_spec(Log, {"name"}, projection_adapters=MIGRATION_ADAPTERS)


@pytest.mark.parametrize(
    ("spec", "path", "expected"),
    [
        ({}, ("a", "b"), {"a": {"b": True}}),
        ({"a": True}, ("a", "b"), {"a": True}),
        ({"a": {"x": True}}, ("a", "b"), {"a": {"x": True, "b": True}}),
        ({"a": {"__all__": {"x": True}}}, ("a", "b"), {"a": True}),
        ({"a": {"x": True}}, ("a",), {"a": True}),
    ],
    ids=["create", "already-whole", "descend", "array-meets-object-widens", "leaf-onto-mapping-widens"],
)
def test_keep_path_merging(spec: dict[str, Any], path: tuple[str, ...], expected: dict[str, Any]):
    _keep_path(spec, path)
    assert spec == expected


def test_migration_adapter_rejects_unknown_field_names():
    bad = migration_adapter(inputs={"scroes": ["score"]})
    with pytest.raises(ValueError, match="does not declare.*scroes"):
        projection_spec(Sample, set(), projection_adapters={Sample: bad})
    bad = migration_adapter(requires={"timelines": ["evnets"]})
    with pytest.raises(ValueError, match="does not declare.*evnets"):
        projection_spec(Sample, set(), projection_adapters={Sample: bad})


def test_migration_adapter_rejects_bad_paths():
    with pytest.raises(TypeError, match="a path is a JSON key or a tuple of keys"):
        migration_adapter(inputs={"scores": [1]})  # type: ignore[list-item]


def test_a_dependency_on_an_excluded_field_is_a_conflict():
    with pytest.raises(ValueError, match="retaining 'timelines' requires 'events', which is excluded"):
        Projected(Sample, {"events"}, projection_adapters=SAMPLE_ONLY)
    Projected(Sample, {"events", "timelines"}, projection_adapters=SAMPLE_ONLY)  # both gone: fine


def test_an_emptied_legacy_object_keeps_its_presence():
    thin = Projected(Sample, {"events", "timelines"}, projection_adapters=SAMPLE_ONLY)
    doc = b'{"id": 1, "transcript": {"events": [{"id": 9, "blob": {}}]}, "attachments": {"stale": "x"}}'
    assert json.loads(thin.spec(doc)) == {"id": 1, "transcript": {}, "attachments": {"stale": "x"}}
    # the (now empty) transcript still overwrites attachments, exactly as in plain validation
    assert thin.validate_json(doc).attachments == Sample.model_validate_json(doc).attachments == {}


def test_legacy_inputs_reach_the_migration():
    thin = Projected(Sample, {"store"}, projection_adapters=SAMPLE_ONLY)
    doc = (
        b'{"id": 1, "score": 0.5, "store": {"big": [1, 2, 3]},'
        b' "timelines": [{"name": "t", "event_ids": [9]}],'
        b' "transcript": {"events": [{"id": 9, "blob": {"b": 1}}], "content": {"k": "v"}}}'
    )
    plain = Sample.model_validate_json(doc)
    got = thin.validate_json(doc)
    assert got.scores == plain.scores == {"legacy": 0.5}
    assert got.events == plain.events and got.attachments == plain.attachments == {"k": "v"}
    assert got.timelines == plain.timelines and got.store == {}


class Child(BaseModel):
    """`unpack` is a classmethod of this class, but not one of its model validators."""

    x: int

    @classmethod
    def unpack(cls, data: Any) -> Any:
        return data["inner"]


class Outer(BaseModel):
    child: Annotated[Child, BeforeValidator(Child.unpack)]


def test_a_bound_classmethod_used_as_an_annotated_validator_is_not_the_class_s_own():
    """Bound to `Child`, but registered on nothing: the adapter for `Child` cannot vouch for it."""
    raw = b'{"child": {"inner": {"x": 7}}}'
    thin = Projected(Outer, {}, projection_adapters={Child: migration_adapter()})
    assert b"inner" in thin.spec(raw)
    assert thin.validate_json(raw).child.x == Outer.model_validate_json(raw).child.x == 7


class StaticWrapped(BaseModel):
    """A `staticmethod` model validator is unbound, and still the class's own."""

    name: str = ""
    junk: dict[str, Any] = {}

    @model_validator(mode="wrap")  # pyright: ignore[reportArgumentType]  (a staticmethod validator takes no cls)
    @staticmethod
    def wrap(data: Any, handler: Any) -> Any:
        return handler(data)


def test_a_staticmethod_wrap_validator_is_projected_through():
    thin = Projected(StaticWrapped, {"junk"}, projection_adapters={StaticWrapped: migration_adapter()})
    assert thin.spec(b'{"name": "a", "junk": {"big": [1, 2]}}') == b'{"name": "a"}'


def _unpack(data: Any) -> Any:
    return data["inner"]


class ForeignWrapped(BaseModel):
    """A before validator installed around the whole class by something other than a model validator."""

    x: int = 0
    junk: dict[str, Any] = {}

    @classmethod
    def __get_pydantic_core_schema__(cls, source: Any, handler: Any) -> Any:
        return core_schema.no_info_before_validator_function(_unpack, handler(source))


def test_a_foreign_wrapper_at_the_root_says_so_instead_of_asking_for_an_adapter():
    with pytest.raises(TypeError, match="not one of ForeignWrapped's own model validators"):
        Projected(ForeignWrapped, {"junk"}, projection_adapters={ForeignWrapped: migration_adapter()})


def test_an_adapter_for_a_class_the_schema_lacks_is_refused():
    with pytest.raises(ValueError, match="Log, which does not appear in the schema of Sample"):
        projection_spec(Sample, {"store"}, projection_adapters=MIGRATION_ADAPTERS)


class Base(BaseModel):
    a: int = 0


class Sub(Base):
    b: int = 0


class SubHolder(BaseModel):
    sub: Sub


def test_an_adapter_is_looked_up_by_exact_class():
    control = migration_adapter(controls=["ctl"])
    with pytest.raises(ValueError, match="Base, which does not appear in the schema of SubHolder"):
        projection_spec(SubHolder, {Sub: {"b"}}, projection_adapters={Base: control})
    assert projection_spec(SubHolder, {Sub: {"b"}}, projection_adapters={Sub: control}) == {
        "sub": {"a": True, "ctl": True}
    }


class Tagged(BaseModel):
    """Adapted, and reachable only inside a discriminated union, which the derivation keeps whole."""

    tag: Literal["a"] = "a"
    values: list[int] = []
    total: int = 0
    junk: dict[str, Any] = {}

    @model_validator(mode="wrap")
    @classmethod
    def calculate(cls, data: Any, handler: Any) -> Any:
        obj = handler(data)
        obj.total = sum(obj.values)
        return obj


class Untagged(BaseModel):
    tag: Literal["b"] = "b"


class Union2(BaseModel):
    item: Annotated[Union[Tagged, Untagged], Field(discriminator="tag")]


def test_an_adapter_inside_a_kept_whole_subtree_still_refuses_a_conflict():
    """Keeping the bytes whole does not make the migration able to live with the exclusion."""
    adapters = {Tagged: migration_adapter(requires={"total": ["values"]})}
    with pytest.raises(ValueError, match="retaining 'total' requires 'values'"):
        Projected(Union2, {Tagged: {"values"}}, projection_adapters=adapters)
    with pytest.raises(ValueError, match="retaining 'total' requires 'values'"):
        Projected(Tagged, {"values"}, projection_adapters=adapters)


def test_a_non_conflicting_adapter_inside_a_union_still_validates():
    raw = b'{"item": {"tag": "a", "values": [2, 3], "junk": {"big": [1]}}}'
    adapters = {Tagged: migration_adapter(requires={"total": ["values"]})}
    thin = Projected(Union2, {Tagged: {"junk"}}, projection_adapters=adapters)
    assert thin.spec(raw) == raw  # the union is kept whole; the discarded derivation changes nothing
    item = thin.validate_json(raw).item
    assert isinstance(item, Tagged)
    assert item.total == 5 and item.junk == {}  # the migration ran on everything it needs


@pytest.mark.parametrize(
    "paths",
    [[("old", "__all__"), ("old", "other")], [("old", "other"), ("old", "__all__")]],
    ids=["all-first", "all-last"],
)
def test_an_all_segment_keeps_its_container_whole_whatever_the_order(paths: list[tuple[str, ...]]):
    spec = projection_spec(Item, set(), projection_adapters={Item: migration_adapter(controls=paths)})
    assert spec == {"id": True, "blob": True, "old": True}


@pytest.mark.parametrize(
    "kwargs",
    [
        {"inputs": {"id": "old_id"}},
        {"controls": "old_id"},
        {"requires": {"id": "blob"}},
    ],
    ids=["inputs", "controls", "requires"],
)
def test_a_bare_str_where_a_collection_is_expected_is_refused(kwargs: dict[str, Any]):
    with pytest.raises(TypeError, match="not a str"):
        migration_adapter(**kwargs)


def test_a_path_cannot_start_with_all():
    with pytest.raises(TypeError, match='cannot start with "__all__"'):
        migration_adapter(controls=[("__all__", "x")])


def test_complete_is_read_only():
    thin = Projected(Legacy, {"payload"}, projection_adapters=ADAPTERS)
    assert thin.complete is False
    with pytest.raises(AttributeError):
        thin.complete = True  # type: ignore[misc]
    assert thin.complete is False


class Twice(BaseModel):
    """Using `Legacy` twice makes pydantic hoist it into `definitions` and refer to it by ref."""

    a: Legacy
    b: Optional[Legacy] = None


def test_an_adapted_class_is_reached_through_a_definition_ref():
    assert Twice.__pydantic_core_schema__["type"] == "definitions"
    spec = projection_spec(Twice, {Legacy: {"payload"}}, projection_adapters=ADAPTERS)
    inner = {"name": True, "items": {"__all__": {"id": True, "blob": True}}, "old_name": True}
    assert spec == {"a": inner, "b": inner}


class SubLegacy(Legacy):
    """Inherits `Legacy.migrate`; the adapter must be registered for this exact class."""

    extra_note: str = ""


def test_a_subclass_inherits_the_validators_and_takes_its_own_adapter():
    thin = Projected(SubLegacy, {"payload"}, projection_adapters={SubLegacy: legacy_adapter})
    got = thin.validate_json(b'{"old_name": "n", "extra_note": "e", "payload": {"big": true}}')
    assert (got.name, got.extra_note, got.payload) == ("n", "e", {})


def test_each_occurrence_gets_a_fresh_context_spec():
    seen: list[dict[str, Any]] = []

    def record(ctx: AdapterContext) -> dict[str, Any]:
        assert not any(k.startswith("mark") for k in ctx.spec)  # nothing left over from the last call
        ctx.spec[f"mark{len(seen)}"] = True
        seen.append(ctx.spec)
        return ctx.spec

    spec = projection_spec(Twice, {Legacy: {"payload"}}, projection_adapters={Legacy: record})
    assert len(seen) == 2 and seen[0] is not seen[1]
    assert spec["a"]["mark0"] is True and "mark1" not in spec["a"]
    assert spec["b"]["mark1"] is True and "mark0" not in spec["b"]


def _dict_chain(depth: int) -> tuple[type[BaseModel], list[type[BaseModel]]]:
    """`Root.kids: dict[str, N{depth-1}]`, each `N{i}.kids: dict[str, N{i-1}]`, `N0.value: int`.

    Every dict value is a subtree the derivation keeps whole, so every level is a discard pass.
    """
    models: list[type[BaseModel]] = [create_model("N0", value=(int, 0))]
    for i in range(1, depth):
        models.append(create_model(f"N{i}", kids=(dict[str, models[-1]], {})))
    root = create_model("Root", kids=(dict[str, models[-1]], {}))
    return root, [*models, root]


def test_a_discarded_derivation_is_shared_across_nesting_levels():
    root, models = _dict_chain(10)
    calls: Counter[type] = Counter()

    def record(ctx: AdapterContext) -> dict[str, Any]:
        calls[ctx.model] += 1
        return ctx.spec

    assert projection_spec(root, set(), projection_adapters={m: record for m in models}) == {"kids": True}
    assert calls == Counter(models)  # exactly one call per class, not one per path to it


def test_nested_discarded_derivations_do_not_blow_up():
    root, models = _dict_chain(16)
    adapters = {m: migration_adapter() for m in models}
    start = time.perf_counter()
    projection_spec(root, set(), projection_adapters=adapters)
    assert time.perf_counter() - start < 0.5  # 2**16 adapter calls took a second and a half


class AliasHolder(BaseModel):
    """The alias path names an enclosing object, so the field's own schema is kept whole as well."""

    leaf: Legacy = Field(default=None, validation_alias=AliasPath("outer", "leaf"))  # type: ignore[assignment]


def test_a_field_behind_an_alias_path_calls_its_adapter_once():
    calls: Counter[type] = Counter()

    def record(ctx: AdapterContext) -> dict[str, Any]:
        calls[ctx.model] += 1
        return ctx.spec

    projection_spec(AliasHolder, {Legacy: {"payload"}}, projection_adapters={Legacy: record})
    assert calls == Counter({Legacy: 1})
