"""Tests for the self-report carousel data build_personal_data attaches to
each summary card (TASK-25): manual_targets and manual_default_rep_count.

manual_default_rep_count opens on the participant's current best (UAT
follow-up), not their most recently logged set -- the opening stop must
match the stop already flagged is_current_best and the points the card's
front face shows."""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from accounts.tests.factories import UserFactory
from challenges.models import Challenge, ChallengeParticipant
from challenges.services import build_personal_data, submit_manual_lift
from challenges.tests.factories import (
    ChallengeParticipantFactory,
    CustomGoalFactory,
    CustomGoalTargetFactory,
    make_custom_challenge,
)
from liftosaur.tests.factories import LiftHistoryFactory
from scoring.models import PointEarnEvent
from scoring.tests.factories import PointEarnEventFactory

pytestmark = pytest.mark.django_db

LIFT = "Bench Press"


def _accept(participant):
    participant.invite_status = ChallengeParticipant.InviteStatus.ACCEPTED
    participant.joined_at = timezone.now() - timedelta(days=30)
    participant.save(update_fields=["invite_status", "joined_at"])
    return participant


def _give_goal(participant, *, targets):
    goal = CustomGoalFactory(participant=participant, name="Goal")
    for rep, weight in targets.items():
        CustomGoalTargetFactory(
            goal=goal, lift=LIFT, rep_count=rep, target_weight=weight
        )
    participant.custom_goal = goal
    participant.save(update_fields=["custom_goal"])
    return goal


@pytest.fixture
def user():
    return UserFactory(unit_preference="kg")


@pytest.fixture
def challenge(user):
    return make_custom_challenge(
        lifts=[LIFT], creator=user, status=Challenge.Status.ACTIVE
    )


@pytest.fixture
def participant(user, challenge):
    return _accept(ChallengeParticipantFactory(user=user, challenge=challenge))


def _flat_targets(weight="60.00"):
    return {rep: Decimal(weight) for rep in range(1, 11)}


