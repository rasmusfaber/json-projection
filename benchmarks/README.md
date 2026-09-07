# Benchmarks

`bench.py` compares, per document shape, plain `Model.model_validate_json` (naive), the schema-projection
validator alone (helper: skips validation of excluded fields but still parses everything), and the schema
projection combined with byte projection (projector). Peak RSS is measured in a fresh process from the
kernel high-water mark because the discarded tree lives in Rust, where tracemalloc cannot see it.

Run by hand; not part of CI:

    uv run maturin develop --uv --release
    uv run python benchmarks/bench.py > benchmarks/results.md

`stream.py` compares whole-buffer projection, streaming projection, and a full-parse control using
lazily generated discarded strings, arrays, fractional numbers, and a mostly retained document.
It reports timing medians and peak RSS growth from fresh processes, including input allocation.
Use `--source file` to compare reads of a generated binary file with the same cache policy:

    uv run maturin develop --uv --release
    uv run --no-sync python benchmarks/stream.py --sizes-mib 1,8,128 --chunk-kib 4,64,1024 --build-profile release
    uv run --no-sync python benchmarks/stream.py --sizes-mib 8 --chunk-kib 64 --source file --build-profile release

Recorded measurements and environment are in [stream-results.md](stream-results.md). These are manual
benchmarks, not CI timing thresholds; the test suite separately checks memory growth with increasing
discarded input size on Linux.
