import dataclasses
import importlib
import math
import sys
from typing import Any, Generic, TypeVar, Union
from unittest import mock

import pydantic
import pytest
from pydantic import (
    AliasChoices,
    AliasPath,
    BaseModel,
    ConfigDict,
    Field,
    RootModel,
    field_validator,
    model_validator,
)
from typing_extensions import TypedDict

from json_projection import project
from json_projection.pydantic import Projected, projected_validator, projection_spec


class Event(BaseModel):
    ts: float
    payload: dict[str, Any]


class Sample(BaseModel):
    id: int
    score: float
    events: list[Event]


class Log(BaseModel):
    name: str
    samples: list[Sample]
    created_by: str = Field(validation_alias="createdBy")
    debug: dict[str, Any] = {}


RAW = (
    b'{"name":"run","createdBy":"r","debug":{"big":[1,2,3]},'
    b'"samples":[{"id":0,"score":NaN,"events":[{"ts":1,"payload":{"k":"v"}}]},'
    b'{"id":1,"score":0.1,"events":[]}]}'
)


def test_required_excluded_field_is_absent_and_default_excluded_field_keeps_default():
    v = projected_validator(Log, {Log: {"debug"}, Sample: {"events"}})
    log = v.validate_json(RAW)
    assert type(log) is Log and type(log.samples[0]) is Sample
    assert log.debug == {}
    assert "events" not in log.samples[0].__dict__
    assert sorted(log.samples[0].model_fields_set) == ["id", "score"]
    with pytest.raises(AttributeError):
        _ = log.samples[0].events
    assert math.isnan(log.samples[0].score)
    assert log.created_by == "r"


def test_excluded_value_is_never_validated():
    v = projected_validator(Log, {Log: {"debug"}, Sample: {"events"}})
    raw = b'{"name":"x","createdBy":"y","debug":"not a dict","samples":[{"id":1,"score":1,"events":7}]}'
    log = v.validate_json(raw)
    assert log.debug == {} and log.samples[0].id == 1


def test_original_class_still_validates_fully():
    projected_validator(Log, {Sample: {"events"}})
    full = Log.model_validate_json(RAW)
    assert len(full.samples[0].events) == 1


def test_plain_set_means_the_root_model():
    v = projected_validator(Log, {"debug"})
    assert v.validate_json(RAW).debug == {}


def test_unknown_field_is_value_error():
    with pytest.raises(ValueError, match="nope"):
        projected_validator(Log, {Log: {"nope"}})


def test_forbid_models_are_refused():
    class Strict(BaseModel):
        model_config = ConfigDict(extra="forbid")
        a: int
        b: list[int]

    with pytest.raises(ValueError, match="forbid"):
        projected_validator(Strict, {"b"})


def test_recursive_model_excluded_at_every_depth():
    class Node(BaseModel):
        v: int
        children: list["Node"] = []
        blob: dict[str, Any] = {}

    Node.model_rebuild()
    v = projected_validator(Node, {Node: {"blob"}})
    n = v.validate_json(b'{"v":1,"blob":{"x":[1]},"children":[{"v":2,"blob":{"y":2}}]}')
    assert n.blob == {} and n.children[0].blob == {} and n.children[0].v == 2


def test_partial_instance_dumps_without_warning(recwarn):
    v = projected_validator(Log, {Sample: {"events"}})
    s = v.validate_json(RAW).samples[1]
    assert s.model_dump() == {"id": 1, "score": 0.1}
    assert s.model_dump_json() == '{"id":1,"score":0.1}'
    assert not [w for w in recwarn if "Pydantic" in str(w.message)]


def test_allow_models_are_refused_standalone():
    class Loose(BaseModel):
        model_config = ConfigDict(extra="allow")
        a: int
        b: list[int]
        c: dict[str, Any] = {}

    with pytest.raises(ValueError, match="allow"):
        projected_validator(Loose, {"b"})
    with pytest.raises(ValueError, match="allow"):
        projected_validator(Loose, {"c"})


def test_by_name_models_are_refused_for_defaulted_fields_only():
    class ByName(BaseModel):
        model_config = ConfigDict(populate_by_name=True)
        a: int = Field(validation_alias="A")
        debug: dict[str, Any] = {}
        events: list[int]

    with pytest.raises(ValueError, match="by name"):
        projected_validator(ByName, {"debug"})
    v = projected_validator(ByName, {"events"})  # required: removed, key becomes unknown and is ignored
    out = v.validate_json(b'{"a": 1, "events": [1, 2, 3]}')
    assert "events" not in out.__dict__ and out.a == 1


