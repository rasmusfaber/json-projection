"""Projection adapters for inspect_ai's EvalLog: load logs without parsing the per-sample bulk fields.

inspect_ai migrates old log formats in `model_validator(mode='before')` methods, and resolves timeline
references in a wrap validator. Those validators read keys the current schema does not declare, so the
adapters below tell json-projection which JSON inputs each retained field may need:

    EvalSample.scores       <- legacy `score`
    EvalSample.events       <- legacy `transcript.events`
    EvalSample.attachments  <- legacy `transcript.content`
    EvalSample.timelines    requires EvalSample.events (resolved by uuid)
    EvalLog.reductions      <- legacy `results.sample_reductions`
    EvalSpec.*_args_passed  <- fallback `task_args` / `solver_args`

A legacy `sandbox` array under `EvalSpec`/`EvalSample` needs nothing: it sits under a declared key, and a kept
value whose shape does not match its sub-spec is copied raw. This module is documentation, not part of the
json-projection package; it needs `inspect_ai` installed.
"""

from __future__ import annotations

from inspect_ai.log import EvalLog, EvalSample, EvalSpec

from json_projection.pydantic import Exclude, Projected, migration_adapter

SAMPLE_ADAPTER = migration_adapter(
    inputs={
        "scores": ["score"],
        "events": [("transcript", "events")],
        "attachments": [("transcript", "content")],
    },
    requires={"timelines": ["events"]},
)
LOG_ADAPTER = migration_adapter(inputs={"reductions": [("results", "sample_reductions")]})
SPEC_ADAPTER = migration_adapter(
    inputs={"task_args_passed": ["task_args"], "solver_args_passed": ["solver_args"]},
)
INSPECT_ADAPTERS = {EvalSample: SAMPLE_ADAPTER, EvalLog: LOG_ADAPTER, EvalSpec: SPEC_ADAPTER}

BULK_FIELDS = frozenset({"events", "messages", "store", "attachments", "timelines"})
"""The per-sample fields that hold most of a log's bytes.

`timelines` is included alongside `events`: `SAMPLE_ADAPTER` declares that retaining `timelines` requires
`events` (its wrap validator resolves timeline references against the parsed events by uuid), so excluding
one without the other is rejected at construction.
"""


def thin_eval_log(exclude: Exclude | None = None) -> Projected:
    """`Projected` for `EvalLog`; by default the per-sample bulk fields are never parsed."""
    return Projected(EvalLog, exclude or {EvalSample: BULK_FIELDS}, projection_adapters=INSPECT_ADAPTERS)
