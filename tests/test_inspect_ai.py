"""Round-trip real inspect_ai logs through the example adapters. Skipped unless inspect_ai is installed.

inspect_ai regenerates `EvalSpec.eval_id` and message ids on every parse unless a "deserializing" context
is passed, so both parses below use it to get a meaningful diff instead of always-fresh ids; it comes from
a private inspect_ai module, so it may move without notice.

Run locally with:  uv run --no-sync --with inspect_ai pytest -q tests/test_inspect_ai.py
"""

import sys
from pathlib import Path

import pytest

pytest.importorskip("inspect_ai")

try:
    from inspect_ai._util.constants import get_deserializing_context  # type: ignore  # noqa: E402
except ImportError:  # the read-context helper is private to inspect_ai and may move
    pytest.skip("inspect_ai's deserializing-context helper is unavailable", allow_module_level=True)

from inspect_ai.log import EvalLog, EvalSample  # type: ignore  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))
from inspect_adapters import BULK_FIELDS, INSPECT_ADAPTERS  # type: ignore  # noqa: E402

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
