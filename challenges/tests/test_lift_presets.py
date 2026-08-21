"""Guard tests pinning the lift-preset constants to the seeded catalogues.

These fail in CI if a fixture rename drifts the presets out of sync with the
seeded lift names (TASK-138). CLASSIC_LIFT_NAMES no longer has a fixture of
its own to cross-check against (TASK-248 deleted the built-in strength-
standards app and its data) -- it is now only checked against the still-live
Liftosaur lift catalogue, the same as IPF_LIFT_NAMES.
"""

import json
from pathlib import Path

from django.conf import settings

from challenges.lift_presets import (
    CALISTHENICS_LIFT_NAMES,
    CLASSIC_LIFT_NAMES,
    IPF_LIFT_NAMES,
)

BASE_DIR = Path(settings.BASE_DIR)


def _liftosaur_lift_names():
    path = BASE_DIR / "liftosaur" / "fixtures" / "liftosaur_lifts.json"
    data = json.loads(path.read_text())
    return {lift["name"] for lift in data["lifts"]}


def test_classic_names_are_seeded_liftosaur_lifts():
    assert _liftosaur_lift_names() >= CLASSIC_LIFT_NAMES


def test_ipf_names_are_seeded_liftosaur_lifts():
    assert _liftosaur_lift_names() >= IPF_LIFT_NAMES


def test_calisthenics_names_are_seeded_liftosaur_lifts():
    assert _liftosaur_lift_names() >= CALISTHENICS_LIFT_NAMES
