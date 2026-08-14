"""Integration tests for scoring/services.py.

Covers process_scored_set and rank_participants. Every challenge is CUSTOM
(TASK-248): thresholds are a flat per-lift, per-rep target table, never a
bodyweight-scaled multiplier, so tests build the equivalent flat table via
tier_thresholds (the same Epley expansion the old built-in path used) rather
than passing bodyweight_kg/multiplier straight to process_scored_set.
"""

from datetime import date
from decimal import Decimal

import pytest
from django.utils import timezone

from accounts.tests.factories import UserFactory
from challenges.models import ChallengeParticipant
from challenges.tests.factories import (
    ChallengeParticipantFactory,
    CustomGoalFactory,
    CustomGoalTargetFactory,
    make_custom_challenge,
)
from scoring.domain.calculator import tier_thresholds
from scoring.models import PointEarnEvent
from scoring.services import process_scored_set, rank_participants

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

LIFT = "Squat"
PERFORMED_AT = date(2025, 1, 15)
SYNCED_AT = timezone.now()


def targets_from_multiplier(multiplier, bodyweight, *, tier="Intermediate"):
    """A flat {rep: weight} table equivalent to the old multiplier x bodyweight
    threshold, via the same Epley expansion (tier_thresholds)."""
    thresholds = tier_thresholds(tier, Decimal(multiplier), Decimal(bodyweight))
    return {rm.reps: rm.weight for rm in thresholds.rep_maxes}


def _add_participant_with_goal(challenge, user, targets_by_lift, *, is_bailed=False):
    """Attach a second (or first) participant with a locked custom goal to an
    existing challenge. ``targets_by_lift`` is ``{lift: {rep: weight}}``."""
    participant = ChallengeParticipantFactory(
        user=user,
        challenge=challenge,
        invite_status=ChallengeParticipant.InviteStatus.ACCEPTED,
        is_bailed=is_bailed,
    )
    goal = CustomGoalFactory(participant=participant, name="Goal")
    for lift, cells in targets_by_lift.items():
        for rep, weight in cells.items():
            CustomGoalTargetFactory(
                goal=goal, lift=lift, rep_count=rep, target_weight=weight
            )
    participant.custom_goal = goal
    participant.save(update_fields=["custom_goal"])
    return participant


def make_setup(
    *,
    is_bailed=False,
    multiplier=Decimal("1.5000"),
    lift=LIFT,
    bodyweight=Decimal("100.00"),
):
    """Return (user, challenge, participant) ready for scoring."""
    user = UserFactory()
    challenge = make_custom_challenge(lifts=[lift])
    targets = targets_from_multiplier(multiplier, bodyweight)
    participant = _add_participant_with_goal(
        challenge, user, {lift: targets}, is_bailed=is_bailed
    )
    return user, challenge, participant


