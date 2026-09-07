"""Projection adapters: projecting through models with migration (before/wrap) validators."""

import json
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from json_projection.pydantic import AdapterContext, Projected, _keep_path, migration_adapter, projection_spec
from migration_models import ADAPTERS as MIGRATION_ADAPTERS
from migration_models import Item, Log, Sample


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
    spec = projection_spec(Sample, {"events", "timelines"}, projection_adapters=MIGRATION_ADAPTERS)
    assert spec["score"] is True and spec["transcript"] == {"content": True}
    assert "events" not in spec and "timelines" not in spec
    spec = projection_spec(
        Sample, {"events", "timelines", "attachments"}, projection_adapters=MIGRATION_ADAPTERS
    )
    assert "transcript" not in spec and spec["score"] is True
    spec = projection_spec(Sample, {"scores"}, projection_adapters=MIGRATION_ADAPTERS)
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
        Projected(Sample, {"events"}, projection_adapters=MIGRATION_ADAPTERS)
    Projected(Sample, {"events", "timelines"}, projection_adapters=MIGRATION_ADAPTERS)  # both gone: fine


def test_an_emptied_legacy_object_keeps_its_presence():
    thin = Projected(Sample, {"events", "timelines"}, projection_adapters=MIGRATION_ADAPTERS)
    doc = b'{"id": 1, "transcript": {"events": [{"id": 9, "blob": {}}]}, "attachments": {"stale": "x"}}'
    assert json.loads(thin.spec(doc)) == {"id": 1, "transcript": {}, "attachments": {"stale": "x"}}
    # the (now empty) transcript still overwrites attachments, exactly as in plain validation
    assert thin.validate_json(doc).attachments == Sample.model_validate_json(doc).attachments == {}


def test_legacy_inputs_reach_the_migration():
    thin = Projected(Sample, {"store"}, projection_adapters=MIGRATION_ADAPTERS)
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