def test_assume_projected_skips_the_guards():
    class Strict(BaseModel):
        model_config = ConfigDict(extra="forbid")
        a: int
        b: list[int]

    v = projected_validator(Strict, {"b"}, assume_projected=True)
    # input already projected: no excluded key present
    assert "b" not in v.validate_json(b'{"a": 1}').__dict__


def test_projection_spec_derives_keys_aliases_and_all():
    spec = projection_spec(Log, {Log: {"debug"}, Sample: {"events"}})
    assert spec == {
        "name": True,
        "samples": {"__all__": {"id": True, "score": True}},
        "createdBy": True,
        "created_by": True,
    }


def test_projection_spec_alias_choices_and_path_are_conservative():
    class A(BaseModel):
        x: int = Field(validation_alias=AliasChoices("X", "x_alt"))
        y: int = Field(validation_alias=AliasPath("outer", "inner"))
        z: list[int] = []

    spec = projection_spec(A, {A: {"z"}})
    assert spec == {"X": True, "x_alt": True, "x": True, "outer": True, "y": True}


def test_projection_spec_recursion_and_unknown_shapes_fall_back_to_true():
    class Node(BaseModel):
        v: int
        children: list["Node"] = []
        extra: dict[str, Any] = {}
        anything: Any = None

    Node.model_rebuild()
    spec = projection_spec(Node, {Node: {"extra"}})
    assert spec == {"v": True, "children": {"__all__": True}, "anything": True}


def test_projected_end_to_end():
    thin = Projected(Log, {Log: {"debug"}, Sample: {"events"}})
    log = thin.validate_json(RAW)
    assert type(log) is Log and len(log.samples) == 2 and log.debug == {}
    assert "events" not in log.samples[0].__dict__
    # the derived Projection is reusable on its own and equals a one-shot projection with the same spec
    assert thin.spec(RAW) == project(RAW, projection_spec(Log, thin.exclude))
    assert thin.validate_json(RAW.decode()).name == "run"


def test_projection_spec_variadic_tuple_uses_all():
    class T(BaseModel):
        tags: tuple[str, ...]
        fixed: tuple[int, str]
        junk: dict[str, Any] = {}

    assert projection_spec(T, {T: {"junk"}}) == {"tags": {"__all__": True}, "fixed": True}


def test_projected_forwards_kwargs_and_errors_match_pydantic():
    thin = Projected(Log, {Sample: {"events"}})
    with pytest.raises(pydantic.ValidationError) as ours:
        thin.validate_json(b'{"name": 1, "createdBy": "r", "samples": []}', strict=True)
    with pytest.raises(pydantic.ValidationError) as theirs:
        Log.model_validate_json(b'{"name": 1, "createdBy": "r", "samples": []}', strict=True)
    assert ours.value.errors()[0]["type"] == theirs.value.errors()[0]["type"] == "string_type"
    bad = b'{"name":"x","createdBy":"y","samples":[],"junk":[1,2,}'
    with pytest.raises(pydantic.ValidationError) as ours:
        thin.validate_json(bad)
    with pytest.raises(pydantic.ValidationError) as theirs:
        Log.model_validate_json(bad)
    assert ours.value.errors()[0]["msg"] == theirs.value.errors()[0]["msg"]


def test_generic_model():
    T = TypeVar("T")

    class Box(BaseModel, Generic[T]):
        item: T
        meta: dict[str, Any] = {}

    thin = Projected(Box[int], {Box[int]: {"meta"}})
    assert thin.validate_json(b'{"item": 1, "meta": {"z": 1}}').meta == {}


def test_projected_handles_configs_the_standalone_validator_refuses():
    class Strict(BaseModel):
        model_config = ConfigDict(extra="forbid")
        a: int
        b: list[int]

    class Loose(BaseModel):
        model_config = ConfigDict(extra="allow")
        a: int
        b: list[int]
        c: dict[str, Any] = {}

    class ByName(BaseModel):
        model_config = ConfigDict(populate_by_name=True)
        a: int = Field(validation_alias="A")
        debug: dict[str, Any] = {}

    raw = b'{"a": 1, "A": 1, "b": [1, 2], "c": {"x": 1}, "debug": {"leak": 1}, "other": 5}'
    s = Projected(Strict, {"b"}).validate_json(b'{"a": 1, "b": [1, 2]}')
    assert s.a == 1 and "b" not in s.__dict__
    lo = Projected(Loose, {"b", "c"}).validate_json(raw)
    assert lo.a == 1 and "b" not in lo.__dict__ and lo.c == {} and not lo.model_extra
    bn = Projected(ByName, {"debug"}).validate_json(raw)
    assert bn.a == 1 and bn.debug == {}


