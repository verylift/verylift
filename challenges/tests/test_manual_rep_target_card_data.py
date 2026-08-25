"""Tests for the self-report carousel data build_rep_target_personal_data
attaches to each REP_TARGET summary card (issue #85 follow-up, UAT rework):
manual_targets and manual_default_rep_count -- the sibling of
test_manual_lift_card_data.py.

The carousel is reps-first: each stop is the fewest reps that earns a new,
distinct point value, so the stop count is min(target_reps, 10) rather than
always 10. For target_reps >= 10 this is numerically identical to the
original points->reps table; the interesting cases are target_reps < 10,
where naively emitting 10 columns would produce unreachable point values and
duplicate rep counts.

manual_default_rep_count opens on the participant's current best (a second
UAT follow-up), not their most recently logged set -- see
TestDefaultRepCountForRepTarget.
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from accounts.tests.factories import UserFactory
from challenges.models import Challenge, ChallengeParticipant
from challenges.services import (
    build_rep_target_personal_data,
    submit_manual_rep_target_set,
)
from challenges.tests.factories import (
    ChallengeParticipantFactory,
    RepTargetGoalFactory,
    RepTargetGoalTargetFactory,
    make_rep_target_challenge,
)
from scoring.models import PointEarnEvent
from scoring.tests.factories import PointEarnEventFactory

pytestmark = pytest.mark.django_db

LIFT = "Push Up"


def _accept(participant):
    participant.invite_status = ChallengeParticipant.InviteStatus.ACCEPTED
    participant.joined_at = timezone.now() - timedelta(days=30)
    participant.save(update_fields=["invite_status", "joined_at"])
    return participant


def _give_goal(participant, *, target_weight, target_reps):
    goal = RepTargetGoalFactory(participant=participant, name="Goal")
    RepTargetGoalTargetFactory(
        goal=goal, lift=LIFT, target_weight=target_weight, target_reps=target_reps
    )
    participant.rep_target_goal = goal
    participant.save(update_fields=["rep_target_goal"])
    return goal


@pytest.fixture
def user():
    return UserFactory(unit_preference="kg")


@pytest.fixture
def challenge(user):
    return make_rep_target_challenge(
        lifts=[LIFT], creator=user, status=Challenge.Status.ACTIVE
    )


@pytest.fixture
def participant(user, challenge):
    return _accept(ChallengeParticipantFactory(user=user, challenge=challenge))


class TestManualTargetsForRepTarget:
    @pytest.mark.parametrize(
        "target_reps,expected",
        [
            (3, [(1, 3), (2, 6), (3, 10)]),
            (5, [(1, 2), (2, 4), (3, 6), (4, 8), (5, 10)]),
            (
                7,
                [(1, 1), (2, 2), (3, 4), (4, 5), (5, 7), (6, 8), (7, 10)],
            ),
            (
                20,
                [
                    (2, 1),
                    (4, 2),
                    (6, 3),
                    (8, 4),
                    (10, 5),
                    (12, 6),
                    (14, 7),
                    (16, 8),
                    (18, 9),
                    (20, 10),
                ],
            ),
            (
                100,
                [(n * 10, n) for n in range(1, 11)],
            ),
        ],
    )
    def test_stop_list_matches_the_real_scoring_formula(
        self, user, challenge, participant, target_reps, expected
    ):
        """Every stop is a real (reps, points) pair the scorer would actually
        award -- no unreachable point values, no padding to a fixed count."""
        _give_goal(participant, target_weight=Decimal("0"), target_reps=target_reps)
        data = build_rep_target_personal_data(user, challenge, participant)
        card = data["summary_cards"][0]
        actual = [(t["rep_count"], t["points"]) for t in card["manual_targets"]]
        assert actual == expected
        assert len(card["manual_targets"]) == min(target_reps, 10)

    @pytest.mark.parametrize("target_reps", [1, 2, 3, 4, 5, 6, 7, 9, 13, 20, 32])
    def test_no_duplicate_reps_or_points_across_stops(
        self, user, challenge, participant, target_reps
    ):
        _give_goal(participant, target_weight=Decimal("0"), target_reps=target_reps)
        data = build_rep_target_personal_data(user, challenge, participant)
        card = data["summary_cards"][0]
        reps = [t["rep_count"] for t in card["manual_targets"]]
        points = [t["points"] for t in card["manual_targets"]]
        assert len(reps) == len(set(reps))
        assert len(points) == len(set(points))
        assert 0 not in points
        assert len(card["manual_targets"]) == min(target_reps, 10)

    def test_current_best_flag_matches_scored_tier(self, user, challenge, participant):
        _give_goal(participant, target_weight=Decimal("0"), target_reps=20)
        PointEarnEventFactory(
            user=user,
            challenge=challenge,
            lift=LIFT,
            points_earned=5,
            is_current_best=True,
        )
        data = build_rep_target_personal_data(user, challenge, participant)
        card = data["summary_cards"][0]
        flagged = [t["points"] for t in card["manual_targets"] if t["is_current_best"]]
        assert flagged == [5]

    def test_confirming_a_stop_always_scores_exactly_its_own_label(
        self, user, challenge, participant
    ):
        """Every stop's label IS the reps that earns its listed points, so
        confirming it can never score more or less than promised -- unlike
        Classic's rep-max ladder, which can tie."""
        _give_goal(participant, target_weight=Decimal("0"), target_reps=5)
        data = build_rep_target_personal_data(user, challenge, participant)
        card = data["summary_cards"][0]
        stop = next(t for t in card["manual_targets"] if t["rep_count"] == 1)
        assert stop["points"] == 2
        assert stop["points_delta"] == 2

        submit_manual_rep_target_set(
            user=user,
            challenge=challenge,
            participant=participant,
            lift=LIFT,
            rep_count=1,
            performed_at=timezone.now().date(),
        )
        event = PointEarnEvent.objects.get(user=user, challenge=challenge, lift=LIFT)
        assert event.points_earned == stop["points"]

    def test_manual_targets_use_the_goals_fixed_target_weight(
        self, user, challenge, participant
    ):
        """Unlike Classic's carousel, weight never varies across entries --
        every entry logs at the goal's own fixed target_weight."""
        _give_goal(participant, target_weight=Decimal("25.00"), target_reps=10)
        data = build_rep_target_personal_data(user, challenge, participant)
        card = data["summary_cards"][0]
        weights = {t["weight"] for t in card["manual_targets"]}
        assert weights == {card["target_weight"]}