# ---------------------------------------------------------------------------
# process_scored_set
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestProcessScoredSet:
    def test_creates_new_best_event_when_no_prior_best(self):
        """First scored set for a lift becomes is_current_best=True."""
        user, challenge, _ = make_setup(multiplier=Decimal("1.0000"))
        # target 1RM=100kg; weight=100kg, reps=1 -> satisfies 1RM -> 10 pts
        event = process_scored_set(
            user=user,
            challenge=challenge,
            lift=LIFT,
            performed_at=PERFORMED_AT,
            reps=1,
            weight=Decimal("100.00"),
            synced_at=SYNCED_AT,
        )
        assert event is not None
        assert event.is_current_best is True
        assert event.points_earned == 10

    def test_new_higher_score_replaces_current_best(self):
        """A better performance demotes the old best and promotes the new one."""
        user, challenge, _ = make_setup(multiplier=Decimal("1.0000"))
        # 5RM threshold ~85.71kg; 87kg satisfies 5RM (6 pts) but NOT 1RM.
        first = process_scored_set(
            user=user,
            challenge=challenge,
            lift=LIFT,
            performed_at=PERFORMED_AT,
            reps=5,
            weight=Decimal("87.00"),
            synced_at=SYNCED_AT,
        )
        assert first is not None
        assert first.points_earned == 6
        assert first.is_current_best is True

        # Second: 1 rep @ 100kg -> 10 pts (higher)
        second = process_scored_set(
            user=user,
            challenge=challenge,
            lift=LIFT,
            performed_at=PERFORMED_AT,
            reps=1,
            weight=Decimal("100.00"),
            synced_at=SYNCED_AT,
        )
        assert second is not None
        assert second.is_current_best is True

        # Old best should be demoted
        first.refresh_from_db()
        assert first.is_current_best is False

        # Only one current best exists
        assert (
            PointEarnEvent.objects.filter(
                user=user, challenge=challenge, lift=LIFT, is_current_best=True
            ).count()
            == 1
        )

    def test_lower_score_appended_as_audit_trail(self):
        """A weaker performance is saved with is_current_best=False."""
        user, challenge, _ = make_setup(multiplier=Decimal("1.0000"))
        first = process_scored_set(
            user=user,
            challenge=challenge,
            lift=LIFT,
            performed_at=PERFORMED_AT,
            reps=1,
            weight=Decimal("100.00"),
            synced_at=SYNCED_AT,
        )
        assert first.is_current_best is True

        second = process_scored_set(
            user=user,
            challenge=challenge,
            lift=LIFT,
            performed_at=PERFORMED_AT,
            reps=5,
            weight=Decimal("87.00"),
            synced_at=SYNCED_AT,
        )
        assert second is not None
        assert second.is_current_best is False

        first.refresh_from_db()
        assert first.is_current_best is True

    def test_equal_score_appended_as_audit_trail(self):
        """A repeated score (same points) is saved but does not displace the best."""
        user, challenge, _ = make_setup(multiplier=Decimal("1.0000"))
        first = process_scored_set(
            user=user,
            challenge=challenge,
            lift=LIFT,
            performed_at=PERFORMED_AT,
            reps=5,
            weight=Decimal("87.00"),
            synced_at=SYNCED_AT,
        )
        second = process_scored_set(
            user=user,
            challenge=challenge,
            lift=LIFT,
            performed_at=PERFORMED_AT,
            reps=5,
            weight=Decimal("87.00"),
            synced_at=SYNCED_AT,
        )
        assert second is not None
        assert second.is_current_best is False
        first.refresh_from_db()
        assert first.is_current_best is True

    def test_bailed_participant_is_skipped(self):
        """is_bailed=True freezes the ledger; no events are written."""
        user, challenge, _ = make_setup(multiplier=Decimal("1.0000"), is_bailed=True)
        result = process_scored_set(
            user=user,
            challenge=challenge,
            lift=LIFT,
            performed_at=PERFORMED_AT,
            reps=1,
            weight=Decimal("100.00"),
            synced_at=SYNCED_AT,
        )
        assert result is None
        assert PointEarnEvent.objects.count() == 0

    def test_completed_challenge_is_skipped(self):
        """A completed challenge locks the ledger; no events are written."""
        from challenges.models import Challenge

        user, challenge, _ = make_setup(multiplier=Decimal("1.0000"))
        challenge.status = Challenge.Status.COMPLETED
        challenge.save(update_fields=["status"])
        result = process_scored_set(
            user=user,
            challenge=challenge,
            lift=LIFT,
            performed_at=PERFORMED_AT,
            reps=1,
            weight=Decimal("100.00"),
            synced_at=SYNCED_AT,
        )
        assert result is None
        assert PointEarnEvent.objects.count() == 0

    def test_cancelled_challenge_is_skipped(self):
        """A cancelled challenge locks the ledger; no events are written."""
        from challenges.models import Challenge

        user, challenge, _ = make_setup(multiplier=Decimal("1.0000"))
        challenge.status = Challenge.Status.CANCELLED
        challenge.save(update_fields=["status"])
        result = process_scored_set(
            user=user,
            challenge=challenge,
            lift=LIFT,
            performed_at=PERFORMED_AT,
            reps=1,
            weight=Decimal("100.00"),
            synced_at=SYNCED_AT,
        )
        assert result is None
        assert PointEarnEvent.objects.count() == 0

    def test_no_goal_configured_is_skipped(self):
        """If the participant has no CustomGoal at all, nothing is written —
        including, deliberately, a participant row that predates this task
        and so was never backfilled a goal (TASK-248 revision 5)."""
        user = UserFactory()
        challenge = make_custom_challenge(lifts=[LIFT])
        ChallengeParticipantFactory(
            user=user,
            challenge=challenge,
            invite_status=ChallengeParticipant.InviteStatus.ACCEPTED,
        )
        result = process_scored_set(
            user=user,
            challenge=challenge,
            lift=LIFT,
            performed_at=PERFORMED_AT,
            reps=1,
            weight=Decimal("100.00"),
            synced_at=SYNCED_AT,
        )
        assert result is None
        assert PointEarnEvent.objects.count() == 0

    def test_below_threshold_persists_zero_point_event(self):
        """A sub-threshold set persists a zero-point, non-current-best audit row."""
        user, challenge, _ = make_setup(
            multiplier=Decimal("2.0000"), bodyweight=Decimal("50.00")
        )
        # threshold=100kg; weight=50kg (well below) -> no threshold satisfied
        result = process_scored_set(
            user=user,
            challenge=challenge,
            lift=LIFT,
            performed_at=PERFORMED_AT,
            reps=1,
            weight=Decimal("50.00"),
            synced_at=SYNCED_AT,
        )
        assert result is not None
        assert result.points_earned == 0
        assert result.is_current_best is False
        assert result.weight == Decimal("50.00")
        assert result.reps == 1
        assert PointEarnEvent.objects.count() == 1

    def test_near_miss_below_threshold_earns_zero(self):
        """A set just short of the threshold now earns zero: the comparison is
        exact, there is no fuzz band (TASK-135)."""
        user, challenge, _ = make_setup(
            multiplier=Decimal("2.0000"), bodyweight=Decimal("50.00")
        )
        # 1RM threshold=100kg. 99kg misses it by 1kg and no longer counts.
        result = process_scored_set(
            user=user,
            challenge=challenge,
            lift=LIFT,
            performed_at=PERFORMED_AT,
            reps=1,
            weight=Decimal("99.00"),
            synced_at=SYNCED_AT,
        )
        assert result is not None
        assert result.points_earned == 0
        assert result.is_current_best is False

    def test_exact_meets_threshold_scores(self):
        """A set exactly on the threshold scores."""
        user, challenge, _ = make_setup(
            multiplier=Decimal("2.0000"), bodyweight=Decimal("50.00")
        )
        result = process_scored_set(
            user=user,
            challenge=challenge,
            lift=LIFT,
            performed_at=PERFORMED_AT,
            reps=1,
            weight=Decimal("100.00"),
            synced_at=SYNCED_AT,
        )
        assert result is not None
        assert result.points_earned == 10

    def test_sub_threshold_event_not_on_leaderboard(self):
        """Sub-threshold events (is_current_best=False) never reach the leaderboard."""
        user, challenge, _ = make_setup(
            multiplier=Decimal("2.0000"), bodyweight=Decimal("50.00")
        )
        process_scored_set(
            user=user,
            challenge=challenge,
            lift=LIFT,
            performed_at=PERFORMED_AT,
            reps=1,
            weight=Decimal("50.00"),
            synced_at=SYNCED_AT,
        )
        assert rank_participants(challenge) == []

    def test_sub_threshold_does_not_trigger_overtaken_notifications(self):
        """A sub-threshold set must not create overtaken notifications."""
        from notifications.models import Notification

        user, challenge, _ = make_setup(
            multiplier=Decimal("2.0000"), bodyweight=Decimal("50.00")
        )
        process_scored_set(
            user=user,
            challenge=challenge,
            lift=LIFT,
            performed_at=PERFORMED_AT,
            reps=1,
            weight=Decimal("50.00"),
            synced_at=SYNCED_AT,
        )
        assert (
            Notification.objects.filter(
                event_type=Notification.EventType.OVERTAKEN
            ).count()
            == 0
        )

    def test_sub_threshold_does_not_demote_existing_best(self):
        """A later sub-threshold set leaves an earlier current best untouched."""
        user, challenge, _ = make_setup(multiplier=Decimal("1.0000"))
        best = process_scored_set(
            user=user,
            challenge=challenge,
            lift=LIFT,
            performed_at=PERFORMED_AT,
            reps=1,
            weight=Decimal("100.00"),
            synced_at=SYNCED_AT,
        )
        assert best.is_current_best is True
        sub = process_scored_set(
            user=user,
            challenge=challenge,
            lift=LIFT,
            performed_at=PERFORMED_AT,
            reps=1,
            weight=Decimal("50.00"),
            synced_at=SYNCED_AT,
        )
        assert sub.points_earned == 0
        assert sub.is_current_best is False
        best.refresh_from_db()
        assert best.is_current_best is True

    def test_no_participant_returns_none(self):
        """If the user is not a participant in the challenge, return None."""
        user = UserFactory()
        challenge = make_custom_challenge(lifts=[LIFT])
        # No ChallengeParticipant created
        result = process_scored_set(
            user=user,
            challenge=challenge,
            lift=LIFT,
            performed_at=PERFORMED_AT,
            reps=1,
            weight=Decimal("100.00"),
            synced_at=SYNCED_AT,
        )
        assert result is None

    def test_goal_not_covering_lift_returns_none(self):
        """If the participant's goal does not cover this lift, return None."""
        user, challenge, _ = make_setup(multiplier=Decimal("1.0000"), lift=LIFT)
        result = process_scored_set(
            user=user,
            challenge=challenge,
            lift="Deadlift",
            performed_at=PERFORMED_AT,
            reps=1,
            weight=Decimal("100.00"),
            synced_at=SYNCED_AT,
        )
        assert result is None

    def test_different_lifts_tracked_independently(self):
        """Each lift has its own high-watermark slot."""
        user = UserFactory()
        challenge = make_custom_challenge(lifts=["Squat", "Deadlift"])
        targets = targets_from_multiplier(Decimal("1.0000"), Decimal("100.00"))
        _add_participant_with_goal(
            challenge, user, {"Squat": targets, "Deadlift": targets}
        )
        squat_event = process_scored_set(
            user=user,
            challenge=challenge,
            lift="Squat",
            performed_at=PERFORMED_AT,
            reps=1,
            weight=Decimal("100.00"),
            synced_at=SYNCED_AT,
        )
        deadlift_event = process_scored_set(
            user=user,
            challenge=challenge,
            lift="Deadlift",
            performed_at=PERFORMED_AT,
            reps=1,
            weight=Decimal("100.00"),
            synced_at=SYNCED_AT,
        )
        assert squat_event.is_current_best is True
        assert deadlift_event.is_current_best is True
        assert (
            PointEarnEvent.objects.filter(
                user=user, challenge=challenge, is_current_best=True
            ).count()
            == 2
        )


