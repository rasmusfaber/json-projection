"""Pure-Python reference for json_projection, used to check the Rust implementation."""

from __future__ import annotations

import json
import random
from collections.abc import Iterator
from typing import Any


class Obj(list[tuple[str, Any]]):
    """A JSON object as its (key, value) pairs, so duplicate members survive the parse."""


def parse(raw: bytes | str) -> Any:
    """Parse `raw`, keeping every duplicate object member in document order."""
    return json.loads(raw, object_pairs_hook=Obj)


def _form(node: Any) -> Any:
    if isinstance(node, Obj):  # before list: Obj is one
        return {"obj": [[k, _form(v)] for k, v in node]}
    if isinstance(node, list):
        return {"arr": [_form(v) for v in node]}
    return node


def canon(node: Any) -> str:
    """A comparable string for a `parse` result: objects and arrays cannot be confused, duplicates and
    member order are significant, and NaN/Infinity become literals so they compare equal."""
    return json.dumps(_form(node))


def reference_project(obj: Any, spec: Any) -> Any:
    """Apply a keep-spec to a document parsed by `parse`."""
    if spec is True:
        return obj
    if isinstance(spec, dict):
        if set(spec) == {"__all__"}:
            if isinstance(obj, Obj) or not isinstance(obj, list):
                return obj
            return [reference_project(x, spec["__all__"]) for x in obj]
        if not isinstance(obj, Obj):
            return obj
        return Obj((k, reference_project(v, spec[k])) for k, v in obj if k in spec)
    keep = set(spec)  # iterable of root keys
    return Obj((k, v) for k, v in obj if k in keep)


_WORDS = ["a", "b", "id", "name", "items", "meta", "é", "日本", "x,y", 'q"q', "", "with space", "\\u0041"]
_DUP = "\x00dup"  # placeholder key, rewritten to a sibling's key to produce a duplicate member


def _object(rng: random.Random, depth: int) -> dict[str, Any]:
    return {rng.choice(_WORDS): _value(rng, depth + 1) for _ in range(rng.randint(0, 5))}


def _value(rng: random.Random, depth: int) -> Any:
    r = rng.random()
    if depth > 3 or r < 0.35:
        return rng.choice(
            [None, True, False, 0, -1, 1.5, 1e300, float("nan"), float("inf"), "s", "é\n\t", "x" * 50]
        )
    if r < 0.65:
        if r < 0.45:  # array of arrays of objects: needs {"__all__": {"__all__": ...}}
            return [[_object(rng, depth + 2) for _ in range(rng.randint(1, 3))] for _ in range(2)]
        return [_value(rng, depth + 1) for _ in range(rng.randint(0, 4))]
    return _object(rng, depth)


def _spec_for(rng: random.Random, obj: Any, depth: int) -> Any:
    if not isinstance(obj, dict) or depth > 3 or rng.random() < 0.3:
        return True
    spec: dict[str, Any] = {}
    for k, v in obj.items():
        if rng.random() < 0.6:
            head = v[0] if isinstance(v, list) and v else None
            if isinstance(head, dict) and rng.random() < 0.7:
                spec[k] = {"__all__": _spec_for(rng, head, depth + 1)}
            elif isinstance(head, list) and head and isinstance(head[0], dict) and rng.random() < 0.7:
                spec[k] = {"__all__": {"__all__": _spec_for(rng, head[0], depth + 1)}}
            else:
                spec[k] = _spec_for(rng, v, depth + 1)
    return spec


def _objects(node: Any) -> Iterator[dict[str, Any]]:
    """Every non-empty object in `node`."""
    if isinstance(node, dict):
        if node:
            yield node
        for v in node.values():
            yield from _objects(v)
    elif isinstance(node, list):
        for v in node:
            yield from _objects(v)


def corpus(seed: int, n: int) -> list[tuple[bytes, Any]]:
    """Documents (as bytes, some with a duplicate member) with a matching spec."""
    rng = random.Random(seed)
    out = []
    for _ in range(n):
        obj = {rng.choice(_WORDS): _value(rng, 0) for _ in range(rng.randint(0, 6))}
        spec = _spec_for(rng, obj, 0)
        if spec is True:
            spec = set(rng.sample(list(obj), k=rng.randint(0, len(obj)))) if obj else set()
        targets = list(_objects(obj))
        # a duplicate key cannot come out of a dict, so plant a placeholder and rewrite it in the text
        target = rng.choice(targets) if targets and rng.random() < 0.3 else None
        dup = None
        if target is not None:
            dup = rng.choice(list(target))
            target[_DUP] = _value(rng, 2)
        ascii_only = rng.random() < 0.5
        text = json.dumps(obj, indent=rng.choice([None, None, 2]), ensure_ascii=ascii_only)
        if dup is not None:
            text = text.replace(
                json.dumps(_DUP, ensure_ascii=ascii_only), json.dumps(dup, ensure_ascii=ascii_only), 1
            )
        out.append((text.encode(), spec))
    return out
