import json
import math
from typing import Any

import pytest

from json_projection import Projection, project
from reference import Obj, canon, corpus, exclusion_corpus, parse, reference_exclude, reference_project


def test_project_root_keys_from_set():
    raw = b'{"a": 1.5, "skip": [1, 2, {"x": "y"}], "b": "s"}'
    assert project(raw, {"a"}) == b'{"a": 1.5}'


def test_excluding_keeps_unspecified_members_and_is_reusable():
    projection = Projection.excluding({"drop"})
    raw = b'{"id":1,"drop":[2,3],"new":4}'
    assert projection(raw) == b'{"id":1,"new":4}'
    assert projection.apply(raw) == b'{"id":1,"new":4}'
    assert projection(b'{"future":{"drop":1}}') == b'{"future":{"drop":1}}'
    assert "Projection.excluding(" in repr(projection)


def test_excluding_nested_members_and_array_elements_preserves_unknown_data():
    projection = Projection.excluding(
        {"meta": {"secret"}, "samples": {"__all__": {"events": True, "store": {"secret": True}}}}
    )
    raw = (
        b'{"meta":{"secret":1,"new":2},"samples":['
        b'{"events":[1],"store":{"secret":2,"keep":3},"extra":4},'
        b'{"store":5},null,[{"events":6}]],"untouched":{"secret":7}}'
    )
    assert projection(raw, strict=True) == (
        b'{"meta":{"new":2},"samples":['
        b'{"store":{"keep":3},"extra":4},{"store":5},null,[{"events":6}]],'
        b'"untouched":{"secret":7}}'
    )


def test_excluding_matches_decoded_keys_and_preserves_retained_spelling():
    raw = b'{"\\u0064rop":0,"id":1.00,"drop":2,"id":NaN,"new":18446744073709551617}'
    assert Projection.excluding({"drop"})(raw, strict=True) == (
        b'{"id":1.00,"id":NaN,"new":18446744073709551617}'
    )


@pytest.mark.parametrize("spec", [{}, set(), [], {"absent": True}])
def test_empty_or_absent_exclusions_keep_all_members(spec):
    raw = b'{"id":1,"obj":{"x":2},"array":[3],"id":4}'
    assert Projection.excluding(spec)(raw, strict=True) == raw


def test_excluding_accepts_mapping_protocols_and_iterators():
    from types import MappingProxyType

    projection = Projection.excluding(MappingProxyType({"obj": iter(["drop"])}))
    assert projection(b'{"obj":{"drop":1,"keep":2},"other":3}') == b'{"obj":{"keep":2},"other":3}'
    assert Projection.excluding({"__all__"})(b'{"__all__":1,"keep":2}') == b'{"keep":2}'


@pytest.mark.parametrize(
    "spec", [True, False, "drop", {"drop": False}, {"drop": 1}, {1: True}, {"__all__": {}}]
)
def test_excluding_rejects_invalid_specs(spec):
    with pytest.raises(TypeError):
        Projection.excluding(spec)


def test_excluding_does_not_remove_array_elements():
    with pytest.raises(TypeError, match="array"):
        Projection.excluding({"items": {"__all__": True}})


@pytest.mark.parametrize("raw", [b'{"drop":[1,]}', b'{"keep":01}', b'{"drop":"\\q"}', b"{}x", b"[]"])
def test_excluding_retains_whole_buffer_error_contract(raw):
    projection = Projection.excluding({"drop"})
    assert projection(raw) is raw
    with pytest.raises(ValueError):
        projection(raw, strict=True)


@pytest.mark.parametrize("seed", [3, 9])
def test_excluding_matches_independent_reference(seed):
    for raw, spec in exclusion_corpus(seed, 1000):
        actual = Projection.excluding(spec)(raw, strict=True)
        assert canon(parse(actual)) == canon(reference_exclude(parse(raw), spec)), (raw, spec)


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


@pytest.mark.parametrize("constructor", [Projection, Projection.excluding])
def test_self_referential_spec_is_rejected(constructor):
    d = {}
    d["self"] = d
    with pytest.raises(TypeError, match="nesting"):
        constructor(d)


@pytest.mark.parametrize("constructor", [Projection, Projection.excluding])
def test_spec_nesting_is_capped(constructor):
    deep: Any = True
    for _ in range(256):  # MAX_DEPTH in src/spec.rs
        deep = {"k": deep}
    assert constructor(deep)(b'{"k": 1}') == b'{"k": 1}'
    for _ in range(2000):
        deep = {"k": deep}
    with pytest.raises(TypeError, match="nesting"):
        constructor(deep)


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
