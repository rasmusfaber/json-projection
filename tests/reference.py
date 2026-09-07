"""Pure-Python reference for json_projection, used to check the Rust implementation."""

from __future__ import annotations

import json
import random
from typing import Any


def reference_project(obj: Any, spec: Any) -> Any:
    """Apply a keep-spec to an already-parsed JSON object (no duplicate keys)."""
    if spec is True:
        return obj
    if isinstance(spec, dict):
        if set(spec) == {"__all__"}:
            return [reference_project(x, spec["__all__"]) for x in obj] if isinstance(obj, list) else obj
        if not isinstance(obj, dict):
            return obj
        return {k: reference_project(v, spec[k]) for k, v in obj.items() if k in spec}
    keep = set(spec)  # iterable of root keys
    return {k: v for k, v in obj.items() if k in keep}


_WORDS = ["a", "b", "id", "name", "items", "meta", "é", "日本", "x,y", 'q"q', "", "with space", "\\u0041"]


def _value(rng: random.Random, depth: int) -> Any:
    r = rng.random()
    if depth > 3 or r < 0.35:
        return rng.choice([None, True, False, 0, -1, 1.5, 1e300, float("nan"), float("inf"), "s", "é\n\t", "x" * 50])
    if r < 0.65:
        return [_value(rng, depth + 1) for _ in range(rng.randint(0, 4))]
    return {rng.choice(_WORDS): _value(rng, depth + 1) for _ in range(rng.randint(0, 5))}


def _spec_for(rng: random.Random, obj: Any, depth: int) -> Any:
    if not isinstance(obj, dict) or depth > 3 or rng.random() < 0.3:
        return True
    spec: dict[str, Any] = {}
    for k, v in obj.items():
        if rng.random() < 0.6:
            if isinstance(v, list) and v and isinstance(v[0], dict) and rng.random() < 0.7:
                spec[k] = {"__all__": _spec_for(rng, v[0], depth + 1)}
            else:
                spec[k] = _spec_for(rng, v, depth + 1)
    return spec


def corpus(seed: int, n: int) -> list[tuple[bytes, Any]]:
    """Documents (as bytes, keys unique per object because they come from a dict) with a matching spec."""
    rng = random.Random(seed)
    out = []
    for _ in range(n):
        obj = {rng.choice(_WORDS): _value(rng, 0) for _ in range(rng.randint(0, 6))}
        spec = _spec_for(rng, obj, 0)
        if spec is True:
            spec = set(rng.sample(list(obj), k=rng.randint(0, len(obj)))) if obj else set()
        indent = rng.choice([None, None, 2])
        out.append((json.dumps(obj, indent=indent, ensure_ascii=rng.random() < 0.5).encode(), spec))
    return out
