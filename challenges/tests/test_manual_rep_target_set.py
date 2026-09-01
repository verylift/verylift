"""Tests for challenges.services.submit_manual_rep_target_set (issue #85
follow-up).

Self-report lets a lifter with no workout tracker connected confirm they hit
a rep count at their own RepTargetGoalTarget's fixed weight. The weight is
always read from that target, never from the caller, and scoring is fully
delegated to score_pooled_history/process_scored_set -- this module only
covers the write + rescore composition and the manual/synced parity
guarantee, not scoring itself (already covered by
scoring/tests/test_rep_target_scoring.py).
"""

from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.db import IntegrityError

from challenges.models import Challenge
from challenges.services import submit_manual_rep_target_set
from liftosaur.models import LiftHistory, LiftSource
from liftosaur.tests.factories import LiftHistoryFactory
from scoring.models import PointEarnEvent
from scoring.services import score_pooled_history
from scoring.tests.factories import make_rep_target_scoring_setup

LIFT = "Push Up"
PERFORMED_AT = date(2025, 6, 1)


@pytest.fixture
def setup(db):
    return make_rep_target_scoring_setup(
        lift=LIFT, target_weight=Decimal("0"), target_reps=20
    )


class TestSubmitManualRepTargetSet:
    def test_creates_manual_lift_history_row(self, setup):
        user, challenge, participant = setup

        result = submit_manual_rep_target_set(
            user=user,
            challenge=challenge,
            participant=participant,
            lift=LIFT,
            rep_count=10,  # floor(10*10/20) == 5 points
            performed_at=PERFORMED_AT,
        )

        assert result is not None
        history_row, points_earned = result
        assert points_earned == 5
        assert history_row.source == LiftSource.MANUAL
        assert history_row.weight_kg == Decimal("0")
        assert history_row.reps == 10
        assert history_row.performed_at == PERFORMED_AT

    def test_creates_matching_point_earn_event(self, setup):
        user, challenge, participant = setup

        submit_manual_rep_target_set(
            user=user,
            challenge=challenge,
            participant=participant,
            lift=LIFT,
            rep_count=10,
            performed_at=PERFORMED_AT,
        )

        event = PointEarnEvent.objects.get(
            user=user, challenge=challenge, lift=LIFT, is_current_best=True
        )
        assert event.source == LiftSource.MANUAL
        assert event.points_earned == 5

    def test_returns_none_when_goal_not_configured(self, db):
        from accounts.tests.factories import UserFactory
        from challenges.tests.factories import (
            ChallengeParticipantFactory,
            make_rep_target_challenge,
        )

        user = UserFactory()
        challenge = make_rep_target_challenge(lifts=[LIFT])
        participant = ChallengeParticipantFactory(user=user, challenge=challenge)
        assert participant.has_goal_configured is False

        result = submit_manual_rep_target_set(
            user=user,
            challenge=challenge,
            participant=participant,
            lift=LIFT,
            rep_count=10,
            performed_at=PERFORMED_AT,
        )
        assert result is None
        assert not LiftHistory.objects.filter(user=user, lift=LIFT).exists()

    def test_returns_none_when_lift_not_covered_by_goal(self, setup):
        user, challenge, participant = setup

        result = submit_manual_rep_target_set(
            user=user,
            challenge=challenge,
            participant=participant,
            lift="Deadlift",  # not configured on this participant's goal
            rep_count=10,
            performed_at=PERFORMED_AT,
        )
        assert result is None

    def test_resubmitting_the_same_set_is_refused(self, setup):
        user, challenge, participant = setup

        first = submit_manual_rep_target_set(
            user=user,
            challenge=challenge,
            participant=participant,
            lift=LIFT,
            rep_count=10,
            performed_at=PERFORMED_AT,
        )
        second = submit_manual_rep_target_set(
            user=user,
            challenge=challenge,
            participant=participant,
            lift=LIFT,
            rep_count=10,
            performed_at=PERFORMED_AT,
        )

        assert first is not None
        assert second is None
        assert (
            LiftHistory.objects.filter(
                user=user, lift=LIFT, performed_at=PERFORMED_AT, reps=10
            ).count()
            == 1
        )

    def test_races_a_duplicate_insert_gracefully(self, setup):
        user, challenge, participant = setup

        LiftHistory.objects.create(
            user=user,
            lift=LIFT,
            performed_at=PERFORMED_AT,
            reps=10,
            weight_kg=Decimal("0"),
            source=LiftSource.MANUAL,
        )

        with patch(
            "liftosaur.models.LiftHistory.objects.get_or_create",
            side_effect=IntegrityError,
        ):
            result = submit_manual_rep_target_set(
                user=user,
                challenge=challenge,
                participant=participant,
                lift=LIFT,
                rep_count=10,
                performed_at=PERFORMED_AT,
            )

        assert result is not None
        history_row, _points = result
        assert history_row.weight_kg == Decimal("0")

    def test_refuses_a_set_worth_less_than_the_existing_best(self, setup):
        user, challenge, participant = setup

        submit_manual_rep_target_set(
            user=user,
            challenge=challenge,
            participant=participant,
            lift=LIFT,
            rep_count=20,  # full 10 points
            performed_at=PERFORMED_AT,
        )
        result = submit_manual_rep_target_set(
            user=user,
            challenge=challenge,
            participant=participant,
            lift=LIFT,
            rep_count=5,  # 2 points -- weaker than the existing best
            performed_at=date(2025, 6, 2),
        )

        assert result is None
        assert not LiftHistory.objects.filter(
            user=user, lift=LIFT, performed_at=date(2025, 6, 2)
        ).exists()

        current_best = PointEarnEvent.objects.get(
            user=user, challenge=challenge, lift=LIFT, is_current_best=True
        )
        assert current_best.reps == 20
        assert current_best.points_earned == 10

    def test_refuses_a_set_below_the_weight_gate(self, db):
        """A weight gate below the target is never reachable through this
        path (weight always comes from the goal, never the caller), but the
        underlying scorer refusing it is what keeps a caller-supplied 0-reps
        edge case from ever scoring -- rep_count=0 doesn't clear even a
        1-point tier."""
        user, challenge, participant = make_rep_target_scoring_setup(
            lift=LIFT, target_weight=Decimal("20.00"), target_reps=10
        )

        result = submit_manual_rep_target_set(
            user=user,
            challenge=challenge,
            participant=participant,
            lift=LIFT,
            rep_count=0,
            performed_at=PERFORMED_AT,
        )
        assert result is None

    def test_manual_entry_scores_identically_to_a_synced_set(self, setup):
        """A manual self-report and a Liftosaur-synced set of the same
        (weight, reps) performance must feed scoring identically -- the whole
        point of writing a plain LiftHistory row and delegating to the same
        score_pooled_history the sync path uses."""
        user, challenge, participant = setup

        LiftHistoryFactory(
            user=user,
            lift=LIFT,
            performed_at=PERFORMED_AT,
            reps=10,
            weight_kg=Decimal("0"),
            source=LiftSource.LIFTOSAUR,
            synced_at=datetime(2025, 6, 2, tzinfo=UTC),
        )
        score_pooled_history(user=user, challenge=challenge)
        synced_event = PointEarnEvent.objects.get(
            user=user, challenge=challenge, lift=LIFT, is_current_best=True
        )

        # A second, distinct participant self-reports the exact same set.
        from accounts.tests.factories import UserFactory
        from challenges.models import ChallengeParticipant, RepTargetGoal
        from challenges.tests.factories import (
            ChallengeParticipantFactory,
            RepTargetGoalFactory,
            RepTargetGoalTargetFactory,
        )

        other_user = UserFactory()
        other_participant = ChallengeParticipantFactory(
            user=other_user,
            challenge=challenge,
            invite_status=ChallengeParticipant.InviteStatus.ACCEPTED,
        )
        other_goal = RepTargetGoalFactory(
            participant=other_participant,
            name="Other Goal",
            source_method=RepTargetGoal.SourceMethod.CUSTOM,
        )
        RepTargetGoalTargetFactory(
            goal=other_goal, lift=LIFT, target_weight=Decimal("0"), target_reps=20
        )
        other_participant.rep_target_goal = other_goal
        other_participant.save(update_fields=["rep_target_goal"])

        manual_result = submit_manual_rep_target_set(
            user=other_user,
            challenge=challenge,
            participant=other_participant,
            lift=LIFT,
            rep_count=10,
            performed_at=PERFORMED_AT,
        )

        assert manual_result is not None
        _history_row, manual_points = manual_result
        assert manual_points == synced_event.points_earned

    @pytest.mark.parametrize(
        "status", [Challenge.Status.COMPLETED, Challenge.Status.CANCELLED]
    )
    def test_returns_none_on_a_terminal_challenge(self, setup, status):
        """Same read-only guard as submit_manual_lift: nothing is written
        before the refusal, so a finished challenge never gains a
        LiftHistory row that can no longer score."""
        user, challenge, participant = setup
        challenge.status = status
        challenge.save(update_fields=["status"])

        result = submit_manual_rep_target_set(
            user=user,
            challenge=challenge,
            participant=participant,
            lift=LIFT,
            rep_count=10,
            performed_at=PERFORMED_AT,
        )

        assert result is None
        assert not LiftHistory.objects.filter(user=user, lift=LIFT).exists()