def test_projected_recursive_models_keep_the_guards():
    class StrictNode(BaseModel):
        model_config = ConfigDict(extra="forbid")
        v: int
        children: list["StrictNode"] = []
        secret: dict[str, Any]

    class LooseNode(BaseModel):
        model_config = ConfigDict(extra="allow")
        v: int
        children: list["LooseNode"] = []
        secret: dict[str, Any]

    StrictNode.model_rebuild()
    LooseNode.model_rebuild()
    with pytest.raises(ValueError, match="forbid"):
        Projected(StrictNode, {StrictNode: {"secret"}})
    with pytest.raises(ValueError, match="allow"):
        Projected(LooseNode, {LooseNode: {"secret"}})


def test_projected_recursive_ignore_model_excludes_at_every_depth():
    class Node(BaseModel):
        v: int
        children: list["Node"] = []
        secret: dict[str, Any]
        blob: dict[str, Any] = {}

    Node.model_rebuild()
    raw = (
        b'{"v":1,"secret":{"x":1},"blob":{"a":1},'
        b'"children":[{"v":2,"secret":{"y":2},"blob":{"b":2},"children":[]}]}'
    )
    n = Projected(Node, {Node: {"secret", "blob"}}).validate_json(raw)
    assert "secret" not in n.__dict__ and n.blob == {}
    child = n.children[0]
    assert "secret" not in child.__dict__ and child.blob == {} and child.v == 2


def test_import_guard_rejects_old_pydantic(monkeypatch):
    import json_projection.pydantic as jpp

    monkeypatch.setattr(pydantic, "VERSION", "2.12.5")
    sys.modules.pop("json_projection.pydantic", None)
    with pytest.raises(ImportError, match="pydantic>=2.13"):
        importlib.import_module("json_projection.pydantic")
    monkeypatch.undo()
    sys.modules["json_projection.pydantic"] = jpp  # restore for the remaining tests


def test_prebuilt_substitution_is_detected():
    with mock.patch("json_projection.pydantic.SchemaValidator") as fake:
        fake.return_value = mock.Mock(
            __repr__=lambda self: "SchemaValidator(validator=PrebuiltValidator(...))"
        )
        with pytest.raises(RuntimeError, match="prebuilt"):
            projected_validator(Log, {Sample: {"events"}})


def test_projection_spec_multi_segment_alias_path_keeps_the_outer_object_whole():
    class Sub(BaseModel):
        x: int

    class A(BaseModel):
        s: Sub = Field(validation_alias=AliasPath("outer", "inner"))
        junk: dict[str, Any] = {}

    raw = b'{"outer": {"inner": {"x": 1}}, "junk": {"big": 1}}'
    assert projection_spec(A, {"junk"}) == {"outer": True, "s": {"x": True}}
    assert Projected(A, {"junk"}).validate_json(raw).s.x == 1


def test_projection_spec_alias_choices_with_a_path_keeps_the_outer_object_whole():
    class Sub(BaseModel):
        x: int

    class A(BaseModel):
        s: Sub = Field(validation_alias=AliasChoices("s", AliasPath("outer", "inner")))
        junk: dict[str, Any] = {}

    assert projection_spec(A, {"junk"}) == {"s": {"x": True}, "outer": True}
    raw = b'{"outer": {"inner": {"x": 1}}, "junk": {"big": 1}}'
    assert Projected(A, {"junk"}).validate_json(raw).s.x == 1


def test_projection_spec_key_collision_falls_back_to_true():
    class Sub(BaseModel):
        x: int

    class Coll(BaseModel):
        x: dict[str, Any]
        s: Sub = Field(validation_alias="x")

    assert projection_spec(Coll, set()) == {"x": True, "s": {"x": True}}
    raw = b'{"x": {"x": 1, "other": 2}}'
    assert Projected(Coll, set()).validate_json(raw).x == Coll.model_validate_json(raw).x


def test_nested_extra_allow_model_without_own_exclusions_is_kept_whole():
    class Loose(BaseModel):
        model_config = ConfigDict(extra="allow")
        a: int

    class Outer(BaseModel):
        inner: Loose
        junk: dict[str, Any] = {}

    raw = b'{"inner": {"a": 1, "extra_k": 2}, "junk": {"z": 1}}'
    assert projection_spec(Outer, {"junk"}) == {"inner": True}
    out = Projected(Outer, {"junk"}).validate_json(raw)
    assert out.inner.model_extra == {"extra_k": 2} and out.junk == {}


