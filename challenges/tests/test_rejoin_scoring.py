"""Scoring-integrity regression tests for rejoining after a bail (TASK-152).

Rejoining resets ``joined_at`` to now so a FROM_JOIN eligibility window restarts
cleanly. LiftHistory accumulated during the bailed gap (lifts performed while not
competing) must not be retroactively scored into PointEarnEvents, while genuine
pre-bail scores stay on the record — mirroring the "your past entries stay
visible" bail design.
"""

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from django.test import Client
from django.urls import reverse

from challenges.models import Challenge
from challenges.tests.factories import ChallengeInviteLinkFactory
from liftosaur.tests.factories import LiftHistoryFactory
from scoring.models import PointEarnEvent
from scoring.services import score_pooled_history
from scoring.tests.factories import make_custom_scoring_setup

LIFT = "Back Squat"


@pytest.mark.django_db
def test_rejoin_does_not_resurrect_bailed_gap_history():
    user, challenge, participant = make_custom_scoring_setup(
        lift=LIFT,
        targets={rep: Decimal("100.00") for rep in range(1, 11)},
        history_window=Challenge.HistoryWindow.FROM_JOIN,
        start_date=date(2025, 1, 1),
        end_date=date(2026, 12, 31),
    )
    # invite_link_view requires a Liftosaur key before it will rejoin.
    user.liftosaur_api_key = "test-liftosaur-key"
    user.save(update_fields=["liftosaur_api_key"])
    challenge.status = Challenge.Status.ACTIVE
    challenge.save(update_fields=["status"])

    original_joined_at = datetime(2025, 1, 1, tzinfo=UTC)
    participant.joined_at = original_joined_at
    participant.save(update_fields=["joined_at"])

    # A qualifying set during the original membership → one PointEarnEvent.
    LiftHistoryFactory(
        user=user,
        lift=LIFT,
        performed_at=date(2025, 1, 2),
        reps=1,
        weight_kg=Decimal("100.00"),
    )
    score_pooled_history(user=user, challenge=challenge)
    assert PointEarnEvent.objects.filter(user=user, challenge=challenge).count() == 1

    # Bail, then accumulate a would-be-qualifying set during the bailed gap.
    participant.is_bailed = True
    participant.bailed_at = datetime(2025, 1, 3, tzinfo=UTC)
    participant.save(update_fields=["is_bailed", "bailed_at"])
    LiftHistoryFactory(
        user=user,
        lift=LIFT,
        performed_at=date(2025, 6, 1),
        reps=1,
        weight_kg=Decimal("100.00"),
    )

    # Rejoin via the real view so joined_at is reset to now.
    link = ChallengeInviteLinkFactory(challenge=challenge, created_by=challenge.creator)
    client = Client()
    client.force_login(user)
    response = client.get(reverse("challenges:invite-link", args=[link.token]))
    assert response.status_code == 302

    participant.refresh_from_db()
    assert participant.is_bailed is False
    assert participant.joined_at > original_joined_at

    score_pooled_history(user=user, challenge=challenge)

    # The bailed-gap set was never scored (its date precedes the reset window).
    assert not PointEarnEvent.objects.filter(
        user=user, challenge=challenge, performed_at=date(2025, 6, 1)
    ).exists()
    # The genuine pre-bail score survives untouched.
    assert PointEarnEvent.objects.filter(
        user=user, challenge=challenge, performed_at=date(2025, 1, 2)
    ).exists()
    assert PointEarnEvent.objects.filter(user=user, challenge=challenge).count() == 1