# ---------------------------------------------------------------------------
# rank_participants
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestRankParticipants:
    def test_empty_challenge_returns_empty_list(self):
        challenge = make_custom_challenge(lifts=[LIFT])
        assert rank_participants(challenge) == []

    def test_single_user_rank_1(self):
        user, challenge, _ = make_setup(multiplier=Decimal("1.0000"))
        # 87kg satisfies 5RM threshold (~85.71kg) but NOT 1RM (100kg) -> 6 pts
        process_scored_set(
            user=user,
            challenge=challenge,
            lift=LIFT,
            performed_at=PERFORMED_AT,
            reps=5,
            weight=Decimal("87.00"),
            synced_at=SYNCED_AT,
        )
        board = rank_participants(challenge)
        assert len(board) == 1
        assert board[0]["user"] == user
        assert board[0]["total_points"] == 6
        assert board[0]["rank"] == 1

    def test_ordering_highest_total_first(self):
        """User with more total points appears before lower-scoring users."""
        challenge = make_custom_challenge(lifts=[LIFT])
        targets = targets_from_multiplier(Decimal("1.0000"), Decimal("100.00"))

        # user_a: 10 pts (1RM)
        user_a = UserFactory()
        _add_participant_with_goal(challenge, user_a, {LIFT: targets})
        process_scored_set(
            user=user_a,
            challenge=challenge,
            lift=LIFT,
            performed_at=PERFORMED_AT,
            reps=1,
            weight=Decimal("100.00"),
            synced_at=SYNCED_AT,
        )

        # user_b: 6 pts (5RM) -- 87kg satisfies 5RM threshold but NOT 1RM
        user_b = UserFactory()
        _add_participant_with_goal(challenge, user_b, {LIFT: targets})
        process_scored_set(
            user=user_b,
            challenge=challenge,
            lift=LIFT,
            performed_at=PERFORMED_AT,
            reps=5,
            weight=Decimal("87.00"),
            synced_at=SYNCED_AT,
        )

        board = rank_participants(challenge)
        assert board[0]["user"] == user_a
        assert board[0]["total_points"] == 10
        assert board[0]["rank"] == 1
        assert board[1]["user"] == user_b
        assert board[1]["total_points"] == 6
        assert board[1]["rank"] == 2

    def test_tied_users_share_rank(self):
        """Two users with identical total_points both receive the same rank."""
        challenge = make_custom_challenge(lifts=[LIFT])
        targets = targets_from_multiplier(Decimal("1.0000"), Decimal("100.00"))

        for _ in range(2):
            user = UserFactory()
            _add_participant_with_goal(challenge, user, {LIFT: targets})
            # 87kg satisfies 5RM threshold but NOT 1RM -> 6 pts each
            process_scored_set(
                user=user,
                challenge=challenge,
                lift=LIFT,
                performed_at=PERFORMED_AT,
                reps=5,
                weight=Decimal("87.00"),
                synced_at=SYNCED_AT,
            )

        board = rank_participants(challenge)
        assert len(board) == 2
        assert board[0]["rank"] == 1
        assert board[1]["rank"] == 1  # Dense ranking: tied users share rank

    def test_leaderboard_sums_across_multiple_lifts(self):
        """Points from multiple lifts are summed per user."""
        challenge = make_custom_challenge(lifts=["Squat", "Deadlift"])
        targets = targets_from_multiplier(Decimal("1.0000"), Decimal("100.00"))
        user = UserFactory()
        _add_participant_with_goal(
            challenge, user, {"Squat": targets, "Deadlift": targets}
        )
        for lift in ["Squat", "Deadlift"]:
            # 87kg satisfies 5RM threshold (~85.71kg) but NOT 1RM (100kg) -> 6 pts
            process_scored_set(
                user=user,
                challenge=challenge,
                lift=lift,
                performed_at=PERFORMED_AT,
                reps=5,
                weight=Decimal("87.00"),
                synced_at=SYNCED_AT,
            )

        board = rank_participants(challenge)
        assert len(board) == 1
        assert board[0]["total_points"] == 12  # 6 + 6

    def test_superseded_events_not_counted(self):
        """Only is_current_best=True events contribute to the leaderboard total."""
        user, challenge, _ = make_setup(multiplier=Decimal("1.0000"))
        # 87kg @ 5RM -> 6 pts (superseded); then 100kg @ 1RM -> 10 pts (current best)
        process_scored_set(
            user=user,
            challenge=challenge,
            lift=LIFT,
            performed_at=PERFORMED_AT,
            reps=5,
            weight=Decimal("87.00"),
            synced_at=SYNCED_AT,
        )
        process_scored_set(
            user=user,
            challenge=challenge,
            lift=LIFT,
            performed_at=PERFORMED_AT,
            reps=1,
            weight=Decimal("100.00"),
            synced_at=SYNCED_AT,
        )
        board = rank_participants(challenge)
        assert board[0]["total_points"] == 10  # not 16

    def test_bailed_participant_excluded_and_ranks_recompute(self):
        """A bailed participant drops off the board and gaps close (dense rank)."""
        challenge = make_custom_challenge(lifts=[LIFT])
        targets = targets_from_multiplier(Decimal("1.0000"), Decimal("100.00"))

        # bailer: 10 pts (1RM) -- the higher scorer, then leaves
        bailer = UserFactory()
        bailer_participant = _add_participant_with_goal(
            challenge, bailer, {LIFT: targets}
        )
        process_scored_set(
            user=bailer,
            challenge=challenge,
            lift=LIFT,
            performed_at=PERFORMED_AT,
            reps=1,
            weight=Decimal("100.00"),
            synced_at=SYNCED_AT,
        )

        # survivor: 6 pts (5RM) -- 87kg satisfies 5RM threshold but NOT 1RM
        survivor = UserFactory()
        _add_participant_with_goal(challenge, survivor, {LIFT: targets})
        process_scored_set(
            user=survivor,
            challenge=challenge,
            lift=LIFT,
            performed_at=PERFORMED_AT,
            reps=5,
            weight=Decimal("87.00"),
            synced_at=SYNCED_AT,
        )

        bailer_participant.is_bailed = True
        bailer_participant.save(update_fields=["is_bailed"])

        board = rank_participants(challenge)
        board_users = {row["user"].pk for row in board}
        assert bailer.pk not in board_users
        assert len(board) == 1
        assert board[0]["user"] == survivor
        assert board[0]["rank"] == 1  # gap left by bailer closed

    def test_dense_ranking_gap_after_tie(self):
        """After a two-way tie at rank 1, the next distinct score is rank 2."""
        challenge = make_custom_challenge(lifts=[LIFT])
        targets = targets_from_multiplier(Decimal("1.0000"), Decimal("100.00"))

        # Two users tied at 10 pts
        for _ in range(2):
            user = UserFactory()
            _add_participant_with_goal(challenge, user, {LIFT: targets})
            process_scored_set(
                user=user,
                challenge=challenge,
                lift=LIFT,
                performed_at=PERFORMED_AT,
                reps=1,
                weight=Decimal("100.00"),
                synced_at=SYNCED_AT,
            )

        # Third user at 6 pts: 87kg @ 5RM satisfies 5RM but NOT 1RM threshold
        user_third = UserFactory()
        _add_participant_with_goal(challenge, user_third, {LIFT: targets})
        process_scored_set(
            user=user_third,
            challenge=challenge,
            lift=LIFT,
            performed_at=PERFORMED_AT,
            reps=5,
            weight=Decimal("87.00"),
            synced_at=SYNCED_AT,
        )

        board = rank_participants(challenge)
        assert len(board) == 3
        ranks = [row["rank"] for row in board]
        assert ranks[0] == 1
        assert ranks[1] == 1
        assert ranks[2] == 2  # Dense: next distinct score is rank 2, not 3


