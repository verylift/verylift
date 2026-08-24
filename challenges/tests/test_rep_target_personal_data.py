"""Tests for build_rep_target_personal_data (issue #85) -- the REP_TARGET
sibling of build_personal_data: a progress-bar-per-lift summary instead of a
rep-max ladder, reusing Classic's close-to-goal/endgame flagging."""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.test import override_settings
from django.utils import timezone

from accounts.tests.factories import UserFactory
from challenges.models import Challenge, ChallengeParticipant
from challenges.services import build_personal_data, build_rep_target_personal_data
from challenges.tests.factories import (
    ChallengeParticipantFactory,
    RepTargetGoalFactory,
    RepTargetGoalTargetFactory,
    make_rep_target_challenge,
)
from liftosaur.tests.factories import LiftHistoryFactory
from scoring.tests.factories import PointEarnEventFactory

pytestmark = pytest.mark.django_db

LIFT = "Push Up"


def _setup(
    *, target_weight=Decimal("0"), target_reps=20, start_date=None, end_date=None
):
    kwargs = {"history_window": Challenge.HistoryWindow.FROM_START}
    if start_date is not None:
        kwargs["start_date"] = start_date
    if end_date is not None:
        kwargs["end_date"] = end_date
    challenge = make_rep_target_challenge(lifts=[LIFT], **kwargs)
    user = UserFactory(unit_preference="kg")
    participant = ChallengeParticipantFactory(
        challenge=challenge,
        user=user,
        invite_status=ChallengeParticipant.InviteStatus.ACCEPTED,
        joined_at=timezone.now(),
    )
    goal = RepTargetGoalFactory(participant=participant)
    RepTargetGoalTargetFactory(
        goal=goal, lift=LIFT, target_weight=target_weight, target_reps=target_reps
    )
    participant.rep_target_goal = goal
    participant.save(update_fields=["rep_target_goal"])
    return user, challenge, participant


class TestBuildRepTargetPersonalData:
    def test_dispatches_from_build_personal_data_for_rep_target_mode(self):
        user, challenge, participant = _setup()
        result = build_personal_data(user, challenge, participant)
        assert result is not None
        assert result["summary_cards"][0]["lift"] == LIFT

    def test_returns_none_without_a_configured_goal(self):
        challenge = make_rep_target_challenge(lifts=[LIFT])
        participant = ChallengeParticipantFactory(
            challenge=challenge,
            invite_status=ChallengeParticipant.InviteStatus.ACCEPTED,
            joined_at=timezone.now(),
        )
        assert (
            build_rep_target_personal_data(participant.user, challenge, participant)
            is None
        )

    def test_scored_card_shows_progress_and_points(self):
        user, challenge, participant = _setup(
            target_weight=Decimal("0"), target_reps=20
        )
        PointEarnEventFactory(
            user=user,
            challenge=challenge,
            lift=LIFT,
            reps=12,
            weight=Decimal("0.00"),
            points_earned=6,
            is_current_best=True,
        )
        data = build_rep_target_personal_data(user, challenge, participant)
        card = data["summary_cards"][0]
        assert card["state"] == "scored"
        assert card["points_earned"] == 6
        assert card["progress_reps"] == 12
        assert card["target_reps"] == 20

    def test_scored_card_below_ten_points_gets_a_reps_gap(self):
        user, challenge, participant = _setup(
            target_weight=Decimal("0"), target_reps=20
        )
        PointEarnEventFactory(
            user=user,
            challenge=challenge,
            lift=LIFT,
            reps=12,
            weight=Decimal("0.00"),
            points_earned=6,
            is_current_best=True,
        )
        data = build_rep_target_personal_data(user, challenge, participant)
        card = data["summary_cards"][0]
        # 14/20 is the first rep count that scores more than the current 6
        # points (floor(10*14/20) = 7); two more reps than the 12 performed.
        assert card["reps_gap"] == 2

    def test_endgame_nudge_fires_on_the_remaining_reps_gap(self):
        # Regression: the fraction was next_reps/target_reps -- the TOTAL the
        # next point requires, >= ~0.2 on any scored card -- so the endgame
        # nudge could never fire in this mode, and was worst exactly when
        # closest (19/20 reps computed 1.0). It must be the REMAINING gap.
        today = timezone.localdate()
        user, challenge, participant = _setup(
            target_weight=Decimal("0"),
            target_reps=20,
            start_date=today - timedelta(days=30),
            end_date=today + timedelta(days=3),
        )
        PointEarnEventFactory(
            user=user,
            challenge=challenge,
            lift=LIFT,
            reps=19,
            weight=Decimal("0.00"),
            points_earned=9,
            is_current_best=True,
        )
        data = build_rep_target_personal_data(user, challenge, participant)
        card = data["summary_cards"][0]
        # One rep short of the 20 that earn the tenth point: 1/20.
        assert card["next_point_gap_fraction"] == Decimal("0.05")
        assert card["endgame_suggestion"] == "next_point"

    def test_maxed_card_has_no_reps_gap(self):
        user, challenge, participant = _setup(
            target_weight=Decimal("0"), target_reps=20
        )
        PointEarnEventFactory(
            user=user,
            challenge=challenge,
            lift=LIFT,
            reps=20,
            weight=Decimal("0.00"),
            points_earned=10,
            is_current_best=True,
        )
        data = build_rep_target_personal_data(user, challenge, participant)
        card = data["summary_cards"][0]
        assert "reps_gap" not in card

    def test_no_points_card_shows_weight_gap_from_window_history(self):
        user, challenge, participant = _setup(
            target_weight=Decimal("10"), target_reps=20, start_date=date(2020, 1, 1)
        )
        LiftHistoryFactory(
            user=user,
            lift=LIFT,
            performed_at=date(2024, 1, 1),
            reps=20,
            weight_kg=Decimal("5"),
        )
        data = build_rep_target_personal_data(user, challenge, participant)
        card = data["summary_cards"][0]
        assert card["state"] == "no_points"
        assert card["weight_gap"] == Decimal("5.0")

    def test_no_data_card_when_nothing_logged(self):
        user, challenge, participant = _setup()
        data = build_rep_target_personal_data(user, challenge, participant)
        card = data["summary_cards"][0]
        assert card["state"] == "no_data"

    @override_settings(CHALLENGES_CLOSE_TO_GOAL_GAP_FRACTION=0.5)
    def test_close_to_goal_flag_reuses_classic_tuning_constant(self):
        # Issue #85 open question #1: Rep Target reuses Classic's tuning
        # constants rather than a separate set -- this proves the same
        # setting actually drives the Rep Target flag.
        user, challenge, participant = _setup(
            target_weight=Decimal("10"), target_reps=20, start_date=date(2020, 1, 1)
        )
        LiftHistoryFactory(
            user=user,
            lift=LIFT,
            performed_at=date(2024, 1, 1),
            reps=20,
            weight_kg=Decimal("9"),
        )
        data = build_rep_target_personal_data(user, challenge, participant)
        card = data["summary_cards"][0]
        assert card.get("close_to_goal") is True