class TestManualTargets:
    def test_ten_targets_present(self, user, challenge, participant):
        _give_goal(participant, targets=_flat_targets())
        data = build_personal_data(user, challenge, participant)
        card = data["summary_cards"][0]
        assert len(card["manual_targets"]) == 10
        assert [t["rep_count"] for t in card["manual_targets"]] == list(
            range(10, 0, -1)
        )

    def test_current_best_flag_matches_scored_rep_count(
        self, user, challenge, participant
    ):
        _give_goal(participant, targets=_flat_targets())
        PointEarnEventFactory(
            user=user,
            challenge=challenge,
            lift=LIFT,
            points_earned=8,  # satisfies rep count 11 - 8 = 3
            is_current_best=True,
        )
        data = build_personal_data(user, challenge, participant)
        card = data["summary_cards"][0]
        flagged = [
            t["rep_count"] for t in card["manual_targets"] if t["is_current_best"]
        ]
        assert flagged == [3]

    def test_default_rep_count_for_scored_card(self, user, challenge, participant):
        _give_goal(participant, targets=_flat_targets())
        PointEarnEventFactory(
            user=user,
            challenge=challenge,
            lift=LIFT,
            points_earned=8,
            is_current_best=True,
        )
        data = build_personal_data(user, challenge, participant)
        card = data["summary_cards"][0]
        assert card["state"] == "scored"
        assert card["manual_default_rep_count"] == 3

    def test_default_rep_count_falls_back_to_ten_with_no_history(
        self, user, challenge, participant
    ):
        _give_goal(participant, targets=_flat_targets())
        data = build_personal_data(user, challenge, participant)
        card = data["summary_cards"][0]
        assert card["state"] == "no_data"
        assert card["manual_default_rep_count"] == 10

    def test_default_rep_count_falls_back_to_ten_when_close_to_goal_but_unscored(
        self, user, challenge, participant, settings
    ):
        # close_to_goal means "hasn't earned a point yet" by definition (state
        # is still no_points) -- no PointEarnEvent exists for this lift, so the
        # no-history default applies same as any other unscored lift, even
        # though there's unscored LiftHistory close to a target.
        settings.CHALLENGES_CLOSE_TO_GOAL_GAP_FRACTION = 1.0
        settings.CHALLENGES_CLOSE_TO_GOAL_REPS_GAP = 10
        _give_goal(participant, targets=_flat_targets("100.00"))
        LiftHistoryFactory(
            user=user,
            lift=LIFT,
            performed_at=timezone.now().date(),
            reps=7,
            weight_kg=Decimal("90.00"),
        )
        data = build_personal_data(user, challenge, participant)
        card = data["summary_cards"][0]
        assert card["state"] == "no_points"
        assert card.get("close_to_goal") is True
        assert card["manual_default_rep_count"] == 10

    def test_default_rep_count_opens_on_current_best_not_most_recent_event(
        self, user, challenge, participant
    ):
        """The regression this whole rule exists for: a lifter whose most
        recent set scored worse than their current best (a deload, a
        warm-up, a failed attempt) must still open the carousel on the best,
        not on what they just did -- fails under the old
        most-recent-event-wins behaviour (would open on 7RM here)."""
        _give_goal(participant, targets=_flat_targets())
        # Current best: 8 points (satisfies 3RM), logged first.
        PointEarnEventFactory(
            user=user,
            challenge=challenge,
            lift=LIFT,
            points_earned=8,
            is_current_best=True,
            performed_at=timezone.now().date() - timedelta(days=10),
        )
        # A later, worse session (4 points, satisfies 7RM) doesn't beat the
        # best, so it's not is_current_best -- but it IS the most recent.
        PointEarnEventFactory(
            user=user,
            challenge=challenge,
            lift=LIFT,
            points_earned=4,
            is_current_best=False,
            performed_at=timezone.now().date(),
        )
        data = build_personal_data(user, challenge, participant)
        card = data["summary_cards"][0]
        assert card["manual_default_rep_count"] == 3
        flagged = [
            t["rep_count"] for t in card["manual_targets"] if t["is_current_best"]
        ]
        assert card["manual_default_rep_count"] == flagged[0]

    @pytest.mark.parametrize(
        "nine_rm,expected_points",
        [
            ("91.00", 1),  # well-formed ladder: 9RM strictly heavier than 10RM
            ("90.00", 2),  # tied rungs, as plate rounding routinely produces
            ("89.00", 2),  # non-monotonic, as a hand-entered grid can be
        ],
    )
    def test_points_delta_matches_what_confirming_actually_scores(
        self, user, challenge, participant, nine_rm, expected_points
    ):
        """The carousel's points figure has to equal the award, not 11 - reps.

        Confirming writes a set of exactly (reps, threshold_at(reps)), and
        best_score_for_set takes the highest-point threshold that set meets --
        so a set at the 10RM weight also clears the 9RM rung whenever the 9RM
        target is <= the 10RM one. Promising 1 point and awarding 2 is the bug
        this pins.
        """
        _give_goal(
            participant,
            targets={
                **{rep: Decimal(100 - rep) for rep in range(1, 11)},
                10: Decimal("90.00"),
                9: Decimal(nine_rm),
            },
        )
        data = build_personal_data(user, challenge, participant)
        card = data["summary_cards"][0]
        ten_rm = next(t for t in card["manual_targets"] if t["rep_count"] == 10)
        # No current best, so the delta IS the points the set would earn.
        assert ten_rm["points_delta"] == expected_points

        submit_manual_lift(
            user=user,
            challenge=challenge,
            participant=participant,
            lift=LIFT,
            rep_count=10,
            performed_at=timezone.now().date(),
        )
        event = PointEarnEvent.objects.get(user=user, challenge=challenge, lift=LIFT)
        assert event.points_earned == ten_rm["points_delta"]

    def test_default_rep_count_falls_back_when_only_zero_point_history_exists(
        self, user, challenge, participant
    ):
        """A sub-threshold set is stored as a real zero-point PointEarnEvent
        with is_current_best=False, so a lifter who has logged sets but never
        scored still has no current-best row and takes the no-history
        fallback, same as a lift with no history at all.
        """
        _give_goal(participant, targets=_flat_targets())
        PointEarnEventFactory(
            user=user,
            challenge=challenge,
            lift=LIFT,
            points_earned=0,
            is_current_best=False,
            performed_at=timezone.now().date(),
        )
        data = build_personal_data(user, challenge, participant)
        card = data["summary_cards"][0]
        assert card["manual_default_rep_count"] == 10
        # Whatever it is, it has to be a rep count the carousel can land on.
        assert card["manual_default_rep_count"] in [
            t["rep_count"] for t in card["manual_targets"]
        ]
