"""Scoring integration tests for CUSTOM challenges (TASK-134, TASK-248).

Every challenge reads a participant-authored flat 1RM–10RM target table
directly, with no bodyweight/sex/tier lookup, no Epley derivation, and (as of
TASK-248) no bodyweight arithmetic anywhere: for bodyweight-added lifts the
target IS the added weight and the recorded LiftHistory weight IS the added
weight, so the comparison is direct.
"""

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from challenges.models import ChallengeParticipant
from challenges.tests.factories import (
    ChallengeParticipantFactory,
    make_custom_challenge,
)
from core.tests.factories import LiftHistoryFactory
from scoring.models import PointEarnEvent
from scoring.services import _GoalTargets, process_scored_set, score_pooled_history
from scoring.tests.factories import make_custom_scoring_setup

LIFT = "Bench Press"
PERFORMED_AT = date(2025, 6, 1)
SYNCED_AT = datetime(2025, 6, 2, tzinfo=UTC)


def flat_targets(weight):
    return {rep: Decimal(weight) for rep in range(1, 11)}


def descending_targets(one_rm, ten_rm):
    """A linearly descending 1RM..10RM table (a shape Epley cannot reproduce)."""
    one_rm = Decimal(one_rm)
    step = (one_rm - Decimal(ten_rm)) / Decimal(9)
    return {rep: (one_rm - step * (rep - 1)) for rep in range(1, 11)}


@pytest.mark.django_db
class TestGoalTargets:
    def test_returns_per_lift_target_dict(self):
        _u, _challenge, participant = make_custom_scoring_setup(
            lift=LIFT, targets=flat_targets("100")
        )
        resolver = _GoalTargets(custom_goal=participant.custom_goal)
        result = resolver.targets_for(LIFT)
        assert result == {rep: Decimal("100.00") for rep in range(1, 11)}

    def test_returns_none_for_uncovered_lift(self):
        _u, _challenge, participant = make_custom_scoring_setup(
            lift=LIFT, targets=flat_targets("100")
        )
        resolver = _GoalTargets(custom_goal=participant.custom_goal)
        assert resolver.targets_for("Deadlift") is None

    def test_bodyweight_added_target_returned_verbatim(self):
        # No bodyweight arithmetic anywhere in scoring (TASK-248): the stored
        # added-weight target comes back exactly as authored.
        _u, _challenge, participant = make_custom_scoring_setup(
            lift="Chin-up", targets=flat_targets("0")
        )
        resolver = _GoalTargets(custom_goal=participant.custom_goal)
        assert resolver.targets_for("Chin-up") == {
            rep: Decimal("0.00") for rep in range(1, 11)
        }


@pytest.mark.django_db
class TestProcessScoredSetCustom:
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

    def test_exact_hit_scores(self):
        user, challenge, _ = make_custom_scoring_setup(
            lift=LIFT, targets=flat_targets("100")
        )
        event = self._score(user, challenge, reps=1, weight="100")
        assert event is not None
        assert event.points_earned == 10
        assert event.is_current_best is True

    def test_sub_target_records_zero_point_audit_row(self):
        user, challenge, _ = make_custom_scoring_setup(
            lift=LIFT, targets=flat_targets("100")
        )
        event = self._score(user, challenge, reps=1, weight="50")
        assert event is not None
        assert event.points_earned == 0
        assert event.is_current_best is False

    def test_near_miss_below_flat_target_earns_zero(self):
        # Custom targets are exact: a 99kg set one kg short of a flat 100kg
        # target no longer counts (no fuzz band, TASK-135).
        user, challenge, _ = make_custom_scoring_setup(
            lift=LIFT, targets=flat_targets("100")
        )
        event = self._score(user, challenge, reps=1, weight="99")
        assert event is not None
        assert event.points_earned == 0
        assert event.is_current_best is False

    def test_over_performance_matches_flat_target(self):
        # 5RM target is 90; 95kg x5 clears it (6 pts) but falls short of the
        # heavier 1–4RM targets — a shape no single Epley 1RM could express.
        targets = {
            1: Decimal("130"),
            2: Decimal("125"),
            3: Decimal("120"),
            4: Decimal("115"),
            5: Decimal("90"),
            6: Decimal("85"),
            7: Decimal("82"),
            8: Decimal("80"),
            9: Decimal("78"),
            10: Decimal("76"),
        }
        user, challenge, _ = make_custom_scoring_setup(lift=LIFT, targets=targets)
        event = self._score(user, challenge, reps=5, weight="95")
        assert event is not None
        assert event.points_earned == 6

    def test_reps_capped_at_ten(self):
        user, challenge, _ = make_custom_scoring_setup(
            lift=LIFT, targets=descending_targets("200", "80")
        )
        # 80kg only clears the 10RM (80); 15 reps caps at 10 → 1 point.
        event = self._score(user, challenge, reps=15, weight="80")
        assert event is not None
        assert event.points_earned == 1

    def test_missing_custom_goal_is_a_no_op(self):
        challenge = make_custom_challenge(lifts=[LIFT])
        participant = ChallengeParticipantFactory(
            challenge=challenge,
            invite_status=ChallengeParticipant.InviteStatus.ACCEPTED,
        )
        assert participant.custom_goal_id is None
        event = self._score(participant.user, challenge, reps=1, weight="500")
        assert event is None
        assert PointEarnEvent.objects.count() == 0


