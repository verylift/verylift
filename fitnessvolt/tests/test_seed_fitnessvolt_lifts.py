"""Tests for the seed_fitnessvolt_lifts management command (TASK-104)."""

import pytest
from django.core.management import call_command

from fitnessvolt.models import FitnessVoltLiftAlias

pytestmark = pytest.mark.django_db


class TestSeedFitnessVoltLifts:
    def test_seeds_alias_rows_from_fixture(self):
        # The session conftest already ran the command once; the reference
        # rows a deployed instance has must exist. Both slug conventions the
        # real capability doc uses are covered: verified's hyphenated slugs
        # and gym's underscored slugs.
        assert FitnessVoltLiftAlias.objects.filter(
            from_slug="squat", to_name="Back Squat"
        ).exists()
        assert FitnessVoltLiftAlias.objects.filter(
            from_slug="back_squat", to_name="Back Squat"
        ).exists()
        assert FitnessVoltLiftAlias.objects.filter(
            from_slug="bench-press", to_name="Bench Press"
        ).exists()
        assert FitnessVoltLiftAlias.objects.filter(
            from_slug="pullup", to_name="Pull-up"
        ).exists()

    def test_rerun_is_idempotent(self):
        before = FitnessVoltLiftAlias.objects.count()
        call_command("seed_fitnessvolt_lifts")
        assert FitnessVoltLiftAlias.objects.count() == before

    def test_rerun_restores_edited_mapping(self):
        FitnessVoltLiftAlias.objects.filter(from_slug="squat").update(
            to_name="Wrong Name"
        )
        call_command("seed_fitnessvolt_lifts")
        alias = FitnessVoltLiftAlias.objects.get(from_slug="squat")
        assert alias.to_name == "Back Squat"
