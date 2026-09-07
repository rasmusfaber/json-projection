import json
import math
from typing import Any

import pytest

from json_projection import Projection, project
from reference import Obj, canon, corpus, parse, reference_project


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


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_matches_reference_on_generated_corpus(seed):
    for raw, spec in corpus(seed, 1500):
        out = project(raw, spec)
        assert canon(parse(out)) == canon(reference_project(parse(raw), spec)), (raw, spec)


def _has_duplicate_key(node):
    if isinstance(node, Obj):
        return len({k for k, _ in node}) < len(node) or any(_has_duplicate_key(v) for _, v in node)
    if isinstance(node, list):
        return any(_has_duplicate_key(v) for v in node)
    return False


def _has_nested_all(spec):
    if not isinstance(spec, dict):
        return False
    inner = spec.get("__all__")
    return (isinstance(inner, dict) and "__all__" in inner) or any(_has_nested_all(v) for v in spec.values())


def test_corpus_covers_duplicate_keys_and_arrays_of_arrays_of_objects():
    docs = corpus(1, 1500)
    assert sum(_has_duplicate_key(parse(raw)) for raw, _ in docs) > 50
    assert sum(_has_nested_all(spec) for _, spec in docs) > 50


def test_self_referential_spec_is_rejected():
    d = {}
    d["self"] = d
    with pytest.raises(TypeError, match="nesting"):
        project(b"{}", d)


def test_spec_nesting_is_capped():
    deep: Any = True
    for _ in range(256):  # MAX_DEPTH in src/spec.rs
        deep = {"k": deep}
    assert project(b'{"k": 1}', deep) == b'{"k": 1}'
    for _ in range(2000):
        deep = {"k": deep}
    with pytest.raises(TypeError, match="nesting"):
        project(b"{}", deep)


def test_a_mapping_that_mutates_the_spec_while_it_compiles_does_not_panic():
    spec: dict[str, Any] = {}

    class Mutator:
        def keys(self):
            spec["added"] = True  # mutates the dict whose iteration produced this value
            return []

        def __getitem__(self, key):
            raise AssertionError("no keys to fetch")

    spec["x"] = Mutator()
    assert isinstance(project(b'{"x": 1, "added": 2}', spec), bytes)


def test_raw_key_spelling_and_projected_whitespace():
    raw = b'{ \n "\\u0061" \t : \n 1.00 , "drop": false , "a": [ 2 , 3 ] \n}'
    assert project(raw, {"a"}, strict=True) == b'{"\\u0061" \t : \n 1.00,"a": [ 2 , 3 ]}'


@pytest.mark.parametrize("negative", [False, True])
@pytest.mark.parametrize("digits", [4299, 4300, 4301])
def test_integer_prefix_limit_includes_sign(negative: bool, digits: int):
    raw = b'{"drop":' + (b"-" if negative else b"") + b"1" * digits + b"}"
    if digits + negative > 4300:
        with pytest.raises(ValueError):
            project(raw, set(), strict=True)
    else:
        assert project(raw, set(), strict=True) == b"{}"


@pytest.mark.parametrize("number", [b"0." + b"1" * 5000, b"1e" + b"1" * 5000])
def test_fraction_and_exponent_do_not_have_integer_length_limit(number: bytes):
    assert project(b'{"drop":' + number + b"}", set(), strict=True) == b"{}"


@pytest.mark.parametrize("escaped", [b"\\uDC00", b"\\uD800", b"\\uD800\\u0041"])
def test_invalid_surrogates_are_rejected_even_when_discarded(escaped: bytes):
    with pytest.raises(ValueError):
        project(b'{"drop":"' + escaped + b'"}', set(), strict=True)


def test_projection_descent_restarts_the_subtree_depth_budget():
    spec: Any = True
    raw = b"0"
    for _ in range(220):
        spec = {"k": spec}
        raw = b'{"k":' + raw + b"}"
    assert project(raw, spec, strict=True) == raw
