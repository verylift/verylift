"""Unit tests for scoring.services.score_pooled_history.

These run purely against the local DB — no Liftosaur API mocking is needed,
which is the whole point of decoupling scoring from sync (TASK-94). Every
challenge is CUSTOM (TASK-248): there is no bodyweight-scaled threshold, no
anchoring, and no bodyweight stamp to refresh — the removal of that whole
apparatus is itself a fixed bug class (a lifter with no bodyweight data used
to be unscoreable; now bodyweight does not exist as a concept in scoring).
"""

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from accounts.tests.factories import UserFactory
from challenges.models import Challenge
from core.tests.factories import LiftHistoryFactory
from scoring.models import PointEarnEvent
from scoring.services import ScoringSummary, score_pooled_history
from scoring.tests.factories import make_custom_scoring_setup

LIFT = "Back Squat"


def flat_targets(weight):
    return {rep: Decimal(weight) for rep in range(1, 11)}


def make_setup(
    *,
    is_bailed=False,
    lift=LIFT,
    history_window=Challenge.HistoryWindow.FROM_JOIN,
    start_date=date(2025, 1, 1),
    targets=None,
):
    """Return (user, challenge, participant) ready for pooled scoring."""
    return make_custom_scoring_setup(
        lift=lift,
        targets=targets or flat_targets("100"),
        is_bailed=is_bailed,
        history_window=history_window,
        start_date=start_date,
        end_date=date(2026, 12, 31),
    )


@pytest.mark.django_db
class TestScorePooledHistory:
    def test_scores_pooled_row_into_point_event(self):
        """A qualifying pooled set produces a current-best PointEarnEvent."""
        user, challenge, _ = make_setup()
        LiftHistoryFactory(
            user=user,
            lift=LIFT,
            performed_at=date(2025, 6, 1),
            reps=1,
            weight_kg=Decimal("100.00"),
        )

        summary = score_pooled_history(user=user, challenge=challenge)

        assert isinstance(summary, ScoringSummary)
        assert summary.sets_evaluated == 1
        assert summary.new_point_events == 1
        event = PointEarnEvent.objects.get(user=user, challenge=challenge)
        assert event.is_current_best is True
        assert event.points_earned == 10

    def test_is_idempotent_across_reruns(self):
        """Re-running does not re-score an already-scored set."""
        user, challenge, _ = make_setup()
        LiftHistoryFactory(
            user=user,
            lift=LIFT,
            performed_at=date(2025, 6, 1),
            reps=1,
            weight_kg=Decimal("100.00"),
        )

        score_pooled_history(user=user, challenge=challenge)
        second = score_pooled_history(user=user, challenge=challenge)

        assert second.sets_evaluated == 0
        assert second.new_point_events == 0
        assert (
            PointEarnEvent.objects.filter(user=user, challenge=challenge).count() == 1
        )

    def test_ignores_lifts_outside_the_configured_set(self):
        """Pooled rows for a lift not configured on the challenge are ignored."""
        user, challenge, _ = make_setup()
        LiftHistoryFactory(
            user=user,
            lift="Bicep Curl",
            performed_at=date(2025, 6, 1),
            reps=1,
            weight_kg=Decimal("100.00"),
        )

        summary = score_pooled_history(user=user, challenge=challenge)

        assert summary.sets_evaluated == 0
        assert summary.new_point_events == 0

    def test_respects_from_start_window(self):
        """Rows before the challenge window start are excluded."""
        user, challenge, _ = make_setup(
            history_window=Challenge.HistoryWindow.FROM_START,
            start_date=date(2025, 6, 1),
        )
        LiftHistoryFactory(
            user=user,
            lift=LIFT,
            performed_at=date(2025, 5, 1),  # before window start
            reps=1,
            weight_kg=Decimal("100.00"),
        )
        LiftHistoryFactory(
            user=user,
            lift=LIFT,
            performed_at=date(2025, 6, 15),  # inside window
            reps=1,
            weight_kg=Decimal("100.00"),
        )

        summary = score_pooled_history(user=user, challenge=challenge)

        assert summary.sets_evaluated == 1
        assert summary.new_point_events == 1

    def test_returns_empty_summary_for_non_participant(self):
        """A user who is not a participant scores nothing and is logged."""
        _, challenge, _ = make_setup()
        stranger = UserFactory()
        LiftHistoryFactory(
            user=stranger,
            lift=LIFT,
            performed_at=date(2025, 6, 1),
            reps=1,
            weight_kg=Decimal("100.00"),
        )

        summary = score_pooled_history(user=stranger, challenge=challenge)

        assert summary == ScoringSummary()
        assert not PointEarnEvent.objects.filter(user=stranger).exists()

    @pytest.mark.parametrize(
        "status", [Challenge.Status.COMPLETED, Challenge.Status.CANCELLED]
    )
    def test_terminal_challenge_evaluates_nothing(self, status):
        """A terminal challenge's ledger is locked, so the pool is never walked.

        process_scored_set would refuse each set anyway; asserting on
        sets_evaluated is what pins the earlier bail-out, which is what keeps
        every detail-page open on a finished challenge from re-walking the pool
        and re-snapshotting the leaderboard for a ledger that cannot change.
        """
        user, challenge, _ = make_setup()
        LiftHistoryFactory(
            user=user,
            lift=LIFT,
            performed_at=date(2025, 6, 1),
            reps=1,
            weight_kg=Decimal("100.00"),
        )
        challenge.status = status
        challenge.save(update_fields=["status"])

        summary = score_pooled_history(user=user, challenge=challenge)

        assert summary.sets_evaluated == 0
        assert summary.new_point_events == 0
        assert not PointEarnEvent.objects.filter(challenge=challenge).exists()

    def test_no_api_call_involved(self):
        """score_pooled_history writes no LiftosaurSyncLog (local-DB only)."""
        from liftosaur.models import LiftosaurSyncLog

        user, challenge, _ = make_setup()
        LiftHistoryFactory(
            user=user,
            lift=LIFT,
            performed_at=date(2025, 6, 1),
            reps=1,
            weight_kg=Decimal("100.00"),
        )

        score_pooled_history(user=user, challenge=challenge)

        assert not LiftosaurSyncLog.objects.exists()


