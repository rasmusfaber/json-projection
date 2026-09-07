"""Projection adapters for inspect_ai's EvalLog: load logs without parsing the per-sample bulk fields.

inspect_ai migrates old log formats in `model_validator(mode='before')` methods, and resolves timeline
references in a wrap validator. Those validators read keys the current schema does not declare, so the
adapters below tell json-projection which JSON inputs each retained field may need:

    EvalSample.scores       <- legacy `score`
    EvalSample.events       <- legacy `transcript.events`
    EvalSample.attachments  <- legacy `transcript.content`
    EvalSample.timelines    requires EvalSample.events (resolved by uuid)
    EvalLog.reductions      <- legacy `results.sample_reductions`
    EvalLog.samples         requires EvalLog.results (legacy score names come from `results.scores[0].name`)
    EvalLog.tags            requires EvalLog.log_updates (edits are replayed onto the eval-time tags)
    EvalLog.metadata        requires EvalLog.log_updates (same replay)
    EvalSpec.*_args_passed  <- fallback `task_args` / `solver_args`

A legacy `sandbox` array under `EvalSpec`/`EvalSample` needs nothing: it sits under a declared key, and a kept
value whose shape does not match its sub-spec is copied raw.

One dependency here cannot be expressed and is therefore unsupported:
`EvalLog.populate_scorer_name_for_samples` reads `results.scores[0].name` off the *validated*
`EvalResults`, so excluding `EvalResults.scores` -- rather than `EvalLog.results`, which the `requires`
above does catch -- silently renames the placeholder score of a legacy sample. `requires` relates fields
of one class; it cannot say "EvalSample.scores needs EvalResults.scores". Do not exclude anything
under `EvalResults` while retaining samples. `recompute_tags_and_metadata` has the same shape of
cross-class dependency on `EvalSpec.tags` and `EvalSpec.metadata`, which it replays the edits onto, so
do not exclude those either while retaining `EvalLog.tags` or `EvalLog.metadata`.

`EvalLog.config_updates` is declared but nothing reads it during validation (only
`recompute_tags_and_metadata` consumes an updates list, and it reads `log_updates`), so it needs no
`requires`.

This module is documentation, not part of the json-projection package; it needs `inspect_ai` installed.
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
LOG_ADAPTER = migration_adapter(
    inputs={"reductions": [("results", "sample_reductions")]},
    requires={
        # `populate_scorer_name_for_samples` renames a legacy sample's placeholder score from
        # `results.scores[0].name`, so retaining samples without results would silently change score keys
        "samples": ["results"],
        # `_validate_tags_and_metadata` recomputes both from the eval-time values plus the edits in
        # `log_updates`, so dropping the updates silently reverts every edit ever made to a log
        "tags": ["log_updates"],
        "metadata": ["log_updates"],
    },
)
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
    """`Projected` for `EvalLog`; by default the per-sample bulk fields are never parsed.

    An explicit empty `exclude` means exclude nothing; only `None` selects the default.
    """
    if exclude is None:
        exclude = {EvalSample: BULK_FIELDS}
    return Projected(EvalLog, exclude, projection_adapters=INSPECT_ADAPTERS)
