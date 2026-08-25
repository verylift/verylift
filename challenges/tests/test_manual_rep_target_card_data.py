"""Tests for the self-report carousel data build_rep_target_personal_data
attaches to each REP_TARGET summary card (issue #85 follow-up): manual_targets
and manual_default_rep_count -- the sibling of test_manual_lift_card_data.py.
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
    def test_ten_targets_present_in_ascending_point_order(
        self, user, challenge, participant
    ):
        _give_goal(participant, target_weight=Decimal("0"), target_reps=20)
        data = build_rep_target_personal_data(user, challenge, participant)
        card = data["summary_cards"][0]
        assert len(card["manual_targets"]) == 10
        assert [t["points"] for t in card["manual_targets"]] == list(range(1, 11))
        assert [t["rep_count"] for t in card["manual_targets"]] == [
            2,
            4,
            6,
            8,
            10,
            12,
            14,
            16,
            18,
            20,
        ]

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
        assert card["manual_default_rep_count"] == 10  # tier 5 -> 10 reps

    def test_default_rep_count_falls_back_to_easiest_tier_with_no_history(
        self, user, challenge, participant
    ):
        _give_goal(participant, target_weight=Decimal("0"), target_reps=20)
        data = build_rep_target_personal_data(user, challenge, participant)
        card = data["summary_cards"][0]
        assert card["state"] == "no_data"
        assert card["manual_default_rep_count"] == 2  # tier 1 -> 2 reps

    def test_default_rep_count_ignores_zero_point_events(
        self, user, challenge, participant
    ):
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

    def test_default_rep_count_uses_most_recent_event_not_current_best(
        self, user, challenge, participant
    ):
        _give_goal(participant, target_weight=Decimal("0"), target_reps=20)
        PointEarnEventFactory(
            user=user,
            challenge=challenge,
            lift=LIFT,
            points_earned=8,
            is_current_best=True,
            performed_at=timezone.now().date() - timedelta(days=10),
        )
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
        assert card["manual_default_rep_count"] == 8  # tier 4 -> 8 reps

    def test_points_delta_matches_what_confirming_actually_scores(
        self, user, challenge, participant
    ):
        """A small target_reps can make several point tiers share the same
        minimum rep count (documented on _rep_target_point_columns), so
        confirming a "cheap" tier can score more than its own label promises.
        This pins the carousel's points_delta to the real scorer, not the
        column's nominal points label.
        """
        _give_goal(participant, target_weight=Decimal("0"), target_reps=5)
        data = build_rep_target_personal_data(user, challenge, participant)
        card = data["summary_cards"][0]
        one_rep_entry = next(t for t in card["manual_targets"] if t["rep_count"] == 1)
        # target_reps=5: 1 rep already scores floor(10*1/5) == 2 points, not 1.
        assert one_rep_entry["points_delta"] == 2

        submit_manual_rep_target_set(
            user=user,
            challenge=challenge,
            participant=participant,
            lift=LIFT,
            rep_count=1,
            performed_at=timezone.now().date(),
        )
        event = PointEarnEvent.objects.get(user=user, challenge=challenge, lift=LIFT)
        assert event.points_earned == one_rep_entry["points_delta"]

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