@pytest.mark.django_db
class TestSameDaySameRepDistinctSetsScore:
    """TASK-116 / TASK-248 plan §1b: a bodyweight and an assisted set of the
    same lift on the same day with the same rep count are two distinct sets.
    The bodyweight set scores normally; the assisted set is excluded from
    scoring entirely — never even a zero-point audit row — since its
    recorded weight is net total load, not added weight, and there is no
    bodyweight left in the product to convert with.
    """

    PERFORMED_AT = date(2026, 6, 30)

    def _make_pullup_setup(self):
        return make_setup(lift="Pull-up", targets=flat_targets("0"))

    def test_bodyweight_set_scores_assisted_set_produces_no_event(self):
        user, challenge, _ = self._make_pullup_setup()
        # Free bodyweight Pull-up: added weight 0 meets the 0 target exactly.
        LiftHistoryFactory(
            user=user,
            lift="Pull-up",
            performed_at=self.PERFORMED_AT,
            reps=5,
            weight_kg=Decimal("0.00"),
            equipment="",
        )
        # Assisted Leverage-Machine Pull-up recorded the same day/reps: net
        # total load (68.95kg), not added weight — never scores on this lift.
        LiftHistoryFactory(
            user=user,
            lift="Pull-up",
            performed_at=self.PERFORMED_AT,
            reps=5,
            weight_kg=Decimal("68.95"),
            equipment="Leverage Machine",
        )

        summary = score_pooled_history(user=user, challenge=challenge)

        assert summary.sets_evaluated == 1
        assert summary.new_point_events == 1

        events = PointEarnEvent.objects.filter(
            user=user, challenge=challenge, lift="Pull-up"
        )
        assert events.count() == 1
        event = events.get()
        assert event.equipment == ""
        assert event.weight == Decimal("0.00")
        assert event.points_earned == 10
        assert event.is_current_best is True

    def test_rerun_does_not_duplicate_the_scored_set(self):
        user, challenge, _ = self._make_pullup_setup()
        LiftHistoryFactory(
            user=user,
            lift="Pull-up",
            performed_at=self.PERFORMED_AT,
            reps=5,
            weight_kg=Decimal("0.00"),
            equipment="",
        )
        LiftHistoryFactory(
            user=user,
            lift="Pull-up",
            performed_at=self.PERFORMED_AT,
            reps=5,
            weight_kg=Decimal("68.95"),
            equipment="Leverage Machine",
        )

        score_pooled_history(user=user, challenge=challenge)
        second = score_pooled_history(user=user, challenge=challenge)

        assert second.sets_evaluated == 0
        assert second.new_point_events == 0
        assert (
            PointEarnEvent.objects.filter(
                user=user, challenge=challenge, lift="Pull-up"
            ).count()
            == 1
        )


@pytest.mark.django_db
def test_synced_at_is_timezone_aware():
    """The synced_at stamp written to events is timezone-aware UTC."""
    user, challenge, _ = make_setup()
    LiftHistoryFactory(
        user=user,
        lift=LIFT,
        performed_at=date(2025, 6, 1),
        reps=1,
        weight_kg=Decimal("100.00"),
    )

    before = datetime.now(tz=UTC)
    score_pooled_history(user=user, challenge=challenge)

    event = PointEarnEvent.objects.get(user=user, challenge=challenge)
    assert event.synced_at.tzinfo is not None
    assert event.synced_at >= before


@pytest.mark.django_db
def test_steady_state_already_scored_issues_no_updates():
    """Re-running when every row is already scored issues no UPDATE queries
    at all — there is no bodyweight stamp left to ever need refreshing."""
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    user, challenge, _ = make_setup()
    LiftHistoryFactory(
        user=user,
        lift=LIFT,
        performed_at=date(2025, 6, 1),
        reps=1,
        weight_kg=Decimal("100.00"),
    )

    # First run scores the set.
    score_pooled_history(user=user, challenge=challenge)
    assert PointEarnEvent.objects.filter(user=user, challenge=challenge).count() == 1

    # Second run: capture queries and verify no UPDATE to PointEarnEvent.
    with CaptureQueriesContext(connection) as queries:
        score_pooled_history(user=user, challenge=challenge)

    update_queries = [
        q
        for q in queries
        if "UPDATE" in q["sql"] and "scoring_pointearnevent" in q["sql"]
    ]
    assert not update_queries, f"Expected no UPDATE queries but found: {update_queries}"
