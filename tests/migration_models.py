"""Synthetic models with migration validators, mirroring the shapes real logs have.

`Sample` migrates a legacy single `score` into `scores`, unnests a legacy `transcript` into `events` and
`attachments` (an empty transcript overwrites both with defaults, as the real one does), rejects `score`
next to `scores`, and resolves `timelines` against `events` in a wrap validator. `Log` dispatches on
`version` and renames a legacy `title` to `name`.
"""

from __future__ import annotations

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
