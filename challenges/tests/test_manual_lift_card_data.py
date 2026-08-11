"""Tests for the self-report carousel data build_personal_data attaches to
each summary card (TASK-25): manual_targets and manual_default_rep_count."""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from accounts.tests.factories import UserFactory
from challenges.models import Challenge, ChallengeParticipant
from challenges.services import build_personal_data
from challenges.tests.factories import (
    ChallengeParticipantFactory,
    CustomGoalFactory,
    CustomGoalTargetFactory,
    make_custom_challenge,
)
from liftosaur.tests.factories import LiftHistoryFactory
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

    def test_default_rep_count_uses_most_recent_event_not_current_best(
        self, user, challenge, participant
    ):
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
        assert card["manual_default_rep_count"] == 7