def test_root_extra_allow_without_own_exclusions_is_refused():
    class Sub(BaseModel):
        x: int
        junk: dict[str, Any] = {}

    class Root(BaseModel):
        model_config = ConfigDict(extra="allow")
        inner: Sub

    with pytest.raises(TypeError, match="allows extra fields"):
        projection_spec(Root, {Sub: {"junk"}})
    with pytest.raises(TypeError, match="allows extra fields"):
        Projected(Root, {Sub: {"junk"}})


def test_bare_str_exclude_is_a_type_error():
    with pytest.raises(TypeError, match="not a str"):
        projected_validator(Log, "debug")
    with pytest.raises(TypeError, match="not a str"):
        Projected(Log, "debug")


def test_forbid_root_stays_projected_when_no_excluded_class_is_below_an_allow_model():
    class Loose(BaseModel):
        model_config = ConfigDict(extra="allow")
        a: int

    class Strict(BaseModel):
        model_config = ConfigDict(extra="forbid")
        nested: Loose
        secret: dict[str, Any]

    raw = b'{"nested": {"a": 1, "extra_k": 2}, "secret": {"big": 1}}'
    out = Projected(Strict, {"secret"}).validate_json(raw)
    assert "secret" not in out.__dict__
    assert out.nested.a == 1 and out.nested.model_extra == {"extra_k": 2}


def test_an_excluded_class_below_an_allow_model_keeps_the_guards():
    class Deep(BaseModel):
        model_config = ConfigDict(extra="forbid")
        d: int
        secret: dict[str, Any]

    class Loose(BaseModel):
        model_config = ConfigDict(extra="allow")
        deep: Deep

    class Strict(BaseModel):
        model_config = ConfigDict(extra="forbid")
        nested: Loose
        secret: dict[str, Any]

    with pytest.raises(ValueError, match="forbid"):
        Projected(Strict, {Strict: {"secret"}, Deep: {"secret"}})

    class Plain(BaseModel):  # nothing on the root itself trips a guard: only Deep can
        nested: Loose
        junk: dict[str, Any] = {}

    with pytest.raises(ValueError, match="Deep"):
        Projected(Plain, {Plain: {"junk"}, Deep: {"secret"}})


def _child(**config: Any) -> type[BaseModel]:
    class Child(BaseModel):
        model_config = ConfigDict(**config)  # type: ignore[typeddict-item]
        keep: int
        secret: int = 0

    return Child


def _dict_shape(Child):
    class Outer(BaseModel):
        children: dict[str, Child]  # type: ignore[valid-type]

    return Outer, b'{"children":{"a":{"keep":1,"secret":9}}}', lambda o: o.children["a"]


def _union_shape(Child):
    class Outer(BaseModel):
        u: Union[Child, int]  # type: ignore[valid-type]

    return Outer, b'{"u":{"keep":1,"secret":9}}', lambda o: o.u


def _fixed_tuple_shape(Child):
    class Outer(BaseModel):
        t: tuple[Child, int]  # type: ignore[valid-type]

    return Outer, b'{"t":[{"keep":1,"secret":9},2]}', lambda o: o.t[0]


def _dataclass_shape(Child):
    @dataclasses.dataclass
    class DC:
        c: Child  # type: ignore[valid-type]

    class Outer(BaseModel):
        d: DC

    return Outer, b'{"d":{"c":{"keep":1,"secret":9}}}', lambda o: o.d.c


def _typeddict_shape(Child):
    class TD(TypedDict):
        c: Child  # type: ignore[valid-type]

    class Outer(BaseModel):
        t: TD

    return Outer, b'{"t":{"c":{"keep":1,"secret":9}}}', lambda o: o.t["c"]


def _alias_path_shape(Child):
    class Outer(BaseModel):
        c: Child = Field(validation_alias=AliasPath("a", "b"))  # type: ignore[valid-type]

    return Outer, b'{"a":{"b":{"keep":1,"secret":9}}}', lambda o: o.c


def _collision_shape(Child):
    class Outer(BaseModel):
        x: dict[str, Any]
        c: Child = Field(validation_alias="x")  # type: ignore[valid-type]

    return Outer, b'{"x":{"keep":1,"secret":9}}', lambda o: o.c


def _before_validator_shape(Child):
    class Outer(BaseModel):
        c: Child  # type: ignore[valid-type]

        @field_validator("c", mode="before")
        @classmethod
        def pre(cls, value):
            return value

    return Outer, b'{"c":{"keep":1,"secret":9}}', lambda o: o.c


