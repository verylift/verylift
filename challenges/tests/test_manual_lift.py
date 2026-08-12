"""Tests for challenges.services.submit_manual_lift (TASK-25).

Self-report lets a lifter with no workout tracker connected confirm they hit
one of their own CustomGoalTarget cells. The weight is always read from that
target, never from the caller, and scoring is fully delegated to
score_pooled_history/process_scored_set -- this module only covers the
write + rescore composition, not scoring itself (already covered by
scoring/tests/test_custom_scoring.py).
"""

from datetime import date
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.db import IntegrityError

from challenges.services import submit_manual_lift
from liftosaur.models import LiftHistory, LiftSource
from scoring.models import PointEarnEvent
from scoring.tests.factories import make_custom_scoring_setup

LIFT = "Bench Press"
PERFORMED_AT = date(2025, 6, 1)


def _full_targets(overrides):
    """A complete 1RM..10RM target table, matching the real invariant every
    saved CustomGoal actually has (save_custom_goal enforces full coverage) --
    best_score_for_set assumes every rep count 1..10 resolves. ``overrides``
    pins specific rep counts to specific weights for the test's own
    assertions; every other rep count gets a descending filler value."""
    targets = {rep: Decimal(100 - rep) for rep in range(1, 11)}
    targets.update(overrides)
    return targets


@pytest.fixture
def setup(db):
    return make_custom_scoring_setup(
        lift=LIFT,
        targets=_full_targets({3: Decimal("90.00"), 8: Decimal("60.00")}),
    )


class TestSubmitManualLift:
    def test_creates_manual_lift_history_row(self, setup):
        user, challenge, participant = setup

        result = submit_manual_lift(
            user=user,
            challenge=challenge,
            participant=participant,
            lift=LIFT,
            rep_count=8,
            performed_at=PERFORMED_AT,
        )

        assert result is not None
        history_row, points_earned = result
        assert points_earned == 3  # 8RM -> 11 - 8
        assert history_row.source == LiftSource.MANUAL
        assert history_row.weight_kg == Decimal("60.00")
        assert history_row.reps == 8
        assert history_row.performed_at == PERFORMED_AT
        assert history_row.equipment == ""

    def test_creates_matching_point_earn_event(self, setup):
        user, challenge, participant = setup

        submit_manual_lift(
            user=user,
            challenge=challenge,
            participant=participant,
            lift=LIFT,
            rep_count=8,
            performed_at=PERFORMED_AT,
        )

        event = PointEarnEvent.objects.get(
            user=user, challenge=challenge, lift=LIFT, is_current_best=True
        )
        assert event.source == LiftSource.MANUAL
        assert event.points_earned == 3  # 11 - 8

    def test_returns_none_when_goal_not_configured(self, db):
        from accounts.tests.factories import UserFactory
        from challenges.tests.factories import (
            ChallengeParticipantFactory,
            make_custom_challenge,
        )

        user = UserFactory()
        challenge = make_custom_challenge(lifts=[LIFT])
        participant = ChallengeParticipantFactory(user=user, challenge=challenge)
        assert participant.has_goal_configured is False

        result = submit_manual_lift(
            user=user,
            challenge=challenge,
            participant=participant,
            lift=LIFT,
            rep_count=8,
            performed_at=PERFORMED_AT,
        )
        assert result is None
        assert not LiftHistory.objects.filter(user=user, lift=LIFT).exists()

    def test_returns_none_when_no_target_for_rep_count(self, setup):
        user, challenge, participant = setup

        result = submit_manual_lift(
            user=user,
            challenge=challenge,
            participant=participant,
            lift=LIFT,
            rep_count=11,  # outside the goal's 1..10 target range
            performed_at=PERFORMED_AT,
        )
        assert result is None

    def test_returns_none_when_lift_not_covered_by_goal(self, setup):
        user, challenge, participant = setup

        result = submit_manual_lift(
            user=user,
            challenge=challenge,
            participant=participant,
            lift="Deadlift",  # not configured on this participant's goal
            rep_count=8,
            performed_at=PERFORMED_AT,
        )
        assert result is None

    def test_resubmitting_the_same_set_is_refused(self, setup):
        """The second submission cannot raise the score -- it IS the current
        best -- so it is refused rather than written again and reported as an
        improvement (which is how it used to surface: a "new personal best"
        toast for a set already logged)."""
        user, challenge, participant = setup

        first = submit_manual_lift(
            user=user,
            challenge=challenge,
            participant=participant,
            lift=LIFT,
            rep_count=8,
            performed_at=PERFORMED_AT,
        )
        second = submit_manual_lift(
            user=user,
            challenge=challenge,
            participant=participant,
            lift=LIFT,
            rep_count=8,
            performed_at=PERFORMED_AT,
        )

        assert first is not None
        assert second is None
        assert (
            LiftHistory.objects.filter(
                user=user, lift=LIFT, performed_at=PERFORMED_AT, reps=8
            ).count()
            == 1
        )

    def test_races_a_duplicate_insert_gracefully(self, setup):
        """A lost race for the same insert hits IntegrityError, not a 500 --
        matches the tolerant-of-races convention liftosaur.services already
        uses for its own pool writes."""
        user, challenge, participant = setup

        # Pre-create the row the "concurrent" insert would have raced against.
        LiftHistory.objects.create(
            user=user,
            lift=LIFT,
            performed_at=PERFORMED_AT,
            reps=8,
            weight_kg=Decimal("60.00"),
            source=LiftSource.MANUAL,
        )

        with patch(
            "liftosaur.models.LiftHistory.objects.get_or_create",
            side_effect=IntegrityError,
        ):
            result = submit_manual_lift(
                user=user,
                challenge=challenge,
                participant=participant,
                lift=LIFT,
                rep_count=8,
                performed_at=PERFORMED_AT,
            )

        assert result is not None
        history_row, _is_new_best = result
        assert history_row.weight_kg == Decimal("60.00")

    def test_refuses_a_set_worth_less_than_the_existing_best(self, setup):
        """A lighter rep-max submitted after a heavier one is already scored
        cannot raise the score, so it is refused outright -- nothing is written
        and the existing higher-points event stays the best."""
        user, challenge, participant = setup

        submit_manual_lift(
            user=user,
            challenge=challenge,
            participant=participant,
            lift=LIFT,
            rep_count=3,  # 8 points
            performed_at=PERFORMED_AT,
        )
        result = submit_manual_lift(
            user=user,
            challenge=challenge,
            participant=participant,
            lift=LIFT,
            rep_count=8,  # 3 points -- weaker than the existing best
            performed_at=date(2025, 6, 2),
        )

        assert result is None
        assert not LiftHistory.objects.filter(
            user=user, lift=LIFT, performed_at=date(2025, 6, 2)
        ).exists()

        current_best = PointEarnEvent.objects.get(
            user=user, challenge=challenge, lift=LIFT, is_current_best=True
        )
        assert current_best.reps == 3
        assert current_best.points_earned == 8
