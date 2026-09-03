"""Scoring integration tests for REP_TARGET challenges (issue #85)."""

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from challenges.models import ChallengeParticipant
from challenges.tests.factories import (
    ChallengeParticipantFactory,
    make_rep_target_challenge,
)
from core.tests.factories import LiftHistoryFactory
from scoring.models import PointEarnEvent
from scoring.services import (
    _RepTargetGoalTargets,
    process_scored_set,
    score_pooled_history,
)
from scoring.tests.factories import make_rep_target_scoring_setup

LIFT = "Push Up"
PERFORMED_AT = date(2025, 6, 1)
SYNCED_AT = datetime(2025, 6, 2, tzinfo=UTC)


class TestRepTargetGoalTargets:
    @pytest.mark.django_db
    def test_returns_weight_reps_pair(self):
        _u, _c, participant = make_rep_target_scoring_setup(
            lift=LIFT, target_weight=Decimal("0"), target_reps=20
        )
        resolver = _RepTargetGoalTargets(rep_target_goal=participant.rep_target_goal)
        assert resolver.targets_for(LIFT) == (Decimal("0.00"), 20)

    @pytest.mark.django_db
    def test_returns_none_for_uncovered_lift(self):
        _u, _c, participant = make_rep_target_scoring_setup(
            lift=LIFT, target_weight=Decimal("0"), target_reps=20
        )
        resolver = _RepTargetGoalTargets(rep_target_goal=participant.rep_target_goal)
        assert resolver.targets_for("Dip") is None


@pytest.mark.django_db
class TestProcessScoredSetRepTarget:
    def _score(self, user, challenge, *, reps, weight, **kwargs):
        return process_scored_set(
            user=user,
            challenge=challenge,
            lift=LIFT,
            performed_at=PERFORMED_AT,
            reps=reps,
            weight=Decimal(weight),
            synced_at=SYNCED_AT,
            **kwargs,
        )

    def test_weight_gate_met_and_reps_scale_points(self):
        user, challenge, _ = make_rep_target_scoring_setup(
            lift=LIFT, target_weight=Decimal("0"), target_reps=20
        )
        event = self._score(user, challenge, reps=12, weight="0")
        assert event is not None
        assert event.points_earned == 6
        assert event.is_current_best is True

    def test_weight_gate_not_met_records_zero_point_audit_row(self):
        user, challenge, _ = make_rep_target_scoring_setup(
            lift=LIFT, target_weight=Decimal("10"), target_reps=20
        )
        event = self._score(user, challenge, reps=20, weight="5")
        assert event is not None
        assert event.points_earned == 0
        assert event.is_current_best is False

    def test_best_set_replaces_old(self):
        user, challenge, _ = make_rep_target_scoring_setup(
            lift=LIFT, target_weight=Decimal("0"), target_reps=20
        )
        first = self._score(user, challenge, reps=10, weight="0")
        assert first.points_earned == 5
        assert first.is_current_best is True

        second = self._score(user, challenge, reps=20, weight="0")
        assert second.points_earned == 10
        assert second.is_current_best is True

        first.refresh_from_db()
        assert first.is_current_best is False

    def test_worse_set_does_not_overtake_current_best(self):
        user, challenge, _ = make_rep_target_scoring_setup(
            lift=LIFT, target_weight=Decimal("0"), target_reps=20
        )
        self._score(user, challenge, reps=20, weight="0")
        worse = self._score(user, challenge, reps=5, weight="0")
        # floor(10 * 5 / 20) = 2; a quarter of the target earns a fifth of
        # the points, and still does not displace the full-target best.
        assert worse.points_earned == 2
        assert worse.is_current_best is False

    def test_missing_rep_target_goal_is_a_no_op(self):
        challenge = make_rep_target_challenge(lifts=[LIFT])
        participant = ChallengeParticipantFactory(
            challenge=challenge,
            invite_status=ChallengeParticipant.InviteStatus.ACCEPTED,
        )
        assert participant.rep_target_goal_id is None
        event = self._score(participant.user, challenge, reps=20, weight="0")
        assert event is None
        assert PointEarnEvent.objects.count() == 0


@pytest.mark.django_db
class TestScorePooledHistoryRepTarget:
    def test_scores_pooled_row_against_rep_target(self):
        user, challenge, _ = make_rep_target_scoring_setup(
            lift=LIFT,
            target_weight=Decimal("0"),
            target_reps=20,
            start_date=date(2025, 1, 1),
            end_date=date(2026, 12, 31),
        )
        LiftHistoryFactory(
            user=user,
            lift=LIFT,
            performed_at=PERFORMED_AT,
            reps=20,
            weight_kg=Decimal("0.00"),
        )
        LiftHistoryFactory(
            user=user,
            lift="Dip",
            performed_at=PERFORMED_AT,
            reps=20,
            weight_kg=Decimal("0.00"),
        )

        summary = score_pooled_history(user=user, challenge=challenge)

        assert summary.sets_evaluated == 1
        assert summary.new_point_events == 1
        event = PointEarnEvent.objects.get(user=user, challenge=challenge)
        assert event.lift == LIFT
        assert event.points_earned == 10
