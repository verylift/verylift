"""Tests for the seed_strong_lift_aliases command and its fixture."""

import json

import pytest
from django.core.management import call_command

from liftosaur.management.commands.seed_liftosaur_lifts import (
    FIXTURE_PATH as LIFTOSAUR_FIXTURE_PATH,
)
from workout_imports.management.commands.seed_strong_lift_aliases import FIXTURE_PATH
from workout_imports.models import StrongLiftAlias


def _fixture_aliases():
    with open(FIXTURE_PATH) as f:
        return json.load(f)["aliases"]


EXPECTED_ALIASES = {row["from_name"]: row["to_name"] for row in _fixture_aliases()}


def _seeded_lift_names():
    with open(LIFTOSAUR_FIXTURE_PATH) as f:
        return {row["name"] for row in json.load(f)["lifts"]}


def test_every_alias_to_name_matches_a_seeded_canonical_lift():
    # Guards against a to_name drifting out of sync with the seeded
    # Liftosaur catalogue (e.g. a rename) -- if this fails, the alias
    # would resolve to a name Lift.objects never contains, silently
    # keeping the imported sets from pooling with anything.
    seeded_names = _seeded_lift_names()
    unknown = {
        to_name for to_name in EXPECTED_ALIASES.values() if to_name not in seeded_names
    }
    assert not unknown


@pytest.mark.django_db
class TestSeedStrongLiftAliasesCommand:
    def test_seeds_all_fixture_aliases(self):
        call_command("seed_strong_lift_aliases")
        assert (
            dict(StrongLiftAlias.objects.values_list("from_name", "to_name"))
            == EXPECTED_ALIASES
        )

    def test_command_is_idempotent(self):
        call_command("seed_strong_lift_aliases")
        call_command("seed_strong_lift_aliases")
        assert StrongLiftAlias.objects.count() == len(EXPECTED_ALIASES)

    def test_reseeding_reconciles_an_edited_row(self):
        call_command("seed_strong_lift_aliases")
        StrongLiftAlias.objects.filter(from_name="Squat (Barbell)").update(
            to_name="Front Squat"
        )

        call_command("seed_strong_lift_aliases")

        assert (
            StrongLiftAlias.objects.get(from_name="Squat (Barbell)").to_name
            == "Back Squat"
        )
