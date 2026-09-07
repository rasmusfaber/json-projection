"""Synthetic models with migration validators, mirroring the shapes real logs have.

`Sample` migrates a legacy single `score` into `scores`, unnests a legacy `transcript` into `events` and
`attachments` (an empty transcript overwrites both with defaults, as the real one does), rejects `score`
next to `scores`, and resolves `timelines` against `events` in a wrap validator. `Log` dispatches on
`version` and renames a legacy `title` to `name`.
"""

from __future__ import annotations

import math
import random
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, model_validator

from json_projection.pydantic import migration_adapter


class Item(BaseModel):
    id: int
    blob: dict[str, Any] = {}


class Timeline(BaseModel):
    name: str
    event_ids: list[int]


class Sample(BaseModel):
    id: int
    scores: dict[str, float] = {}
    events: list[Item] = []
    attachments: dict[str, str] = {}
    timelines: list[Timeline] = []
    store: dict[str, Any] = {}

    @model_validator(mode="before")
    @classmethod
    def migrate(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        data = dict(data)
        if "score" in data:
            if "scores" in data:
                raise ValueError("unexpected `scores` next to legacy `score`")
            data["scores"] = {"legacy": data.pop("score")}
        if "transcript" in data:
            transcript = data.pop("transcript")
            data["events"] = transcript.get("events", [])
            data["attachments"] = transcript.get("content", {})
        return data

    @model_validator(mode="wrap")
    @classmethod
    def resolve_timelines(cls, data: Any, handler: Any) -> Any:
        raw = data.pop("timelines", None) if isinstance(data, dict) else None
        sample = handler(data)
        if raw:
            known = {event.id for event in sample.events}
            timelines = [Timeline.model_validate(t) for t in raw]
            if any(i not in known for t in timelines for i in t.event_ids):
                raise ValueError("a timeline references an unknown event")
            sample.timelines = timelines
        return sample


class Log(BaseModel):
    version: int
    name: str
    samples: list[Sample] = []
    debug: dict[str, Any] = {}

    @model_validator(mode="before")
    @classmethod
    def migrate(cls, data: Any) -> Any:
        if isinstance(data, dict) and data.get("version") == 1 and "title" in data:
            data = dict(data)
            data["name"] = data.pop("title")
        return data


SAMPLE_ADAPTER = migration_adapter(
    inputs={
        "scores": ["score"],
        "events": [("transcript", "events")],
        "attachments": [("transcript", "content")],
    },
    requires={"timelines": ["events"]},
)
LOG_ADAPTER = migration_adapter(inputs={"name": ["title"]}, controls=["version"])
ADAPTERS = {Sample: SAMPLE_ADAPTER, Log: LOG_ADAPTER}


def _item(rng: random.Random, i: int) -> dict[str, Any]:
    blob = {"n": rng.random(), "nan": float("nan")} if rng.random() < 0.3 else {}
    return {"id": i, "blob": blob}


def _sample(rng: random.Random, sid: int) -> dict[str, Any]:
    events = [_item(rng, i) for i in range(rng.randint(0, 3))]
    content = {f"k{i}": "v" for i in range(rng.randint(0, 2))}
    doc: dict[str, Any] = {"id": sid}
    if rng.random() < 0.5:
        doc["store"] = {"blob": [rng.random() for _ in range(5)]}
    if rng.random() < 0.5:
        doc["score"] = rng.random()  # legacy single score
    elif rng.random() < 0.7:
        doc["scores"] = {"acc": rng.random()}
    event_ids: list[int] = []
    shape = rng.random()
    if shape < 0.4:  # legacy transcript, possibly partial or empty
        transcript: dict[str, Any] = {}
        if rng.random() < 0.8:
            transcript["events"] = events
        if rng.random() < 0.8:
            transcript["content"] = content
        doc["transcript"] = transcript
        event_ids = [e["id"] for e in transcript.get("events", [])]
    elif shape < 0.9:  # current format
        doc["events"] = events
        doc["attachments"] = content
        event_ids = [e["id"] for e in events]
    if event_ids and rng.random() < 0.5:
        doc["timelines"] = [{"name": "t", "event_ids": rng.sample(event_ids, rng.randint(1, len(event_ids)))}]
    return doc


def document(rng: random.Random) -> dict[str, Any]:
    """A log in legacy (version 1, `title`) or current (version 2, `name`) format."""
    doc: dict[str, Any] = {"samples": [_sample(rng, i) for i in range(rng.randint(0, 3))]}
    if rng.random() < 0.5:
        doc.update(version=1, title="legacy")
    else:
        doc.update(version=2, name="current")
    if rng.random() < 0.5:
        doc["debug"] = {"big": list(range(20))}
    return doc


def retained_dump(value: Any, excluded: Mapping[type, frozenset[str]]) -> Any:
    """Dump `value` without the excluded fields, recursing into models, lists and dicts; NaN compares
    equal.
    """
    if isinstance(value, BaseModel):
        cls = type(value)
        return {
            name: retained_dump(getattr(value, name), excluded)
            for name in cls.model_fields
            if name not in excluded.get(cls, ()) and hasattr(value, name)
        }
    if isinstance(value, dict):
        return {k: retained_dump(v, excluded) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [retained_dump(v, excluded) for v in value]
    if isinstance(value, float) and math.isnan(value):
        return "NaN"
    return value
