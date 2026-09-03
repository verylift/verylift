"""Tests for the seed_lifts command and DB-backed lift-quality lookups.

Split out of liftosaur/tests/test_seed_liftosaur_lifts.py when the register
moved to core (TASK-347, originally TASK-89). The Liftosaur alias half of
that file now lives in liftosaur/tests/test_seed_liftosaur_lift_aliases.py.
"""

import json

import pytest
from django.core.management import call_command

from core.management.commands.seed_lifts import FIXTURE_PATH
from core.models import Lift
from scoring.domain.calculator import is_bodyweight_added_lift


def _fixture_data():
    with open(FIXTURE_PATH) as f:
        return json.load(f)


# The fixture carries the canonical lift catalogue (~139 lifts, originally
# derived from liftosaur.com/exercises) — asserting against it directly (rather
# than duplicating the list here) keeps these tests from drifting out of sync
# with the fixture.
EXPECTED_LIFTS = {row["name"] for row in _fixture_data()["lifts"]}
EXPECTED_BODYWEIGHT_ADDED = {
    row["name"] for row in _fixture_data()["lifts"] if row["is_bodyweight_added"]
}


@pytest.mark.django_db
class TestSeedLiftsCommand:
    def test_seeds_exactly_the_fixture_catalogue(self):
        """Every fixture lift becomes a row, and nothing else does.

        Equality (not containment) is the point: alias raw names like
        "Barbell Row" -> Pendlay Row must NOT be seeded as Lift rows of their
        own, or they would shadow the canonical lift during resolution.
        """
        call_command("seed_lifts")
        assert set(Lift.objects.values_list("name", flat=True)) == EXPECTED_LIFTS

    def test_seeds_bodyweight_added_quality(self):
        call_command("seed_lifts")
        tagged = set(
            Lift.objects.filter(is_bodyweight_added=True).values_list("name", flat=True)
        )
        assert tagged == EXPECTED_BODYWEIGHT_ADDED

    def test_command_is_idempotent(self):
        call_command("seed_lifts")
        call_command("seed_lifts")
        assert Lift.objects.count() == len(EXPECTED_LIFTS)

    def test_reseeding_restores_edited_rows(self):
        """update_or_create means a re-run reconciles rows back to the fixture."""
        call_command("seed_lifts")
        Lift.objects.filter(name="Pull-up").update(is_bodyweight_added=False)

        call_command("seed_lifts")

        assert Lift.objects.get(name="Pull-up").is_bodyweight_added is True


@pytest.mark.django_db
class TestDbBackedQualityLookups:
    """The old module-level constants are gone; lookups resolve via the DB.

    The session conftest seeds the fixture, so these run against the same
    reference data a deployed instance has.
    """

    def test_bodyweight_added_quality_from_db(self):
        assert is_bodyweight_added_lift("Pull-up")
        assert not is_bodyweight_added_lift("Back Squat")

    def test_bodyweight_added_quality_follows_admin_edits(self):
        Lift.objects.filter(name="Dip").update(is_bodyweight_added=False)
        assert not is_bodyweight_added_lift("Dip")
