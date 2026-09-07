from __future__ import annotations

import gc
import sys
from collections.abc import Iterable

import pytest

from json_projection import Projection, ProjectionStream
from reference import canon, corpus, parse, reference_project


def run_chunks(projection: Projection, chunks: Iterable[bytes]) -> bytes:
    session = projection.stream()
    for chunk in chunks:
        session.feed(chunk)
    return session.finish()


def test_stream_public_api():
    session = Projection({"id"}).stream()
    session.feed(b'{"id":1,"discard":[')
    session.feed(b"1,2,3]}")
    assert session.finish() == b'{"id":1}'


def test_sessions_are_created_by_the_projection():
    with pytest.raises(TypeError):
        ProjectionStream()


def test_input_chunk_is_not_retained_after_feed():
    chunk = b'{"drop":"' + b"x" * 100_000 + b'"}'
    references = sys.getrefcount(chunk)
    session = Projection(set()).stream()
    session.feed(chunk)
    assert sys.getrefcount(chunk) == references
    assert session.finish() == b"{}"


def test_interleaved_sessions_own_their_plan():
    projection = Projection({"id"})
    first = projection.stream()
    second = projection.stream()
    del projection
    gc.collect()
    first.feed(b'{"id":')
    second.feed(b'{"id":2}')
    first.feed(b"1}")
    assert first.finish() == b'{"id":1}'
    assert second.finish() == b'{"id":2}'


def test_finish_is_terminal():
    session = Projection(set()).stream()
    session.feed(b"{}")
    assert session.finish() == b"{}"
    with pytest.raises(RuntimeError):
        session.finish()
    with pytest.raises(RuntimeError):
        session.feed(b"")


def test_parse_failure_is_terminal():
    session = Projection({"id"}).stream()
    with pytest.raises(ValueError):
        session.feed(b'{"id":1,"drop":[1,]}')
    with pytest.raises(RuntimeError):
        session.finish()
    with pytest.raises(RuntimeError):
        session.feed(b"")


def test_incomplete_finish_fails():
    session = Projection({"id"}).stream()
    session.feed(b'{"id":1')
    with pytest.raises(ValueError, match="byte 7"):
        session.finish()
    with pytest.raises(RuntimeError):
        session.finish()


@pytest.mark.parametrize("value", ["{}", bytearray(b"{}"), memoryview(b"{}"), None, 1])
def test_bad_argument_does_not_poison_session(value):
    session = Projection(set()).stream()
    with pytest.raises(TypeError):
        session.feed(value)
    session.feed(b"{}")
    assert session.finish() == b"{}"


@pytest.mark.parametrize(
    "raw",
    [
        b"{}",
        b" \n\t{}\r\n ",
        b'{"\\u0069d":1,"drop":[true,{"x":"\\uD83D\\uDE00"}],'
        b'"items":[{"id":2,"drop":null},{"id":3}],"id":1.25e+03}',
        '{"日本":1,"é":2,"drop":"日本"}'.encode(),
        b'{"items":{"unexpected": [1,2]},"id":-Infinity}',
        b'{"id":NaN,"id":Infinity,"id":-Infinity}',
        b'{ "id" \t : \n [ 1 , 2 ] , "items": [ { "id" : 1 } , {} ] }',
    ],
)
def test_every_split_preserves_raw_output(raw: bytes):
    projection = Projection({"id": True, "items": {"__all__": {"id": True}}, "日本": True, "é": True})
    expected = projection(raw, strict=True)
    for split in range(len(raw) + 1):
        assert run_chunks(projection, (raw[:split], b"", raw[split:])) == expected
    assert run_chunks(projection, (raw[i : i + 1] for i in range(len(raw)))) == expected