class TestDefaultRepCountForRepTarget:
    def test_default_rep_count_for_scored_card(self, user, challenge, participant):
        _give_goal(participant, target_weight=Decimal("0"), target_reps=20)
        PointEarnEventFactory(
            user=user,
            challenge=challenge,
            lift=LIFT,
            points_earned=5,
            is_current_best=True,
        )
        data = build_rep_target_personal_data(user, challenge, participant)
        card = data["summary_cards"][0]
        assert card["state"] == "scored"
        assert card["manual_default_rep_count"] == 10  # 5 points -> 10 reps

    def test_default_rep_count_falls_back_to_first_stop_with_no_history(
        self, user, challenge, participant
    ):
        _give_goal(participant, target_weight=Decimal("0"), target_reps=20)
        data = build_rep_target_personal_data(user, challenge, participant)
        card = data["summary_cards"][0]
        assert card["state"] == "no_data"
        assert card["manual_default_rep_count"] == 2  # first stop -> 2 reps

    def test_default_rep_count_falls_back_to_first_stop_for_a_short_stop_list(
        self, user, challenge, participant
    ):
        """A small target_reps produces fewer than 10 stops -- the fallback
        must still land on a stop that actually exists (target_reps=3 has
        only 3 stops, at reps 1/2/3)."""
        _give_goal(participant, target_weight=Decimal("0"), target_reps=3)
        data = build_rep_target_personal_data(user, challenge, participant)
        card = data["summary_cards"][0]
        assert card["manual_default_rep_count"] == 1
        assert card["manual_default_rep_count"] in [
            t["rep_count"] for t in card["manual_targets"]
        ]

    def test_default_rep_count_falls_back_when_only_zero_point_history_exists(
        self, user, challenge, participant
    ):
        """A sub-threshold set is stored as a real zero-point PointEarnEvent
        with is_current_best=False, so it never contributes to
        card["points_earned"] -- the card falls back to the first stop same
        as a lift with no history at all."""
        _give_goal(participant, target_weight=Decimal("0"), target_reps=20)
        PointEarnEventFactory(
            user=user,
            challenge=challenge,
            lift=LIFT,
            points_earned=0,
            is_current_best=False,
            performed_at=timezone.now().date(),
        )
        data = build_rep_target_personal_data(user, challenge, participant)
        card = data["summary_cards"][0]
        assert card["manual_default_rep_count"] == 2
        assert card["manual_default_rep_count"] in [
            t["rep_count"] for t in card["manual_targets"]
        ]

    def test_default_rep_count_opens_on_current_best_not_most_recent_event(
        self, user, challenge, participant
    ):
        """The regression this whole rule exists for: a lifter whose most
        recent set scored worse than their current best (a deload, a
        warm-up, a failed attempt) must still open the carousel on the best,
        not on what they just did -- fails under the old
        most-recent-event-wins behaviour (would open on 8 reps here)."""
        _give_goal(participant, target_weight=Decimal("0"), target_reps=20)
        # Current best: 8 points (16 reps), logged first.
        PointEarnEventFactory(
            user=user,
            challenge=challenge,
            lift=LIFT,
            points_earned=8,
            is_current_best=True,
            performed_at=timezone.now().date() - timedelta(days=10),
        )
        # A later, worse session (4 points, 8 reps) doesn't beat the best, so
        # it's not is_current_best -- but it IS the most recent.
        PointEarnEventFactory(
            user=user,
            challenge=challenge,
            lift=LIFT,
            points_earned=4,
            is_current_best=False,
            performed_at=timezone.now().date(),
        )
        data = build_rep_target_personal_data(user, challenge, participant)
        card = data["summary_cards"][0]
        assert card["manual_default_rep_count"] == 16
        flagged = [
            t["rep_count"] for t in card["manual_targets"] if t["is_current_best"]
        ]
        assert card["manual_default_rep_count"] == flagged[0]

    def test_default_rep_count_falls_back_when_event_points_are_unmatched(
        self, user, challenge, participant
    ):
        """A current-best event whose points don't match any current stop
        (e.g. a goal that changed target_reps after the event was scored)
        falls back to the first stop rather than raising or defaulting to a
        rep count no stop actually has."""
        _give_goal(participant, target_weight=Decimal("0"), target_reps=3)
        PointEarnEventFactory(
            user=user,
            challenge=challenge,
            lift=LIFT,
            points_earned=7,  # not a reachable value for target_reps=3
            is_current_best=True,
            performed_at=timezone.now().date(),
        )
        data = build_rep_target_personal_data(user, challenge, participant)
        card = data["summary_cards"][0]
        assert card["manual_default_rep_count"] == 1
        assert card["manual_default_rep_count"] in [
            t["rep_count"] for t in card["manual_targets"]
        ]
