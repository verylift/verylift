import datetime
from decimal import Decimal

import pytest
from django.db import IntegrityError
from django.utils import timezone

from accounts.tests.factories import UserFactory
from core.models import LiftHistory, LiftSource
from core.tests.factories import LiftHistoryFactory


@pytest.mark.django_db
class TestLiftHistoryModel:
    def test_unique_on_user_lift_date_reps_weight(self):
        user = UserFactory()
        performed = timezone.now().date()
        LiftHistoryFactory(
            user=user,
            lift="Back Squat",
            performed_at=performed,
            reps=5,
            weight_kg=Decimal("100.00"),
        )
        with pytest.raises(IntegrityError):
            LiftHistory.objects.create(
                user=user,
                lift="Back Squat",
                performed_at=performed,
                reps=5,
                weight_kg=Decimal("100.00"),
            )

    def test_same_day_same_reps_different_weight_coexist(self):
        """TASK-116: a top set and a lighter set of the same lift on the same
        day at the same rep count are distinct rows, not a collision."""
        user = UserFactory()
        performed = timezone.now().date()
        LiftHistoryFactory(
            user=user,
            lift="Pull-up",
            performed_at=performed,
            reps=5,
            weight_kg=Decimal("0.00"),
        )
        LiftHistoryFactory(
            user=user,
            lift="Pull-up",
            performed_at=performed,
            reps=5,
            weight_kg=Decimal("68.95"),
        )
        assert LiftHistory.objects.filter(user=user, lift="Pull-up").count() == 2

    def test_ordering_by_performed_at_descending(self):
        user = UserFactory()
        earlier = LiftHistoryFactory(
            user=user,
            lift="Deadlift",
            performed_at=timezone.now().date() - datetime.timedelta(days=7),
        )
        later = LiftHistoryFactory(
            user=user,
            lift="Bench Press",
            performed_at=timezone.now().date(),
        )
        rows = list(LiftHistory.objects.filter(user=user))
        assert rows[0].pk == later.pk
        assert rows[1].pk == earlier.pk

    def test_str_includes_lift_weight_and_reps(self):
        row = LiftHistoryFactory(lift="Back Squat", weight_kg=Decimal("120.00"), reps=3)
        s = str(row)
        assert "Back Squat" in s
        assert "120.00" in s
        assert "3" in s

    def test_source_defaults_to_liftosaur(self):
        """TASK-25: a row created without an explicit source (the shape of
        every pre-existing sync-written row) is LIFTOSAUR, never blank/null —
        the model default is what the AddField migration backfills existing
        rows to."""
        row = LiftHistory.objects.create(
            user=UserFactory(),
            lift="Back Squat",
            performed_at=timezone.now().date(),
            reps=5,
            weight_kg=Decimal("100.00"),
        )
        assert row.source == LiftSource.LIFTOSAUR

    def test_source_can_be_manual(self):
        row = LiftHistoryFactory(source=LiftSource.MANUAL)
        assert row.source == LiftSource.MANUAL
