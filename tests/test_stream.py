from __future__ import annotations

import gc
import gzip
import io
import signal
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path

import pytest

from json_projection import Projection, ProjectionStream
from reference import canon, corpus, exclusion_corpus, parse, reference_exclude, reference_project


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


@pytest.mark.parametrize(
    "raw",
    [
        b"{}",
        b'{ "drop":1,"new":2,"drop":3,"id":1.00,"new":NaN}',
        b'{"samples":[{"events":[1],"id":2},null,{"events":3,"future":4}]}',
        b'{"samples":{"events":1},"new":[ {"drop":2} ]}',
        '{"\\u0064rop":1,"日本":2,"é":"keep"}'.encode(),
        b'{"drop":"\\uD83D\\uDE00","new":Infinity}',
    ],
)
def test_excluding_stream_matches_whole_buffer_at_every_split(raw: bytes):
    projection = Projection.excluding({"drop": True, "samples": {"__all__": {"events": True}}})
    expected = projection(raw, strict=True)
    for split in range(len(raw) + 1):
        assert run_chunks(projection, (raw[:split], b"", raw[split:])) == expected
    assert projection.apply_stream(io.BytesIO(raw), chunk_size=1) == expected


def test_excluding_stream_matches_generated_reference():
    for raw, spec in exclusion_corpus(29, 300):
        projection = Projection.excluding(spec)
        expected = projection(raw, strict=True)
        for size in (1, 7, 64):
            actual = run_chunks(projection, (raw[i : i + size] for i in range(0, len(raw), size)))
            assert actual == expected
            assert canon(parse(actual)) == canon(reference_exclude(parse(raw), spec))


@pytest.mark.parametrize("raw", [b'{"drop":[1,]}', b'{"new":"\\q"}', b'{"drop":"\\uD800"}', b"{}x"])
def test_excluding_stream_error_offsets_do_not_depend_on_chunking(raw: bytes):
    projection = Projection.excluding({"drop"})
    with pytest.raises(ValueError) as expected:
        run_chunks(projection, (raw,))
    for split in range(len(raw) + 1):
        with pytest.raises(ValueError) as actual:
            run_chunks(projection, (raw[:split], raw[split:]))
        assert str(actual.value) == str(expected.value)


def test_apply_stream_reads_from_current_position_and_leaves_source_open() -> None:
    source = io.BytesIO(b'prefix{"id":1,"drop":[2,3]} \n')
    source.seek(len(b"prefix"))
    assert Projection({"id"}).apply_stream(source) == b'{"id":1}'
    assert source.tell() == len(source.getvalue())
    assert not source.closed


@pytest.mark.parametrize("compressed", [False, True])
def test_apply_stream_reads_binary_files(tmp_path: Path, compressed: bool) -> None:
    raw = b'{"id":1,"drop":[2,3]}'
    path = tmp_path / "source"
    path.write_bytes(gzip.compress(raw) if compressed else raw)
    with gzip.open(path, "rb") if compressed else path.open("rb") as source:
        assert Projection({"id"}).apply_stream(source, chunk_size=7) == b'{"id":1}'
        assert source.read() == b""
        assert not source.closed


@pytest.mark.parametrize("chunk_size", [None, 1, 7])
def test_apply_stream_accepts_short_reads(chunk_size: int | None) -> None:
    class Reader:
        def __init__(self) -> None:
            self.source = io.BytesIO(b'{"id":1,"drop":[2,3]}')
            self.requests: list[int] = []

        def read(self, size: int, /) -> bytes:
            self.requests.append(size)
            return self.source.read(min(size, 3))

    source = Reader()
    projection = Projection({"id"})
    result = (
        projection.apply_stream(source)
        if chunk_size is None
        else projection.apply_stream(source, chunk_size=chunk_size)
    )
    assert result == b'{"id":1}'
    assert len(source.requests) > 1
    assert set(source.requests) == {65536 if chunk_size is None else chunk_size}


@pytest.mark.parametrize("raw", [b"", b'{"id":1', b'{"id":1} \n!', b'{"drop":[1,]}'])
def test_apply_stream_preserves_json_errors(raw: bytes) -> None:
    projection = Projection({"id"})
    with pytest.raises(ValueError) as expected:
        run_chunks(projection, (raw,))
    source = io.BytesIO(b"prefix" + raw)
    source.seek(len(b"prefix"))
    with pytest.raises(ValueError) as actual:
        projection.apply_stream(source, chunk_size=1)
    assert str(actual.value) == str(expected.value)
    assert not source.closed


@pytest.mark.parametrize("chunk_size", [0, -1])
def test_apply_stream_rejects_nonpositive_chunk_size_before_reading(chunk_size: int) -> None:
    source = io.BytesIO(b"{}")
    with pytest.raises(ValueError, match="chunk_size must be positive"):
        Projection(set()).apply_stream(source, chunk_size=chunk_size)
    assert source.tell() == 0
    assert not source.closed


@pytest.mark.parametrize("chunk_size", [None, 1.5, "7"])
def test_apply_stream_requires_integer_chunk_size(chunk_size) -> None:
    source = io.BytesIO(b"{}")
    with pytest.raises(TypeError):
        Projection(set()).apply_stream(source, chunk_size=chunk_size)
    assert source.tell() == 0


@pytest.mark.parametrize("value", ["", "{}", None, bytearray(), memoryview(b"")])
def test_apply_stream_requires_binary_read_results(value) -> None:
    class Reader:
        def read(self, size: int, /):
            return value

    with pytest.raises(TypeError, match=r"source.read\(\) must return bytes"):
        Projection(set()).apply_stream(Reader())


class FailingReader:
    def __init__(self, prefix: bytes, error: OSError) -> None:
        self.prefix = prefix
        self.error = error
        self.requests = 0

    def read(self, size: int, /) -> bytes:
        self.requests += 1
        if self.requests == 1:
            return self.prefix[:size]
        raise self.error


def test_apply_stream_propagates_read_errors() -> None:
    error = OSError("connection lost")
    source = FailingReader(b'{"id":1,', error)
    projection = Projection({"id"})
    with pytest.raises(OSError) as actual:
        projection.apply_stream(source)
    assert actual.value is error
    assert projection.apply_stream(io.BytesIO(b'{"id":2}')) == b'{"id":2}'


def test_apply_stream_stops_reading_on_json_error() -> None:
    source = FailingReader(b'{"id":1,]', OSError("must not read after malformed input"))
    with pytest.raises(ValueError, match="invalid JSON"):
        Projection({"id"}).apply_stream(source)
    assert source.requests == 1


@pytest.mark.skipif(not hasattr(signal, "setitimer"), reason="requires POSIX interval timers")
def test_apply_stream_checks_signals_between_native_reads() -> None:
    probe = """
import io
import signal

from json_projection import Projection

raw = b'{"drop":[' + b'0,' * (32 * 1024 * 1024) + b'0]}'
source = io.BytesIO(raw)
projection = Projection(set())

def interrupt(signum, frame):
    raise TimeoutError("timer expired")

signal.signal(signal.SIGPROF, interrupt)
try:
    signal.setitimer(signal.ITIMER_PROF, 0.02)
    projection.apply_stream(source)
except TimeoutError:
    assert source.tell() < len(raw), (source.tell(), len(raw))
else:
    raise AssertionError("timer did not interrupt projection")
finally:
    signal.setitimer(signal.ITIMER_PROF, 0)
"""
    result = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


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
