"""Tests for challenges.views.manual_lift_view (TASK-25)."""

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
    CustomGoalFactory,
    CustomGoalTargetFactory,
    make_custom_challenge,
)
from core.models import LiftHistory, LiftSource
from scoring.models import PointEarnEvent

pytestmark = pytest.mark.django_db

LIFT = "Bench Press"
HX = {"HTTP_HX_REQUEST": "true"}


def _accept(participant):
    participant.invite_status = ChallengeParticipant.InviteStatus.ACCEPTED
    # Far enough back that the fixed 2025-06-01 dates used throughout this
    # file always fall inside the FROM_JOIN scoring window regardless of when
    # the suite actually runs.
    participant.joined_at = timezone.now() - timedelta(days=3650)
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
    return make_custom_challenge(lifts=[LIFT], creator=user)


@pytest.fixture
def participant(user, challenge):
    p = ChallengeParticipantFactory(user=user, challenge=challenge)
    return _accept(p)


def _full_targets(overrides):
    """A complete 1RM..10RM target table -- the real invariant every saved
    CustomGoal has (save_custom_goal enforces full coverage); best_score_for_set
    assumes every rep count 1..10 resolves."""
    targets = {rep: Decimal(100 - rep) for rep in range(1, 11)}
    targets.update(overrides)
    return targets


@pytest.fixture
def participant_with_goal(participant):
    _give_goal(
        participant, targets=_full_targets({3: Decimal("90.00"), 8: Decimal("60.00")})
    )
    return participant


def _post(client, challenge, data, **extra):
    return client.post(
        reverse("challenges:manual-lift", args=[challenge.pk]), data, **extra
    )


class TestManualLiftViewAuth:
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
            {"lift": LIFT, "rep_count": "8", "performed_at": "2025-06-01"},
        )
        assert response.status_code == 403

    def test_requires_goal_configured(self, user, challenge, participant):
        client = Client()
        client.force_login(user)
        response = _post(
            client,
            challenge,
            {"lift": LIFT, "rep_count": "8", "performed_at": "2025-06-01"},
        )
        assert response.status_code == 403


class TestManualLiftViewValidation:
    def test_invalid_rep_count(self, user, challenge, participant_with_goal):
        client = Client()
        client.force_login(user)
        response = _post(
            client,
            challenge,
            {"lift": LIFT, "rep_count": "not-a-number", "performed_at": "2025-06-01"},
        )
        assert response.status_code == 400

    def test_invalid_date(self, user, challenge, participant_with_goal):
        client = Client()
        client.force_login(user)
        response = _post(
            client,
            challenge,
            {"lift": LIFT, "rep_count": "8", "performed_at": "not-a-date"},
        )
        assert response.status_code == 400

    def test_future_date_rejected(self, user, challenge, participant_with_goal):
        client = Client()
        client.force_login(user)
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        response = _post(
            client,
            challenge,
            {"lift": LIFT, "rep_count": "8", "performed_at": tomorrow},
        )
        assert response.status_code == 400

    def test_unknown_lift_rep_count_combination(
        self, user, challenge, participant_with_goal
    ):
        client = Client()
        client.force_login(user)
        response = _post(
            client,
            challenge,
            {"lift": LIFT, "rep_count": "11", "performed_at": "2025-06-01"},
        )
        assert response.status_code == 400
        assert not LiftHistory.objects.filter(lift=LIFT).exists()


class TestManualLiftViewSuccess:
    def test_logs_a_set_and_returns_card_fragment(
        self, user, challenge, participant_with_goal
    ):
        client = Client()
        client.force_login(user)
        response = _post(
            client,
            challenge,
            {"lift": LIFT, "rep_count": "8", "performed_at": "2025-06-01"},
            **HX,
        )
        assert response.status_code == 200
        content = response.content.decode()
        assert "summary-card-" in content
        assert LIFT in content

        history_row = LiftHistory.objects.get(user=user, lift=LIFT)
        assert history_row.source == LiftSource.MANUAL
        assert history_row.weight_kg == Decimal("60.00")

        event = PointEarnEvent.objects.get(
            user=user, challenge=challenge, lift=LIFT, is_current_best=True
        )
        assert event.source == LiftSource.MANUAL

    def test_non_beating_submission_is_rejected(
        self, user, challenge, participant_with_goal
    ):
        """The carousel disables entries that cannot raise the score, so this
        only arrives from a stale card or a hand-made request -- either way it
        is refused rather than written, which is what stops a no-op set being
        reported back as an improvement."""
        client = Client()
        client.force_login(user)
        # First establish a heavier current best.
        _post(
            client,
            challenge,
            {"lift": LIFT, "rep_count": "3", "performed_at": "2025-06-01"},
            **HX,
        )
        # Then submit a lighter one that cannot beat it.
        response = _post(
            client,
            challenge,
            {"lift": LIFT, "rep_count": "8", "performed_at": "2025-06-02"},
            **HX,
        )
        assert response.status_code == 400
        assert not LiftHistory.objects.filter(
            user=user, lift=LIFT, performed_at=date(2025, 6, 2)
        ).exists()


class TestManualLiftViewTerminalChallenge:
    """A COMPLETED/CANCELLED challenge is read-only (issue: completed
    challenges still accepted manual registrations).

    The detail page stops rendering the card's self-report face entirely, so
    these arrive only from a page that was open when the challenge closed, or
    from a hand-made request. Both halves matter: the 400 AND the absence of a
    LiftHistory row -- the ledger lock in process_scored_set sits downstream of
    that write, so before this guard the set was persisted and then scored
    nothing, reporting "Logged 0 points" back to the lifter.
    """

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
            {"lift": LIFT, "rep_count": "8", "performed_at": "2025-06-01"},
            **HX,
        )

        assert response.status_code == 400
        assert not LiftHistory.objects.filter(user=user, lift=LIFT).exists()
        assert not PointEarnEvent.objects.filter(
            user=user, challenge=challenge
        ).exists()
