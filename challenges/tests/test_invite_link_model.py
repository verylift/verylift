"""Tests for ChallengeInviteLink (TASK-249)."""

from datetime import timedelta

import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone

from challenges.tests.factories import ChallengeFactory, ChallengeInviteLinkFactory


@pytest.mark.django_db
class TestChallengeInviteLinkModel:
    def test_has_uuid_pk(self):
        link = ChallengeInviteLinkFactory()
        assert link.pk is not None
        assert len(str(link.pk)) == 36

    def test_token_is_unique(self):
        link = ChallengeInviteLinkFactory()
        with pytest.raises(IntegrityError), transaction.atomic():
            ChallengeInviteLinkFactory(token=link.token)

    def test_is_expired_false_before_expiry(self):
        link = ChallengeInviteLinkFactory(expires_at=timezone.now() + timedelta(days=1))
        assert link.is_expired is False

    def test_is_expired_true_after_expiry(self):
        link = ChallengeInviteLinkFactory(
            expires_at=timezone.now() - timedelta(seconds=1)
        )
        assert link.is_expired is True

    def test_is_usable_true_when_live(self):
        link = ChallengeInviteLinkFactory(
            expires_at=timezone.now() + timedelta(days=1), revoked_at=None
        )
        assert link.is_usable is True

    def test_is_usable_false_when_expired(self):
        link = ChallengeInviteLinkFactory(
            expires_at=timezone.now() - timedelta(seconds=1), revoked_at=None
        )
        assert link.is_usable is False

    def test_is_usable_false_when_revoked(self):
        link = ChallengeInviteLinkFactory(
            expires_at=timezone.now() + timedelta(days=1),
            revoked_at=timezone.now(),
        )
        assert link.is_usable is False

    def test_one_live_link_per_challenge_constraint(self):
        challenge = ChallengeFactory()
        ChallengeInviteLinkFactory(challenge=challenge, revoked_at=None)
        with pytest.raises(IntegrityError), transaction.atomic():
            ChallengeInviteLinkFactory(challenge=challenge, revoked_at=None)

    def test_revoked_link_does_not_block_a_new_live_one(self):
        challenge = ChallengeFactory()
        ChallengeInviteLinkFactory(challenge=challenge, revoked_at=timezone.now())
        # A second revoked (or newly-live) row for the same challenge is fine
        # once the incumbent is revoked -- the partial constraint only
        # applies to revoked_at IS NULL rows.
        second = ChallengeInviteLinkFactory(challenge=challenge, revoked_at=None)
        assert second.pk is not None

    def test_str_names_the_challenge(self):
        challenge = ChallengeFactory(name="Winter Showdown")
        link = ChallengeInviteLinkFactory(challenge=challenge)
        assert "Winter Showdown" in str(link)
