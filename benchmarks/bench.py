"""Manual benchmark: naive pydantic vs schema projection vs schema projection + byte projection.

Usage: uv run python benchmarks/bench.py [--shapes S1,S2] > benchmarks/results-<date>.md
Not run in CI. Numbers depend on the machine; report medians and the environment.
"""

from __future__ import annotations

import argparse
import platform
import statistics
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import gen_shapes  # noqa: E402

import json_projection  # noqa: E402
from json_projection import Projection  # noqa: E402
from json_projection.pydantic import projected_validator  # noqa: E402


def approaches(shape):
    """Return {approach_name: callable}, or None for an approach that doesn't apply to this shape."""
    raw, model, exclude, spec = shape.raw, shape.model, shape.exclude, shape.spec
    proj = Projection(spec)

    def naive():
        return model.model_validate_json(raw)

    if exclude:
        # `assume_projected=True` for the projector: the byte projection already strips the excluded
        # keys, so the validator doesn't need to guard against extra='forbid'/'allow' etc. The plain
        # (helper) validator has no such guarantee and can be refused; if so, its row is n/a.
        projector_validator = projected_validator(model, exclude, assume_projected=True)
        try:
            helper_validator = projected_validator(model, exclude)
        except ValueError:
            helper = None
        else:

            def helper():
                return helper_validator.validate_json(raw)
    else:
        projector_validator = model.__pydantic_validator__

        def helper():
            return projector_validator.validate_json(raw)

    def projector():
        return projector_validator.validate_json(proj(raw))

    return {"naive": naive, "helper": helper, "projector": projector}


def time_call(fn, target_s=1.0):
    for _ in range(3):
        fn()
    t0 = time.perf_counter()
    fn()
    n = max(20, int(target_s / max(time.perf_counter() - t0, 1e-9)))
    medians = []
    for _ in range(5):
        t0 = time.perf_counter()
        for _ in range(n):
            fn()
        medians.append((time.perf_counter() - t0) / n * 1000)
    return statistics.median(medians)


def peak_rss_mb(shape_name: str, approach: str) -> float:
    """Peak RSS growth of one call in a fresh process (median of 5)."""
    code = (
        "import sys, gc, ctypes, json; sys.path.insert(0, 'benchmarks'); import gen_shapes, bench\n"
        f"fn = bench.approaches(gen_shapes.SHAPES['{shape_name}']())['{approach}']\n"
        "fn(); gc.collect(); ctypes.CDLL('libc.so.6').malloc_trim(0)\n"
        "open('/proc/self/clear_refs', 'w').write('5')\n"
        "hwm = lambda: int(next(l for l in open('/proc/self/status') if l.startswith('VmHWM'))"
        ".split()[1]) / 1024\n"
        "before = hwm(); fn(); print(hwm() - before)\n"
    )
    samples = [float(subprocess.check_output([sys.executable, "-c", code], text=True)) for _ in range(5)]
    return statistics.median(samples)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--shapes", default=",".join(gen_shapes.SHAPES))
    args = ap.parse_args()
    import pydantic
    import pydantic_core

    print(
        f"# json-projection benchmark\n\n{platform.platform()} | Python {platform.python_version()} | "
        f"pydantic {pydantic.VERSION} | pydantic-core {pydantic_core.__version__} | "
        f"json-projection {json_projection.__version__}\n"
    )
    print("| shape | bytes | approach | wall ms | peak RSS MiB | x vs naive |\n|---|---:|---|---:|---:|---:|")
    for name in args.shapes.split(","):
        shape = gen_shapes.SHAPES[name]()
        fns = approaches(shape)
        base = None
        for approach, fn in fns.items():
            if fn is None:
                print(f"| {name} | {len(shape.raw):,} | {approach} | n/a | n/a | n/a |")
                continue
            ms = time_call(fn)
            rss = peak_rss_mb(name, approach) if sys.platform == "linux" else float("nan")
            base = base or ms
            print(f"| {name} | {len(shape.raw):,} | {approach} | {ms:.3f} | {rss:.2f} | {base / ms:.2f}x |")


if __name__ == "__main__":
    main()
