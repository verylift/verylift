"""Tests for invite-link service functions (TASK-249)."""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from accounts.tests.factories import UserFactory
from challenges.models import Challenge, ChallengeInviteLink
from challenges.services import (
    create_challenge,
    current_invite_link,
    regenerate_invite_link,
    resolve_invite_token,
)
from challenges.tests.factories import ChallengeFactory, ChallengeInviteLinkFactory


@pytest.mark.django_db
class TestCurrentInviteLink:
    def test_returns_the_live_link(self):
        challenge = ChallengeFactory()
        link = ChallengeInviteLinkFactory(
            challenge=challenge,
            revoked_at=None,
            expires_at=timezone.now() + timedelta(days=1),
        )
        assert current_invite_link(challenge) == link

    def test_returns_none_when_no_link_exists(self):
        challenge = ChallengeFactory()
        assert current_invite_link(challenge) is None

    def test_returns_none_when_only_link_is_expired(self):
        challenge = ChallengeFactory()
        ChallengeInviteLinkFactory(
            challenge=challenge,
            revoked_at=None,
            expires_at=timezone.now() - timedelta(seconds=1),
        )
        assert current_invite_link(challenge) is None

    def test_returns_none_when_only_link_is_revoked(self):
        challenge = ChallengeFactory()
        ChallengeInviteLinkFactory(
            challenge=challenge,
            revoked_at=timezone.now(),
            expires_at=timezone.now() + timedelta(days=1),
        )
        assert current_invite_link(challenge) is None


@pytest.mark.django_db
class TestRegenerateInviteLink:
    def test_creates_a_usable_link(self):
        challenge = ChallengeFactory()
        user = UserFactory()
        link = regenerate_invite_link(challenge, user)
        assert link.challenge_id == challenge.pk
        assert link.created_by_id == user.id
        assert link.is_usable is True

    def test_revokes_the_incumbent_live_link(self):
        challenge = ChallengeFactory()
        user = UserFactory()
        first = regenerate_invite_link(challenge, user)
        second = regenerate_invite_link(challenge, user)

        first.refresh_from_db()
        assert first.revoked_at is not None
        assert second.revoked_at is None
        assert current_invite_link(challenge) == second

    def test_revokes_an_expired_but_unrevoked_incumbent(self):
        challenge = ChallengeFactory()
        user = UserFactory()
        stale = ChallengeInviteLinkFactory(
            challenge=challenge,
            revoked_at=None,
            expires_at=timezone.now() - timedelta(seconds=1),
        )
        fresh = regenerate_invite_link(challenge, user)

        stale.refresh_from_db()
        assert stale.revoked_at is not None
        assert fresh.revoked_at is None

    def test_honours_the_ttl_setting(self, settings):
        settings.CHALLENGES_INVITE_LINK_TTL_DAYS = 3
        challenge = ChallengeFactory()
        user = UserFactory()
        before = timezone.now()
        link = regenerate_invite_link(challenge, user)
        assert link.expires_at - before <= timedelta(days=3, seconds=5)
        assert link.expires_at - before >= timedelta(days=3, seconds=-5)

    def test_token_is_reasonably_long_and_unique(self):
        challenge = ChallengeFactory()
        user = UserFactory()
        first = regenerate_invite_link(challenge, user)
        second_challenge = ChallengeFactory()
        second = regenerate_invite_link(second_challenge, user)
        assert len(first.token) >= 32
        assert first.token != second.token


@pytest.mark.django_db
class TestResolveInviteToken:
    def test_unknown_token_returns_unknown(self):
        link, reason = resolve_invite_token("does-not-exist")
        assert link is None
        assert reason == "unknown"

    def test_usable_token_returns_link_and_no_reason(self):
        challenge = ChallengeFactory()
        expected = ChallengeInviteLinkFactory(
            challenge=challenge,
            revoked_at=None,
            expires_at=timezone.now() + timedelta(days=1),
        )
        link, reason = resolve_invite_token(expected.token)
        assert link == expected
        assert reason is None

    def test_expired_token_returns_expired(self):
        expected = ChallengeInviteLinkFactory(
            revoked_at=None,
            expires_at=timezone.now() - timedelta(seconds=1),
        )
        link, reason = resolve_invite_token(expected.token)
        assert link == expected
        assert reason == "expired"

    def test_revoked_token_returns_revoked(self):
        expected = ChallengeInviteLinkFactory(
            revoked_at=timezone.now(),
            expires_at=timezone.now() + timedelta(days=1),
        )
        link, reason = resolve_invite_token(expected.token)
        assert link == expected
        assert reason == "revoked"


@pytest.mark.django_db
class TestCreateChallengeMintsInviteLink:
    def test_create_challenge_mints_a_live_link(self):
        creator = UserFactory()
        challenge = create_challenge(
            creator,
            {
                "name": "Spring Challenge",
                "start_date": timezone.now().date(),
                "end_date": timezone.now().date() + timedelta(days=30),
                "history_window": Challenge.HistoryWindow.FROM_START,
                "plate_unit": Challenge.PlateUnit.LB,
                "smallest_plate_kg": Decimal("1.25"),
                "custom_lift_names": ["Bench Press"],
            },
        )
        link = current_invite_link(challenge)
        assert link is not None
        assert link.created_by_id == creator.id
        assert ChallengeInviteLink.objects.filter(challenge=challenge).count() == 1