def test_generated_documents_match_both_references():
    for raw, spec in corpus(23, 300):
        projection = Projection(spec)
        expected = projection(raw, strict=True)
        for size in (1, 2, 7, 64, 4096):
            actual = run_chunks(projection, (raw[i : i + size] for i in range(0, len(raw), size)))
            assert actual == expected
            assert canon(parse(actual)) == canon(reference_project(parse(raw), spec))


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b" ",
        b"[]",
        b"null",
        b"{}[]",
        b'{"x":1,}',
        b'{"drop":[1,]}',
        b'{"drop":truex}',
        b'{"drop":01}',
        b'{"drop":1e+}',
        b'{"drop":1.}',
        b'{"drop":[1}',
        b'{"drop":{"x" 1}}',
        b'{"drop":"\\q"}',
        b'{"drop":"\\uDC00"}',
        b'{"drop":"\\uD800\\u0041"}',
        b'{"drop":"\\uD800"}',
        b'{"drop":"line\nbreak"}',
        b'{"\xff":1}',
        b'{"drop":"\\u00XX"}',
        b'{"drop":',
    ],
)
def test_errors_and_offsets_are_independent_of_chunking(raw: bytes):
    projection = Projection({"id"})
    with pytest.raises(ValueError) as whole:
        run_chunks(projection, (raw,))
    for split in range(len(raw) + 1):
        with pytest.raises(ValueError) as split_error:
            run_chunks(projection, (raw[:split], raw[split:]))
        assert str(split_error.value) == str(whole.value)


def test_every_incomplete_prefix_fails():
    raw = b'{"id":1,"drop":[true,{"text":"a\\uD83D\\uDE00"}]}'
    projection = Projection({"id"})
    for end in range(len(raw)):
        with pytest.raises(ValueError):
            run_chunks(projection, (raw[:end],))


@pytest.mark.parametrize("keep", [True, False])
@pytest.mark.parametrize("depth", [199, 200, 201])
def test_global_depth_limit(keep: bool, depth: int):
    raw = b'{"id":' + b"[" * (depth - 1) + b"0" + b"]" * (depth - 1) + b"}"
    projection = Projection({"id"} if keep else set())
    if depth > 200:
        with pytest.raises(ValueError, match="nesting"):
            run_chunks(projection, (raw,))
    else:
        assert run_chunks(projection, (raw,)) == projection(raw, strict=True)


def test_invalid_raw_utf8_is_preserved_or_discarded():
    raw = b'{"id":"\xff","drop":{"\xff":"\xfe"}}'
    assert run_chunks(Projection(set()), (raw,)) == b"{}"
    assert run_chunks(Projection({"id"}), (raw,)) == b'{"id":"\xff"}'
    assert run_chunks(Projection({"drop"}), (raw,)) == b'{"drop":{"\xff":"\xfe"}}'


@pytest.mark.parametrize(
    "raw",
    [
        b'{"\xff":1}',
        b'{"\\u0061\xff":1}',
        b'{"first":0, "\\u0061\xff":1}',
        b'{"first":0, "\xc3\xa9\\uD83D\\uDE00\xff":1}',
    ],
)
def test_invalid_lookup_key_utf8_reports_original_byte_offset(raw: bytes):
    projection = Projection(set())
    offset = raw.index(b"\xff")
    for split in range(len(raw) + 1):
        with pytest.raises(ValueError, match=rf"byte {offset}:"):
            run_chunks(projection, (raw[:split], raw[split:]))


@pytest.mark.parametrize("negative", [False, True])
@pytest.mark.parametrize("digits", [4299, 4300, 4301])
def test_integer_prefix_limit(negative: bool, digits: int):
    raw = b'{"id":' + (b"-" if negative else b"") + b"1" * digits + b"}"
    projection = Projection({"id"})
    chunks = (raw[i : i + 17] for i in range(0, len(raw), 17))
    if digits + negative > 4300:
        with pytest.raises(ValueError):
            run_chunks(projection, chunks)
    else:
        assert run_chunks(projection, chunks) == raw


@pytest.mark.parametrize("number", [b"0." + b"1" * 10000, b"1e" + b"1" * 10000])
def test_long_fraction_and_exponent(number: bytes):
    raw = b'{"id":' + number + b"}"
    assert run_chunks(Projection({"id"}), (raw[i : i + 31] for i in range(0, len(raw), 31))) == raw
