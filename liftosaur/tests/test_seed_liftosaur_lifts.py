"""Tests for the seed_liftosaur_lifts command and DB-backed lift lookups (TASK-89)."""

import json

import pytest
from django.core.management import call_command

from liftosaur.management.commands.seed_liftosaur_lifts import FIXTURE_PATH
from liftosaur.models import Lift, LiftAlias
from liftosaur.services import canonical_lift_name, liftosaur_builtin_lift_names
from scoring.domain.calculator import is_bodyweight_added_lift


def _fixture_data():
    with open(FIXTURE_PATH) as f:
        return json.load(f)


# The fixture carries Liftosaur's full built-in exercise catalogue
# (liftosaur.com/exercises, ~139 lifts) — asserting against it directly (rather
# than duplicating the list here) keeps these tests from drifting out of sync
# with the fixture.
EXPECTED_BUILTIN_LIFTS = {row["name"] for row in _fixture_data()["lifts"]}
EXPECTED_ALIASES = {
    row["from_name"]: row["to_name"] for row in _fixture_data()["aliases"]
}
EXPECTED_BODYWEIGHT_ADDED = {"Pull-up", "Chin-up", "Dip"}


@pytest.mark.django_db
class TestSeedLiftosaurLiftsCommand:
    def test_seeds_all_builtin_lifts(self):
        call_command("seed_liftosaur_lifts")
        assert Lift.builtin_names() == frozenset(EXPECTED_BUILTIN_LIFTS)

    def test_seeds_all_aliases(self):
        call_command("seed_liftosaur_lifts")
        assert dict(LiftAlias.objects.values_list("from_name", "to_name")) == (
            EXPECTED_ALIASES
        )

    def test_seeds_bodyweight_added_quality(self):
        call_command("seed_liftosaur_lifts")
        tagged = set(
            Lift.objects.filter(is_bodyweight_added=True).values_list("name", flat=True)
        )
        assert tagged == EXPECTED_BODYWEIGHT_ADDED

    def test_command_is_idempotent(self):
        call_command("seed_liftosaur_lifts")
        call_command("seed_liftosaur_lifts")
        assert Lift.objects.count() == len(EXPECTED_BUILTIN_LIFTS)
        assert LiftAlias.objects.count() == len(EXPECTED_ALIASES)

    def test_reseeding_restores_edited_rows(self):
        """update_or_create means a re-run reconciles rows back to the fixture."""
        call_command("seed_liftosaur_lifts")
        Lift.objects.filter(name="Pull-up").update(is_bodyweight_added=False)
        LiftAlias.objects.filter(from_name="Squat").update(to_name="Front Squat")

        call_command("seed_liftosaur_lifts")

        assert Lift.objects.get(name="Pull-up").is_bodyweight_added is True
        assert LiftAlias.objects.get(from_name="Squat").to_name == "Back Squat"


@pytest.mark.django_db
class TestDbBackedLookups:
    """The old module-level constants are gone; lookups resolve via the DB.

    The session conftest seeds the fixture, so these run against the same
    reference data a deployed instance has.
    """

    def test_canonical_lift_name_resolves_alias_from_db(self):
        assert canonical_lift_name("Squat") == "Back Squat"
        assert canonical_lift_name("Barbell Row") == "Pendlay Row"

    def test_canonical_lift_name_passes_unknown_names_through(self):
        assert canonical_lift_name("Some Novel Lift") == "Some Novel Lift"

    def test_canonical_lift_name_follows_admin_edits(self):
        LiftAlias.objects.create(from_name="Weighted Pullup", to_name="Pull-up")
        assert canonical_lift_name("Weighted Pullup") == "Pull-up"

    def test_builtin_membership_from_db(self):
        builtins = liftosaur_builtin_lift_names()
        assert "Back Squat" in builtins
        # "Barbell Row" only exists as an alias raw name (-> Pendlay Row), never
        # seeded as its own Lift row, so it is never itself builtin.
        assert "Barbell Row" not in builtins

    def test_builtin_membership_follows_admin_edits(self):
        Lift.objects.create(name="Landmine Press", is_liftosaur_builtin=True)
        assert "Landmine Press" in liftosaur_builtin_lift_names()

    def test_bodyweight_added_quality_from_db(self):
        assert is_bodyweight_added_lift("Pull-up")
        assert not is_bodyweight_added_lift("Back Squat")

    def test_bodyweight_added_quality_follows_admin_edits(self):
        Lift.objects.filter(name="Dip").update(is_bodyweight_added=False)
        assert not is_bodyweight_added_lift("Dip")
