"""The correctness statement for adapters: plain and projected validation agree on every retained field."""

import json
import random

import pytest
from pydantic import ValidationError

from json_projection.pydantic import Projected
from migration_models import ADAPTERS, Item, Log, Sample, document, retained_dump

CANDIDATES = [
    (Sample, "events"),
    (Sample, "attachments"),
    (Sample, "store"),
    (Sample, "timelines"),
    (Sample, "scores"),
    (Item, "blob"),
    (Log, "debug"),
    (Log, "name"),
    (Log, "version"),
]


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_projected_validation_matches_plain_validation_on_retained_fields(seed: int):
    rng = random.Random(seed)
    checked = rejected = skipped = 0
    for _ in range(400):
        excluded: dict[type, set[str]] = {}
        for cls, name in CANDIDATES:
            if rng.random() < 0.35:
                excluded.setdefault(cls, set()).add(name)
        try:
            thin = Projected(Log, excluded, projection_adapters=ADAPTERS)
        except ValueError:
            # the only declared dependency: timelines need events
            sample = excluded.get(Sample, set())
            assert "events" in sample and "timelines" not in sample, excluded
            rejected += 1
            continue
        doc = json.dumps(document(rng)).encode()
        try:
            plain = Log.model_validate_json(doc)
        except ValidationError:
            skipped += 1  # a document ordinary validation rejects says nothing about the projection
            continue
        norm = {cls: frozenset(names) for cls, names in excluded.items()}
        assert retained_dump(thin.validate_json(doc), norm) == retained_dump(plain, norm), (excluded, doc)
        checked += 1
    assert checked >= 200 and rejected >= 10 and skipped >= 1, (checked, rejected, skipped)
