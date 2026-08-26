"""Tests for the seed_fitnessvolt_lifts management command (TASK-104)."""

import pytest
from django.core.management import call_command

from core.models import LiftAlias, LiftAliasSource

pytestmark = pytest.mark.django_db


class TestSeedFitnessVoltLifts:
    def test_seeds_alias_rows_from_fixture(self):
        # The session conftest already ran the command once; the reference
        # rows a deployed instance has must exist. Both slug conventions the
        # real capability doc uses are covered: verified's hyphenated slugs
        # and gym's underscored slugs.
        assert LiftAlias.objects.filter(
            source=LiftAliasSource.FITNESSVOLT, from_name="squat", to_name="Back Squat"
        ).exists()
        assert LiftAlias.objects.filter(
            source=LiftAliasSource.FITNESSVOLT,
            from_name="back_squat",
            to_name="Back Squat",
        ).exists()
        assert LiftAlias.objects.filter(
            source=LiftAliasSource.FITNESSVOLT,
            from_name="bench-press",
            to_name="Bench Press",
        ).exists()
        assert LiftAlias.objects.filter(
            source=LiftAliasSource.FITNESSVOLT, from_name="pullup", to_name="Pull-up"
        ).exists()

    def test_rerun_is_idempotent(self):
        before = LiftAlias.objects.filter(source=LiftAliasSource.FITNESSVOLT).count()
        call_command("seed_fitnessvolt_lifts")
        assert (
            LiftAlias.objects.filter(source=LiftAliasSource.FITNESSVOLT).count()
            == before
        )

    def test_rerun_restores_edited_mapping(self):
        LiftAlias.objects.filter(
            source=LiftAliasSource.FITNESSVOLT, from_name="squat"
        ).update(to_name="Wrong Name")
        call_command("seed_fitnessvolt_lifts")
        alias = LiftAlias.objects.get(
            source=LiftAliasSource.FITNESSVOLT, from_name="squat"
        )
        assert alias.to_name == "Back Squat"
