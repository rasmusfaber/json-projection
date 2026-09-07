"""Round-trip real inspect_ai logs through the example adapters. Skipped unless inspect_ai is installed.

inspect_ai regenerates `EvalSpec.eval_id` and message ids on every parse unless a "deserializing" context
is passed, so both parses below use it to get a meaningful diff instead of always-fresh ids; it comes from
a private inspect_ai module, so it may move without notice.

Run locally with:  uv run --no-sync --with inspect_ai pytest -q tests/test_inspect_ai.py
"""

import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("inspect_ai")

try:
    from inspect_ai._util.constants import get_deserializing_context  # type: ignore  # noqa: E402
except ImportError:  # the read-context helper is private to inspect_ai and may move
    pytest.skip("inspect_ai's deserializing-context helper is unavailable", allow_module_level=True)

from inspect_ai.log import EvalLog, EvalSample, EvalSpec  # type: ignore  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))
from inspect_adapters import BULK_FIELDS, INSPECT_ADAPTERS, thin_eval_log  # type: ignore  # noqa: E402

from json_projection.pydantic import Projected, normalize_exclude  # noqa: E402
from migration_models import retained_dump  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "inspect_legacy_log.json"
EXCLUDE = {EvalSample: BULK_FIELDS}


def _both(raw: bytes) -> tuple[EvalLog, EvalLog]:
    # inspect_ai's own log readers pass this context so that ids it otherwise generates fresh
    # (EvalSpec.eval_id, ChatMessage.id) come out stable across repeated deserialization; without it,
    # even two plain parses of the same bytes disagree on those ids.
    ctx = get_deserializing_context()
    plain = EvalLog.model_validate_json(raw, context=ctx)
    thin = Projected(EvalLog, EXCLUDE, projection_adapters=INSPECT_ADAPTERS).validate_json(raw, context=ctx)
    norm = normalize_exclude(EvalLog, EXCLUDE)
    assert retained_dump(thin, norm) == retained_dump(plain, norm)
    return plain, thin


def test_a_legacy_log_loads_with_the_bulk_fields_excluded():
    plain, thin = _both(FIXTURE.read_bytes())
    assert plain.reductions and thin.reductions == plain.reductions  # migrated from results.sample_reductions
    assert plain.samples[0].events and thin.samples[0].events == []  # transcript's events were never parsed
    assert thin.eval.task_args_passed == plain.eval.task_args_passed  # fallback from task_args survived


def test_a_current_format_log_round_trips():
    current = EvalLog.model_validate_json(FIXTURE.read_bytes()).model_dump_json().encode()
    _both(current)


def test_the_spec_fallback_survives_excluding_task_args():
    """Excluding `task_args` must not lose `task_args_passed`, which the migration derives from it."""
    doc = json.loads(FIXTURE.read_bytes())
    doc["eval"]["task_args"] = {"n": 3, "flag": True}  # the fixture's own task_args is empty
    raw = json.dumps(doc).encode()
    exclude = {EvalSample: BULK_FIELDS, EvalSpec: {"task_args"}}
    ctx = get_deserializing_context()
    plain = EvalLog.model_validate_json(raw, context=ctx)
    thin = Projected(EvalLog, exclude, projection_adapters=INSPECT_ADAPTERS).validate_json(raw, context=ctx)
    assert plain.eval.task_args_passed == {"n": 3, "flag": True}
    assert thin.eval.task_args_passed == plain.eval.task_args_passed
    assert thin.eval.task_args == {}  # excluded: default applied, though the key was kept for the migration
    norm = normalize_exclude(EvalLog, exclude)
    assert retained_dump(thin, norm) == retained_dump(plain, norm)


def test_excluding_the_results_is_refused_while_samples_are_retained():
    """`populate_scorer_name_for_samples` renames a legacy score from `results.scores[0].name`."""
    exclude = {EvalSample: BULK_FIELDS, EvalLog: {"results"}}
    with pytest.raises(ValueError, match="retaining 'samples' requires 'results'"):
        Projected(EvalLog, exclude, projection_adapters=INSPECT_ADAPTERS)


def test_an_explicit_empty_exclusion_excludes_nothing():
    assert thin_eval_log({}).exclude == {}
    assert thin_eval_log().exclude == {EvalSample: BULK_FIELDS}
