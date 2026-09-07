# Benchmarks

`bench.py` compares, per document shape, plain `Model.model_validate_json` (naive), the schema-projection
validator alone (helper: skips validation of excluded fields but still parses everything), and the schema
projection combined with byte projection (projector). Peak RSS is measured in a fresh process from the
kernel high-water mark because the discarded tree lives in Rust, where tracemalloc cannot see it.

Run by hand; not part of CI:

    uv run maturin develop --uv --release
    uv run python benchmarks/bench.py > benchmarks/results.md
