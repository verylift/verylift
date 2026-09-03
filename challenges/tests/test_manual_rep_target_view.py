"""Tests for challenges.views.manual_rep_target_view (issue #85 follow-up)."""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from accounts.tests.factories import UserFactory
from challenges.models import Challenge, ChallengeParticipant
from challenges.tests.factories import (
    ChallengeParticipantFactory,
    RepTargetGoalFactory,
    RepTargetGoalTargetFactory,
    make_rep_target_challenge,
)
from core.models import LiftHistory, LiftSource
from scoring.models import PointEarnEvent

pytestmark = pytest.mark.django_db

LIFT = "Push Up"
HX = {"HTTP_HX_REQUEST": "true"}


def _accept(participant):
    participant.invite_status = ChallengeParticipant.InviteStatus.ACCEPTED
    participant.joined_at = timezone.now() - timedelta(days=3650)
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
    return make_rep_target_challenge(lifts=[LIFT], creator=user)


@pytest.fixture
def participant(user, challenge):
    p = ChallengeParticipantFactory(user=user, challenge=challenge)
    return _accept(p)


@pytest.fixture
def participant_with_goal(participant):
    _give_goal(participant, target_weight=Decimal("0"), target_reps=20)
    return participant


def _post(client, challenge, data, **extra):
    return client.post(
        reverse("challenges:manual-rep-target", args=[challenge.pk]), data, **extra
    )


class TestManualRepTargetViewAuth:
    def test_requires_login(self, challenge):
        client = Client()
        response = _post(client, challenge, {})
        assert response.status_code == 302
        assert "/login" in response.url or "login" in response.url

    def test_requires_membership(self, challenge):
        outsider = UserFactory()
        client = Client()
        client.force_login(outsider)
        response = _post(
            client,
            challenge,
            {"lift": LIFT, "rep_count": "10", "performed_at": "2025-06-01"},
        )
        assert response.status_code == 403

    def test_requires_goal_configured(self, user, challenge, participant):
        client = Client()
        client.force_login(user)
        response = _post(
            client,
            challenge,
            {"lift": LIFT, "rep_count": "10", "performed_at": "2025-06-01"},
        )
        assert response.status_code == 403


class TestManualRepTargetViewValidation:
    def test_invalid_rep_count(self, user, challenge, participant_with_goal):
        client = Client()
        client.force_login(user)
        response = _post(
            client,
            challenge,
            {"lift": LIFT, "rep_count": "not-a-number", "performed_at": "2025-06-01"},
        )
        assert response.status_code == 400

    def test_zero_rep_count_rejected(self, user, challenge, participant_with_goal):
        client = Client()
        client.force_login(user)
        response = _post(
            client,
            challenge,
            {"lift": LIFT, "rep_count": "0", "performed_at": "2025-06-01"},
        )
        assert response.status_code == 400

    def test_rep_count_above_max_rejected(self, user, challenge, participant_with_goal):
        client = Client()
        client.force_login(user)
        response = _post(
            client,
            challenge,
            {"lift": LIFT, "rep_count": "1000", "performed_at": "2025-06-01"},
        )
        assert response.status_code == 400

    def test_invalid_date(self, user, challenge, participant_with_goal):
        client = Client()
        client.force_login(user)
        response = _post(
            client,
            challenge,
            {"lift": LIFT, "rep_count": "10", "performed_at": "not-a-date"},
        )
        assert response.status_code == 400

    def test_future_date_rejected(self, user, challenge, participant_with_goal):
        client = Client()
        client.force_login(user)
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        response = _post(
            client,
            challenge,
            {"lift": LIFT, "rep_count": "10", "performed_at": tomorrow},
        )
        assert response.status_code == 400

    def test_unknown_lift_rejected(self, user, challenge, participant_with_goal):
        client = Client()
        client.force_login(user)
        response = _post(
            client,
            challenge,
            {"lift": "Deadlift", "rep_count": "10", "performed_at": "2025-06-01"},
        )
        assert response.status_code == 400
        assert not LiftHistory.objects.filter(lift="Deadlift").exists()


class TestManualRepTargetViewSuccess:
    def test_logs_a_set_and_returns_card_fragment(
        self, user, challenge, participant_with_goal
    ):
        client = Client()
        client.force_login(user)
        response = _post(
            client,
            challenge,
            {"lift": LIFT, "rep_count": "10", "performed_at": "2025-06-01"},
            **HX,
        )
        assert response.status_code == 200
        content = response.content.decode()
        assert "summary-card-" in content
        assert LIFT in content

        history_row = LiftHistory.objects.get(user=user, lift=LIFT)
        assert history_row.source == LiftSource.MANUAL
        assert history_row.weight_kg == Decimal("0")
        assert history_row.reps == 10

        event = PointEarnEvent.objects.get(
            user=user, challenge=challenge, lift=LIFT, is_current_best=True
        )
        assert event.source == LiftSource.MANUAL
        assert event.points_earned == 5  # floor(10 * 10 / 20)

    def test_non_beating_submission_is_rejected(
        self, user, challenge, participant_with_goal
    ):
        client = Client()
        client.force_login(user)
        _post(
            client,
            challenge,
            {"lift": LIFT, "rep_count": "20", "performed_at": "2025-06-01"},
            **HX,
        )
        response = _post(
            client,
            challenge,
            {"lift": LIFT, "rep_count": "5", "performed_at": "2025-06-02"},
            **HX,
        )
        assert response.status_code == 400
        assert not LiftHistory.objects.filter(
            user=user, lift=LIFT, performed_at=date(2025, 6, 2)
        ).exists()


class TestManualRepTargetViewTerminalChallenge:
    """The REP_TARGET sibling of TestManualLiftViewTerminalChallenge: a
    finished challenge takes no self-reported sets, and nothing is written
    before the refusal."""

    @pytest.mark.parametrize(
        "status", [Challenge.Status.COMPLETED, Challenge.Status.CANCELLED]
    )
    def test_rejected_without_writing_history(
        self, user, challenge, participant_with_goal, status
    ):
        challenge.status = status
        challenge.save(update_fields=["status"])
        client = Client()
        client.force_login(user)

        response = _post(
            client,
            challenge,
            {"lift": LIFT, "rep_count": "10", "performed_at": "2025-06-01"},
            **HX,
        )

        assert response.status_code == 400
        assert not LiftHistory.objects.filter(user=user, lift=LIFT).exists()
        assert not PointEarnEvent.objects.filter(
            user=user, challenge=challenge
        ).exists()