# ---------------------------------------------------------------------------
# rank_participants(include_unscored=True)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestRankParticipantsIncludeUnscored:
    def test_unscored_participants_included_at_zero(self):
        """An accepted participant with no PointEarnEvent still appears, tied
        last at 0 points, once include_unscored=True."""
        user, challenge, _ = make_setup(multiplier=Decimal("1.0000"))
        process_scored_set(
            user=user,
            challenge=challenge,
            lift=LIFT,
            performed_at=PERFORMED_AT,
            reps=1,
            weight=Decimal("100.00"),
            synced_at=SYNCED_AT,
        )
        unscored = UserFactory()
        _add_participant_with_goal(
            challenge,
            unscored,
            {LIFT: targets_from_multiplier(Decimal("1.0000"), Decimal("100.00"))},
        )

        default_board = rank_participants(challenge)
        assert {row["user"] for row in default_board} == {user}

        full_board = rank_participants(challenge, include_unscored=True)
        by_user = {row["user"].pk: row for row in full_board}
        assert by_user[user.pk]["rank"] == 1
        assert by_user[unscored.pk]["total_points"] == 0
        assert by_user[unscored.pk]["rank"] == 2

    def test_one_scorer_rest_tied_at_zero(self):
        """One scorer at rank 1; every unscored participant dense-ties at rank 2."""
        user, challenge, _ = make_setup(multiplier=Decimal("1.0000"))
        process_scored_set(
            user=user,
            challenge=challenge,
            lift=LIFT,
            performed_at=PERFORMED_AT,
            reps=1,
            weight=Decimal("100.00"),
            synced_at=SYNCED_AT,
        )
        targets = targets_from_multiplier(Decimal("1.0000"), Decimal("100.00"))
        unscored_users = [UserFactory() for _ in range(2)]
        for u in unscored_users:
            _add_participant_with_goal(challenge, u, {LIFT: targets})

        board = rank_participants(challenge, include_unscored=True)
        assert len(board) == 3
        ranks_by_user = {row["user"].pk: row["rank"] for row in board}
        assert ranks_by_user[user.pk] == 1
        for u in unscored_users:
            assert ranks_by_user[u.pk] == 2

    def test_bailed_unscored_participant_excluded(self):
        """A bailed participant never appears, scored or not."""
        challenge = make_custom_challenge(lifts=[LIFT])
        targets = targets_from_multiplier(Decimal("1.0000"), Decimal("100.00"))
        bailed_user = UserFactory()
        _add_participant_with_goal(
            challenge, bailed_user, {LIFT: targets}, is_bailed=True
        )

        board = rank_participants(challenge, include_unscored=True)
        assert board == []

    def test_no_participants_returns_empty(self):
        challenge = make_custom_challenge(lifts=[LIFT])
        assert rank_participants(challenge, include_unscored=True) == []
