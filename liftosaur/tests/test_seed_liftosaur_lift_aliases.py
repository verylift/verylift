"""Tests for the seed_liftosaur_lift_aliases command and DB-backed resolution.

Split out of test_seed_liftosaur_lifts.py when the lift register moved to core
(TASK-347, originally TASK-89); the register half now lives in
core/tests/test_seed_lifts.py.
"""

import json

import pytest
from django.core.management import call_command

from core.models import LiftAlias, LiftAliasSource
from liftosaur.management.commands.seed_liftosaur_lift_aliases import FIXTURE_PATH
from liftosaur.services import canonical_lift_name


def _fixture_data():
    with open(FIXTURE_PATH) as f:
        return json.load(f)


EXPECTED_ALIASES = {
    row["from_name"]: row["to_name"] for row in _fixture_data()["aliases"]
}


@pytest.mark.django_db
class TestSeedLiftosaurLiftAliasesCommand:
    def test_seeds_all_aliases(self):
        call_command("seed_liftosaur_lift_aliases")
        assert (
            dict(
                LiftAlias.objects.filter(source=LiftAliasSource.LIFTOSAUR).values_list(
                    "from_name", "to_name"
                )
            )
            == EXPECTED_ALIASES
        )

    def test_command_is_idempotent(self):
        call_command("seed_liftosaur_lift_aliases")
        call_command("seed_liftosaur_lift_aliases")
        assert LiftAlias.objects.filter(
            source=LiftAliasSource.LIFTOSAUR
        ).count() == len(EXPECTED_ALIASES)

    def test_reseeding_restores_edited_rows(self):
        """update_or_create means a re-run reconciles rows back to the fixture."""
        call_command("seed_liftosaur_lift_aliases")
        LiftAlias.objects.filter(
            source=LiftAliasSource.LIFTOSAUR, from_name="Squat"
        ).update(to_name="Front Squat")

        call_command("seed_liftosaur_lift_aliases")

        assert (
            LiftAlias.objects.get(
                source=LiftAliasSource.LIFTOSAUR, from_name="Squat"
            ).to_name
            == "Back Squat"
        )


@pytest.mark.django_db
class TestDbBackedAliasResolution:
    def test_canonical_lift_name_resolves_alias_from_db(self):
        assert canonical_lift_name("Squat") == "Back Squat"
        assert canonical_lift_name("Barbell Row") == "Pendlay Row"

    def test_canonical_lift_name_passes_unknown_names_through(self):
        assert canonical_lift_name("Some Novel Lift") == "Some Novel Lift"

    def test_canonical_lift_name_follows_admin_edits(self):
        LiftAlias.objects.create(
            source=LiftAliasSource.LIFTOSAUR,
            from_name="Weighted Pullup",
            to_name="Pull-up",
        )
        assert canonical_lift_name("Weighted Pullup") == "Pull-up"
