"""Tests for the seed_strong_lift_aliases command and its fixture."""

import json
from collections import Counter

import pytest
from django.core.management import call_command

from core.lift_resolution import normalize_lift_name, normalize_lift_name_strict
from core.management.commands.seed_lifts import (
    FIXTURE_PATH as LIFTOSAUR_FIXTURE_PATH,
)
from core.models import LiftAlias, LiftAliasSource
from workout_imports.management.commands.seed_strong_lift_aliases import FIXTURE_PATH


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


def _colliding_groups(names, normalize):
    counts = Counter(normalize(name) for name in names)
    colliding_keys = {key for key, count in counts.items() if count > 1}
    return {
        key: sorted(name for name in names if normalize(name) == key)
        for key in colliding_keys
    }


def test_no_two_seeded_lift_names_collide_under_punctuation_normalization():
    # StrongImporter's stage 3 treats "Chin Up" and "Chin-up" (or any other
    # punctuation/casing variant) as the same lift. That's only safe as long
    # as the seeded catalogue never contains two distinct lifts that fold to
    # the same key -- otherwise this importer (and the guard below) would
    # silently pool two different exercises' history together.
    collisions = _colliding_groups(_seeded_lift_names(), normalize_lift_name)
    assert not collisions, collisions


def test_no_two_seeded_lift_names_collide_under_separator_free_normalization():
    # Stage 4's catch-all is strictly looser than stage 3 (it also folds
    # "Chinup" onto "Chin-up"), so it needs its own, stricter version of the
    # same safety property: a future catalogue addition like "Pull Up"
    # alongside the existing "Pull-up", or "Situp" alongside "Sit Up", would
    # make two distinct lifts collide and silently pool their history. This
    # must fail CI before that ships, not surface later as a scoring bug.
    collisions = _colliding_groups(_seeded_lift_names(), normalize_lift_name_strict)
    assert not collisions, collisions


@pytest.mark.django_db
class TestSeedStrongLiftAliasesCommand:
    def test_seeds_all_fixture_aliases(self):
        call_command("seed_strong_lift_aliases")
        assert (
            dict(
                LiftAlias.objects.filter(source=LiftAliasSource.STRONG).values_list(
                    "from_name", "to_name"
                )
            )
            == EXPECTED_ALIASES
        )

    def test_command_is_idempotent(self):
        call_command("seed_strong_lift_aliases")
        call_command("seed_strong_lift_aliases")
        assert LiftAlias.objects.filter(source=LiftAliasSource.STRONG).count() == len(
            EXPECTED_ALIASES
        )

    def test_reseeding_reconciles_an_edited_row(self):
        call_command("seed_strong_lift_aliases")
        LiftAlias.objects.filter(
            source=LiftAliasSource.STRONG, from_name="Squat (Barbell)"
        ).update(to_name="Front Squat")

        call_command("seed_strong_lift_aliases")

        assert (
            LiftAlias.objects.get(
                source=LiftAliasSource.STRONG, from_name="Squat (Barbell)"
            ).to_name
            == "Back Squat"
        )