@pytest.mark.django_db
class TestScorePooledHistoryCustom:
    def test_scores_pooled_row_against_custom_targets(self):
        user, challenge, _ = make_custom_scoring_setup(
            lift=LIFT,
            targets=flat_targets("100"),
            start_date=date(2025, 1, 1),
            end_date=date(2026, 12, 31),
        )
        LiftHistoryFactory(
            user=user,
            lift=LIFT,
            performed_at=PERFORMED_AT,
            reps=1,
            weight_kg=Decimal("100.00"),
        )
        # A lift not configured on the challenge must never be scored.
        LiftHistoryFactory(
            user=user,
            lift="Deadlift",
            performed_at=PERFORMED_AT,
            reps=1,
            weight_kg=Decimal("300.00"),
        )

        summary = score_pooled_history(user=user, challenge=challenge)

        assert summary.sets_evaluated == 1
        assert summary.new_point_events == 1
        event = PointEarnEvent.objects.get(user=user, challenge=challenge)
        assert event.lift == LIFT
        assert event.points_earned == 10


BW_LIFT = "Chin-up"


@pytest.mark.django_db
class TestBodyweightAddedCustomScoring:
    """For bodyweight-added lifts the target IS the added weight, and the
    recorded weight IS the added weight too (TASK-248) — direct comparison,
    no bodyweight arithmetic anywhere. Assisted-equipment sets are excluded
    from scoring entirely (§1b): their recorded weight is net total load, not
    added weight, and there is no bodyweight left to convert with.
    """

    def _score(self, user, challenge, *, reps, weight, **kwargs):
        return process_scored_set(
            user=user,
            challenge=challenge,
            lift=BW_LIFT,
            performed_at=PERFORMED_AT,
            reps=reps,
            weight=Decimal(weight),
            synced_at=SYNCED_AT,
            **kwargs,
        )

    def test_zero_target_met_by_bodyweight_only_set(self):
        user, challenge, _ = make_custom_scoring_setup(
            lift=BW_LIFT, targets=flat_targets("0")
        )
        event = self._score(user, challenge, reps=1, weight="0")
        assert event is not None
        assert event.points_earned == 10
        assert event.weight == Decimal("0.00")

    def test_zero_target_not_met_by_assisted_set(self):
        # Silent-wrong-answer guard (§1b): this must NEVER produce a
        # PointEarnEvent, not even a sub-threshold audit row. A regression
        # that reintroduces bodyweight-free "just compare the numbers" logic
        # would instead award 10 points here (70 >= 0).
        user, challenge, _ = make_custom_scoring_setup(
            lift=BW_LIFT, targets=flat_targets("0")
        )
        event = self._score(
            user, challenge, reps=1, weight="70", equipment="Leverage Machine"
        )
        assert event is None
        assert PointEarnEvent.objects.count() == 0

    def test_negative_target_never_met_by_assisted_set(self):
        # Inverted from the pre-TASK-248 behaviour: a negative (band-assisted)
        # target used to be satisfiable by a leverage-machine set's net load.
        # It no longer is — leverage-machine sets never score on this lift.
        user, challenge, _ = make_custom_scoring_setup(
            lift=BW_LIFT, targets=flat_targets("-5")
        )
        event = self._score(
            user, challenge, reps=1, weight="75", equipment="Leverage Machine"
        )
        assert event is None
        assert PointEarnEvent.objects.count() == 0

    def test_positive_target_met_exactly(self):
        user, challenge, _ = make_custom_scoring_setup(
            lift=BW_LIFT, targets=flat_targets("20")
        )
        event = self._score(user, challenge, reps=1, weight="20")
        assert event is not None
        assert event.points_earned == 10
        assert event.weight == Decimal("20.00")

    def test_positive_target_missed_when_below_target(self):
        user, challenge, _ = make_custom_scoring_setup(
            lift=BW_LIFT, targets=flat_targets("20")
        )
        event = self._score(user, challenge, reps=1, weight="10")
        assert event is not None
        assert event.points_earned == 0
