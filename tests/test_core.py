import json
import math
from typing import Any

import pytest

from json_projection import Projection, project
from reference import corpus, reference_project


def test_project_root_keys_from_set():
    raw = b'{"a": 1.5, "skip": [1, 2, {"x": "y"}], "b": "s"}'
    assert project(raw, {"a"}) == b'{"a": 1.5}'


def test_projection_is_reusable_and_callable():
    p = Projection({"name": True, "items": {"__all__": {"id": True}}})
    raw = b'{"name":"n","items":[{"id":1,"big":[0]*3},{"id":2}],"meta":{}}'.replace(b"[0]*3", b"[0,0,0]")
    assert p(raw) == b'{"name":"n","items":[{"id":1},{"id":2}]}'
    assert p.apply(raw) == p(raw)
    assert "Projection(" in repr(p)


def test_nan_and_infinity_survive_verbatim():
    raw = b'{"a": NaN, "b": Infinity, "c": -Infinity, "skip": [NaN]}'
    out = project(raw, {"a", "b", "c"})
    assert out == b'{"a": NaN,"b": Infinity,"c": -Infinity}'
    parsed = json.loads(out)
    assert math.isnan(parsed["a"]) and parsed["b"] == math.inf and parsed["c"] == -math.inf


def test_passthrough_returns_same_bytes_object_on_json_error():
    bad = b'{"a": 1, "junk": [1, 2,]}'
    assert project(bad, {"a"}) is bad


def test_passthrough_on_non_object_root():
    for raw in (b"[1, 2]", b"42", b'"s"', b"", b"   "):
        assert project(raw, {"a"}) is raw


def test_strict_raises_value_error():
    with pytest.raises(ValueError, match="JSON"):
        project(b'{"a": 1, "junk": [1, 2,]}', {"a"}, strict=True)
    with pytest.raises(ValueError, match="object"):
        project(b"[1]", {"a"}, strict=True)


def test_accepts_str_bytearray_memoryview_and_returns_bytes():
    raw = '{"a": 1, "z": "é"}'
    expected = b'{"a": 1}'
    assert project(raw, {"a"}) == expected
    assert project(bytearray(raw.encode()), {"a"}) == expected
    assert project(memoryview(raw.encode()), {"a"}) == expected
    bad = "[1, 2]"
    assert project(bad, {"a"}) == b"[1, 2]"
    assert type(project(bytearray(b"[1]"), {"a"})) is bytes


def test_rejects_other_input_types():
    with pytest.raises(TypeError):
        project(123, {"a"})  # type: ignore[arg-type]


def test_bad_spec_is_type_error():
    with pytest.raises(TypeError):
        Projection({"a": 1})
    with pytest.raises(TypeError):
        Projection("abc")
    with pytest.raises(TypeError):
        Projection({"__all__": True})


def test_invalid_utf8_in_dropped_member_is_skipped():
    raw = b'{"a": 1.5, "skip": ["\xff\xfe"]}'
    assert project(raw, {"a"}) == b'{"a": 1.5}'
    with pytest.raises(UnicodeDecodeError):
        json.loads(raw)  # the unprojected document is invalid; the projected one is fine


def test_non_dict_mapping_spec_is_honoured():
    from types import MappingProxyType

    out = project(b'{"a": {"x": 1, "y": 2}, "b": 1}', MappingProxyType({"a": {"x": True}}))
    assert out == b'{"a": {"x": 1}}'


def _canon(obj):
    return json.dumps(obj, sort_keys=True)  # NaN/Infinity serialise as literals, so they compare equal


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_matches_reference_on_generated_corpus(seed):
    for raw, spec in corpus(seed, 1500):
        out = project(raw, spec)
        assert _canon(json.loads(out)) == _canon(reference_project(json.loads(raw), spec)), (raw, spec)


def test_self_referential_spec_is_rejected():
    d = {}
    d["self"] = d
    with pytest.raises(TypeError, match="nesting"):
        project(b"{}", d)


def test_spec_nesting_is_capped():
    deep: Any = True
    for _ in range(1000):
        deep = {"k": deep}
    assert project(b'{"k": 1}', deep) == b'{"k": 1}'
    for _ in range(1000):
        deep = {"k": deep}
    with pytest.raises(TypeError, match="nesting"):
        project(b"{}", deep)
