import math
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict, Field

from json_projection.pydantic import projected_validator


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
    log = v.validate_json(b'{"name":"x","createdBy":"y","debug":"not a dict","samples":[{"id":1,"score":1,"events":7}]}')
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