def _wrap_validator_shape(Child):
    class Outer(BaseModel):
        c: Child  # type: ignore[valid-type]

        @field_validator("c", mode="wrap")
        @classmethod
        def around(cls, value, handler):
            return handler(value)

    return Outer, b'{"c":{"keep":1,"secret":9}}', lambda o: o.c


def _allow_model_shape(Child):
    class Loose(BaseModel):
        model_config = ConfigDict(extra="allow")
        c: Child  # type: ignore[valid-type]

    class Outer(BaseModel):
        inner: Loose

    return Outer, b'{"inner":{"c":{"keep":1,"secret":9}}}', lambda o: o.inner.c


SHAPES = {
    "dict": _dict_shape,
    "union": _union_shape,
    "fixed_tuple": _fixed_tuple_shape,
    "dataclass": _dataclass_shape,
    "typeddict": _typeddict_shape,
    "alias_path": _alias_path_shape,
    "key_collision": _collision_shape,
    "field_validator_before": _before_validator_shape,
    "field_validator_wrap": _wrap_validator_shape,
    "extra_allow_parent": _allow_model_shape,
}


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_excluded_class_under_an_opaque_shape_still_gets_its_default(shape):
    Child = _child()
    Outer, raw, get = SHAPES[shape](Child)
    child = get(Projected(Outer, {Child: {"secret"}}).validate_json(raw))
    assert child.keep == 1, shape
    assert child.secret == 0, shape  # the projection cannot reach it, but the validator ignores the key


@pytest.mark.parametrize("shape", sorted(SHAPES))
@pytest.mark.parametrize(
    ("config", "match"),
    [({"extra": "forbid"}, "forbid"), ({"extra": "allow"}, "allow"), ({"validate_by_name": True}, "by name")],
)
def test_excluded_class_under_an_opaque_shape_keeps_the_guards(shape, config, match):
    Child = _child(**config)
    Outer, _, _ = SHAPES[shape](Child)
    with pytest.raises(ValueError, match=match):
        Projected(Outer, {Child: {"secret"}})


def test_field_before_validator_input_is_preserved():
    class Child(BaseModel):
        x: int

    class Outer(BaseModel):
        child: Child

        @field_validator("child", mode="before")
        @classmethod
        def adapt(cls, value):
            return {"x": value["source"]}

    raw = b'{"child":{"source":1}}'
    assert Projected(Outer, set()).validate_json(raw) == Outer.model_validate_json(raw)


def test_root_model_before_validator_cannot_be_projected():
    class Root(BaseModel):
        x: int

        @model_validator(mode="before")
        @classmethod
        def pre(cls, value):
            return value

    with pytest.raises(TypeError, match="function-before"):
        Projected(Root, {"x"})
    with pytest.raises(TypeError, match="function-wrap"):

        class Wrapped(BaseModel):
            x: int

            @model_validator(mode="wrap")
            @classmethod
            def around(cls, value, handler):
                return handler(value)

        Projected(Wrapped, {"x"})


def test_a_default_that_looks_like_a_schema_is_not_walked():
    class M(BaseModel):
        options: dict[str, str] = {"type": "model"}
        junk: dict[str, Any] = {}

    assert Projected(M, {"junk"}).validate_json(b'{"junk":{"a":1}}').options == {"type": "model"}


def test_a_self_referential_default_is_not_copied():
    cyclic: dict[str, Any] = {}
    cyclic["self"] = cyclic

    class M(BaseModel):
        options: dict[str, Any] = Field(default=cyclic)
        junk: dict[str, Any] = {}

    out = Projected(M, {"junk"}).validate_json(b"{}")
    assert out.options["self"] is out.options  # pydantic deep-copies the default; the cycle survives


def test_the_original_core_schema_is_not_mutated():
    class Sub(BaseModel):
        keep: int
        secret: int = 0

    class Outer(BaseModel):
        sub: Sub

    before = repr(Outer.__pydantic_core_schema__)
    Projected(Outer, {Sub: {"secret"}})
    assert repr(Outer.__pydantic_core_schema__) == before
    assert Outer.model_validate_json(b'{"sub":{"keep":1,"secret":9}}').sub.secret == 9


def test_root_model_fields_are_not_taken_from_the_wrapped_class():
    class Child(BaseModel):
        x: int

    class Root(RootModel[Child]):
        pass

    with pytest.raises(ValueError, match="Root.x"):
        projected_validator(Root, {"x"})
    with pytest.raises(TypeError):
        projection_spec(Root, set())
    with pytest.raises(TypeError):
        Projected(Root, {"x"})
