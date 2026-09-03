"""Tests for the recanonicalize_lift_history repair command (TASK-156)."""

from datetime import date
from decimal import Decimal

import pytest
from django.core.management import call_command

from accounts.tests.factories import UserFactory
from challenges.custom_goals import save_custom_goal
from challenges.models import Challenge, ChallengeParticipant
from challenges.tests.factories import (
    ChallengeParticipantFactory,
    make_custom_challenge,
)
from core.models import LiftHistory
from core.tests.factories import LiftHistoryFactory
from scoring.models import PointEarnEvent

PERFORMED_AT = date(2025, 6, 20)


def _setup(*, status=Challenge.Status.ACTIVE):
    """Challenge covering Snatch Press, with one participant with a locked goal."""
    user = UserFactory(liftosaur_api_key="key")
    challenge = make_custom_challenge(
        lifts=["Snatch Press"],
        status=status,
        start_date=date(2025, 1, 1),
        end_date=date(2026, 12, 31),
    )
    participant = ChallengeParticipantFactory(
        user=user,
        challenge=challenge,
        invite_status=ChallengeParticipant.InviteStatus.ACCEPTED,
    )
    save_custom_goal(
        participant,
        "Goal",
        {"Snatch Press": {rep: Decimal("10.00") for rep in range(1, 11)}},
    )
    return user, challenge


def _raw_row(user):
    """A set pooled under the raw, un-aliased name the bug produced.

    'Behind The Neck Press' (capital 'The') is what Liftosaur actually emitted;
    the seeded alias reads 'Behind the Neck Press', so the case-sensitive lookup
    missed and the set landed here instead of under 'Snatch Press'.
    """
    return LiftHistoryFactory(
        user=user,
        lift="Behind The Neck Press",
        performed_at=PERFORMED_AT,
        reps=7,
        weight_kg=Decimal("27.22"),
        equipment="",
    )


@pytest.mark.django_db
class TestRecanonicalizeLiftHistory:
    def test_raw_row_is_recanonicalized_and_scored(self):
        user, challenge = _setup()
        _raw_row(user)

        call_command("recanonicalize_lift_history")

        assert not LiftHistory.objects.filter(
            user=user, lift="Behind The Neck Press"
        ).exists()
        assert LiftHistory.objects.filter(user=user, lift="Snatch Press").exists()
        # The recovered set now counts in scoring.
        assert PointEarnEvent.objects.filter(
            user=user, challenge=challenge, lift="Snatch Press"
        ).exists()

    def test_collision_with_existing_canonical_row_deletes_duplicate(self):
        user, _ = _setup()
        _raw_row(user)
        # A later correctly-aliased re-sync already pooled the same set canonically.
        canonical = LiftHistoryFactory(
            user=user,
            lift="Snatch Press",
            performed_at=PERFORMED_AT,
            reps=7,
            weight_kg=Decimal("27.22"),
            equipment="",
        )

        call_command("recanonicalize_lift_history")

        rows = LiftHistory.objects.filter(user=user, lift="Snatch Press")
        assert rows.count() == 1
        assert rows.get().pk == canonical.pk
        assert not LiftHistory.objects.filter(lift="Behind The Neck Press").exists()

    def test_dry_run_makes_no_changes(self):
        user, challenge = _setup()
        _raw_row(user)

        call_command("recanonicalize_lift_history", "--dry-run")

        assert LiftHistory.objects.filter(lift="Behind The Neck Press").exists()
        assert not LiftHistory.objects.filter(lift="Snatch Press").exists()
        assert not PointEarnEvent.objects.filter(challenge=challenge).exists()

    def test_already_canonical_row_is_untouched(self):
        user, _ = _setup()
        row = LiftHistoryFactory(
            user=user,
            lift="Snatch Press",
            performed_at=PERFORMED_AT,
            reps=7,
            weight_kg=Decimal("27.22"),
        )

        call_command("recanonicalize_lift_history")

        assert LiftHistory.objects.get(pk=row.pk).lift == "Snatch Press"

    def test_frozen_challenge_is_not_rescored(self):
        user, challenge = _setup(status=Challenge.Status.COMPLETED)
        _raw_row(user)

        call_command("recanonicalize_lift_history")

        # Row is still recanonicalized, but the frozen ledger is not written.
        assert LiftHistory.objects.filter(user=user, lift="Snatch Press").exists()
        assert not PointEarnEvent.objects.filter(challenge=challenge).exists()

    def test_username_filter_limits_repair(self):
        user, _ = _setup()
        _raw_row(user)
        other_user, _ = _setup()
        _raw_row(other_user)

        call_command("recanonicalize_lift_history", "--username", user.username)

        assert LiftHistory.objects.filter(user=user, lift="Snatch Press").exists()
        assert LiftHistory.objects.filter(
            user=other_user, lift="Behind The Neck Press"
        ).exists()
