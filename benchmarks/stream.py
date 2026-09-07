"""Compare incremental projection with full-input projection and stdlib JSON parsing.

Run manually with a release build; the full default matrix is intentionally expensive::

    uv run maturin develop --uv --release
    uv run python benchmarks/stream.py --build-profile release > benchmarks/stream-results.md

A small smoke run exercises every shape and both source modes::

    uv run python benchmarks/stream.py --sizes-mib 0.0625 --chunk-kib 4 --source both
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import io
import json
import platform
import statistics
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import json_projection
from json_projection import Projection

SHAPES = ("dropped-string", "dropped-array", "dropped-number", "mostly-retained")
APPROACHES = ("stream", "full-input", "full-parse")
MIB = 1024 * 1024


@dataclass(frozen=True)
class Document:
    """An exact-sized document represented by a reusable block and small delimiters."""

    shape: str
    size_bytes: int
    chunk_bytes: int
    prefix: bytes
    suffix: bytes
    block: bytes
    blocks: int
    remainder: bytes
    payload_bytes: int

    def body_chunks(self) -> Iterator[bytes]:
        for _ in range(self.blocks):
            yield self.block
        if self.remainder:
            yield self.remainder

    def chunks(self, path: str | None = None) -> Iterator[bytes]:
        if path is not None:
            with open(path, "rb") as source:
                while chunk := source.read(self.chunk_bytes):
                    yield chunk
        else:
            yield self.prefix
            yield from self.body_chunks()
            yield self.suffix

    def validate(self, output: bytes | dict[str, Any]) -> int:
        """Check kept data without constructing a second complete expected document."""
        if self.shape != "mostly-retained":
            assert output == (b'{"id":1}' if isinstance(output, bytes) else {"id": 1})
            return len(b'{"id":1}')

        expected_size = len(self.prefix) + self.payload_bytes + len(b'"}')
        if isinstance(output, bytes):
            assert len(output) == expected_size
            expected = hashlib.sha256(self.prefix)
            for chunk in self.body_chunks():
                expected.update(chunk)
            expected.update(b'"}')
            assert hashlib.sha256(output).digest() == expected.digest()
        else:
            assert set(output) == {"id", "payload"}
            assert output["id"] == 1
            payload = output["payload"]
            assert isinstance(payload, str)
            assert len(payload) == self.payload_bytes
            assert payload.count("x") == self.payload_bytes
        return expected_size


def document(shape: str, size_bytes: int, chunk_bytes: int) -> Document:
    if shape == "dropped-string":
        prefix, unit, suffix = b'{"id":1,"drop":"', b"x", b'"}'
    elif shape == "dropped-array":
        prefix, unit, suffix = b'{"id":1,"drop":[', b'{"value":1,"text":"discarded"},', b"{}]}"
    elif shape == "dropped-number":
        prefix, unit, suffix = b'{"id":1,"drop":0.', b"1", b"}"
    elif shape == "mostly-retained":
        prefix, unit, suffix = b'{"id":1,"payload":"', b"x", b'","drop":false}'
    else:
        raise ValueError(f"Unknown shape: {shape}")
    payload_bytes = size_bytes - len(prefix) - len(suffix)
    if payload_bytes < 0 or chunk_bytes < max(len(prefix), len(unit), len(suffix)):
        raise ValueError("Document or chunk size is too small for JSON delimiters")
    units, padding = divmod(payload_bytes, len(unit))
    units_per_block = chunk_bytes // len(unit)
    blocks, remainder_units = divmod(units, units_per_block)
    return Document(
        shape=shape,
        size_bytes=size_bytes,
        chunk_bytes=chunk_bytes,
        prefix=prefix,
        suffix=suffix,
        block=unit * units_per_block,
        blocks=blocks,
        remainder=unit * remainder_units + b" " * padding,
        payload_bytes=payload_bytes,
    )


@dataclass(frozen=True)
class Case:
    shape: str
    size_bytes: int
    chunk_bytes: int
    source: str
    approach: str
    path: str | None


def execute(case: Case, source: Document, projection: Projection) -> bytes | dict[str, Any]:
    if case.approach == "stream":
        session = projection.stream()
        for chunk in source.chunks(case.path):
            session.feed(chunk)
        return session.finish()

    with io.BytesIO() as assembled:
        for chunk in source.chunks(case.path):
            assembled.write(chunk)
        raw = assembled.getvalue()
    if case.approach == "full-input":
        return projection(raw, strict=True)
    if case.approach == "full-parse":
        parsed = json.loads(raw)
        keys = ("id", "payload") if case.shape == "mostly-retained" else ("id",)
        return {key: parsed[key] for key in keys}
    raise ValueError(f"Unknown approach: {case.approach}")


def peak_rss_kib() -> int | None:
    if sys.platform != "linux":
        return None
    import resource

    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss


def measure(case: Case, repeats: int) -> dict[str, Any]:
    """Measure one cold process call before validation or timing can raise its RSS peak."""
    source = document(case.shape, case.size_bytes, case.chunk_bytes)
    keys = {"id", "payload"} if case.shape == "mostly-retained" else {"id"}
    projection = Projection(keys)
    gc.collect()
    before = peak_rss_kib()
    output = execute(case, source, projection)
    after = peak_rss_kib()
    output_bytes = source.validate(output)
    del output

    timings = []
    for _ in range(repeats):
        start = time.perf_counter()
        output = execute(case, source, projection)
        timings.append(time.perf_counter() - start)
        assert source.validate(output) == output_bytes
        del output
    return {
        "wall_ms": statistics.median(timings) * 1000,
        "rss_delta_mib": (after - before) / 1024 if before is not None and after is not None else None,
        "output_bytes": output_bytes,
    }


def measure_child(case: Case, repeats: int) -> dict[str, Any]:
    proc = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "--worker",
            json.dumps(asdict(case)),
            "--repeats",
            str(repeats),
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode:
        raise RuntimeError(f"Benchmark failed for {case}:\n{proc.stdout}{proc.stderr}")
    result: dict[str, Any] = json.loads(proc.stdout)
    return result


def cpu_name() -> str:
    cpuinfo = Path("/proc/cpuinfo")
    if cpuinfo.exists():
        for line in cpuinfo.read_text().splitlines():
            if line.startswith("model name"):
                return line.partition(":")[2].strip()
    return platform.processor() or platform.machine()


def positive_csv(value: str) -> list[float]:
    try:
        values = [float(part) for part in value.split(",")]
        if not values or any(not 0 < part < float("inf") for part in values):
            raise ValueError
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Expected comma-separated positive finite numbers") from exc
    return values


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--sizes-mib", type=positive_csv, default=positive_csv("1,8,128"))
    parser.add_argument("--chunk-kib", type=positive_csv, default=positive_csv("4,64,1024"))
    parser.add_argument(
        "--shapes", default=",".join(SHAPES), help="Comma-separated subset of " + ",".join(SHAPES)
    )
    parser.add_argument("--source", choices=("generated", "file", "both"), default="both")
    parser.add_argument("--repeats", type=int, default=7, help="Timing samples per case, minimum 7")
    parser.add_argument("--build-profile", choices=("release", "debug", "unknown"), default="unknown")
    parser.add_argument("--worker", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.repeats < 7:
        parser.error("--repeats must be at least 7")
    if args.worker:
        print(json.dumps(measure(Case(**json.loads(args.worker)), args.repeats)))
        return
    shapes = args.shapes.split(",")
    if not shapes or any(shape not in SHAPES for shape in shapes):
        parser.error("--shapes must be a comma-separated subset of " + ",".join(SHAPES))
    sizes = [int(size * MIB) for size in args.sizes_mib]
    chunks = [int(chunk * 1024) for chunk in args.chunk_kib]
    if min(sizes) < 256 or min(chunks) < 64:
        parser.error("Sizes must be at least 256 bytes and chunks at least 64 bytes")
    sources = ("generated", "file") if args.source == "both" else (args.source,)

    print("# Streaming projection benchmark\n")
    print(
        f"OS: {platform.platform()}  \nCPU: {cpu_name()}  \n"
        f"Python: {platform.python_implementation()} {platform.python_version()}  \n"
        f"Packages: json-projection {json_projection.__version__}; JSON control: Python stdlib json  \n"
        f"Build profile: {args.build_profile} (operator-declared, not detected from the extension)\n"
    )
    print("## Methodology\n")
    print(
        f"Each table row runs in a fresh child. After imports, plan creation, and allocation of reusable "
        f"source blocks, Linux ru_maxrss growth is sampled across the first call, before validation or "
        f"timing. No complete source is read or assembled before that baseline. RSS is n/a elsewhere; "
        f"zero growth means no increase over the process's earlier high-water mark. Timing is the "
        f"median of {args.repeats} subsequent calls, with generation or file open/read and input assembly "
        f"included; validation and disposal of the returned value are outside the timer. Every output "
        f"is checked for equivalent selected data. The unprojected full-parse control uses json.loads "
        f"before removing unselected keys, and returns a dict; projection approaches return bytes. "
        f"Output sizes are equivalent compact JSON sizes.\n\n"
        f"Generated sources reuse bounded bytes blocks. File fixtures are written lazily once per "
        f"shape/size before trials; all approaches reopen them in binary mode and read at most the "
        f"specified chunk size. Files and repeated reads use the ordinary OS page cache, without "
        f"eviction; this is a warm-cache workload, not a cold-disk measurement. Full-input and "
        f"full-parse assemble the same chunk iterator in BytesIO inside the measured call. "
        f"Mostly-retained documents deliberately show the cost of buffered selected output. "
        f"Use a release build for performance comparisons; build profile is recorded metadata.\n"
    )
    print(
        "| shape | source | input MiB | chunk KiB | approach | median ms | "
        "peak RSS growth MiB | output bytes |"
    )
    print("|---|---|---:|---:|---|---:|---:|---:|", flush=True)
    with tempfile.TemporaryDirectory(prefix="json-projection-stream-") as temp:
        for shape in shapes:
            for size_bytes in sizes:
                path = Path(temp) / "source.json"
                if "file" in sources:
                    with path.open("wb") as destination:
                        for chunk in document(shape, size_bytes, 64 * 1024).chunks():
                            destination.write(chunk)
                for chunk_bytes in chunks:
                    for mode in sources:
                        for approach in APPROACHES:
                            case = Case(
                                shape=shape,
                                size_bytes=size_bytes,
                                chunk_bytes=chunk_bytes,
                                source=mode,
                                approach=approach,
                                path=str(path) if mode == "file" else None,
                            )
                            result = measure_child(case, args.repeats)
                            rss = result["rss_delta_mib"]
                            rss_text = "n/a" if rss is None else f"{rss:.2f}"
                            print(
                                f"| {shape} | {mode} | {size_bytes / MIB:g} | {chunk_bytes / 1024:g} | "
                                f"{approach} | {result['wall_ms']:.3f} | {rss_text} | "
                                f"{result['output_bytes']:,} |",
                                flush=True,
                            )


if __name__ == "__main__":
    main()
