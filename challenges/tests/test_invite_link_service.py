"""Tests for invite-link service functions (TASK-249, TASK-300)."""

from datetime import UTC, datetime, time, timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from accounts.tests.factories import UserFactory
from challenges.models import Challenge, ChallengeInviteLink
from challenges.services import (
    challenge_timezone,
    create_challenge,
    current_invite_link,
    record_invite_link_use,
    regenerate_invite_link,
    resolve_invite_token,
    update_invite_link,
)
from challenges.tests.factories import ChallengeFactory, ChallengeInviteLinkFactory


@pytest.mark.django_db
class TestChallengeTimezone:
    """Priority ladder: pinned User.timezone, then detected_timezone, then
    UTC (TASK-300)."""

    def test_pinned_timezone_wins(self):
        creator = UserFactory(
            timezone="America/Toronto", detected_timezone="Asia/Tokyo"
        )
        challenge = ChallengeFactory(creator=creator)
        assert str(challenge_timezone(challenge)) == "America/Toronto"

    def test_detected_timezone_used_when_nothing_pinned(self):
        creator = UserFactory(timezone="", detected_timezone="Asia/Tokyo")
        challenge = ChallengeFactory(creator=creator)
        assert str(challenge_timezone(challenge)) == "Asia/Tokyo"

    def test_falls_back_to_utc_when_neither_is_set(self):
        creator = UserFactory(timezone="", detected_timezone="")
        challenge = ChallengeFactory(creator=creator)
        assert str(challenge_timezone(challenge)) == "UTC"

    def test_invalid_pinned_timezone_falls_through_to_detected(self):
        creator = UserFactory(timezone="Not/AZone", detected_timezone="Asia/Tokyo")
        challenge = ChallengeFactory(creator=creator)
        assert str(challenge_timezone(challenge)) == "Asia/Tokyo"

    def test_creator_less_challenge_uses_utc(self):
        assert str(challenge_timezone(Challenge(end_date=timezone.now().date()))) == (
            "UTC"
        )


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
class TestUpdateInviteLink:
    def test_updates_expiry_and_max_uses_on_the_same_row(self):
        challenge = ChallengeFactory()
        link = ChallengeInviteLinkFactory(
            challenge=challenge,
            revoked_at=None,
            expires_at=timezone.now() + timedelta(days=7),
            max_uses=None,
        )
        new_expiry = timezone.now() + timedelta(days=2)

        updated = update_invite_link(link, expires_at=new_expiry, max_uses=3)

        assert updated.pk == link.pk
        assert updated.token == link.token
        assert updated.expires_at == new_expiry
        assert updated.max_uses == 3
        link.refresh_from_db()
        assert link.expires_at == new_expiry
        assert link.max_uses == 3

    def test_blank_expiry_falls_back_to_challenge_end_date(self):
        challenge = ChallengeFactory(
            end_date=(timezone.now() + timedelta(days=30)).date()
        )
        link = ChallengeInviteLinkFactory(
            challenge=challenge,
            revoked_at=None,
            expires_at=timezone.now() + timedelta(days=2),
        )

        updated = update_invite_link(link, expires_at=None, max_uses=None)

        expected = datetime.combine(challenge.end_date, time.max, tzinfo=UTC)
        assert updated.expires_at == expected
        assert updated.max_uses is None

    def test_does_not_reset_use_count(self):
        challenge = ChallengeFactory()
        link = ChallengeInviteLinkFactory(
            challenge=challenge, revoked_at=None, use_count=4
        )

        updated = update_invite_link(link, expires_at=None, max_uses=10)

        assert updated.use_count == 4


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

    def test_defaults_to_end_of_day_of_the_challenges_end_date(self):
        challenge = ChallengeFactory(
            end_date=(timezone.now() + timedelta(days=10)).date()
        )
        user = UserFactory()
        link = regenerate_invite_link(challenge, user)
        assert link.expires_at == datetime.combine(
            challenge.end_date, time.max, tzinfo=UTC
        )

    def test_custom_expires_at_override_is_used_verbatim(self):
        challenge = ChallengeFactory(
            end_date=(timezone.now() + timedelta(days=10)).date()
        )
        user = UserFactory()
        custom = timezone.now() + timedelta(hours=2)
        link = regenerate_invite_link(challenge, user, expires_at=custom)
        assert link.expires_at == custom

    def test_max_uses_override_is_stored(self):
        challenge = ChallengeFactory()
        user = UserFactory()
        link = regenerate_invite_link(challenge, user, max_uses=5)
        assert link.max_uses == 5
        assert link.use_count == 0

    def test_max_uses_defaults_to_unlimited(self):
        challenge = ChallengeFactory()
        user = UserFactory()
        link = regenerate_invite_link(challenge, user)
        assert link.max_uses is None

    def test_fresh_link_does_not_carry_over_incumbents_use_count(self):
        challenge = ChallengeFactory()
        user = UserFactory()
        ChallengeInviteLinkFactory(
            challenge=challenge,
            revoked_at=None,
            expires_at=timezone.now() + timedelta(days=1),
            max_uses=5,
            use_count=3,
        )
        fresh = regenerate_invite_link(challenge, user, max_uses=5)
        assert fresh.use_count == 0

    def test_token_is_reasonably_long_and_unique(self):
        challenge = ChallengeFactory()
        user = UserFactory()
        first = regenerate_invite_link(challenge, user)
        second_challenge = ChallengeFactory()
        second = regenerate_invite_link(second_challenge, user)
        assert len(first.token) >= 6
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

    def test_exhausted_token_returns_exhausted(self):
        expected = ChallengeInviteLinkFactory(
            revoked_at=None,
            expires_at=timezone.now() + timedelta(days=1),
            max_uses=2,
            use_count=2,
        )
        link, reason = resolve_invite_token(expected.token)
        assert link == expected
        assert reason == "exhausted"

    def test_expired_takes_precedence_over_exhausted(self):
        expected = ChallengeInviteLinkFactory(
            revoked_at=None,
            expires_at=timezone.now() - timedelta(seconds=1),
            max_uses=2,
            use_count=2,
        )
        link, reason = resolve_invite_token(expected.token)
        assert link == expected
        assert reason == "expired"

    def test_below_max_uses_returns_no_reason(self):
        expected = ChallengeInviteLinkFactory(
            revoked_at=None,
            expires_at=timezone.now() + timedelta(days=1),
            max_uses=2,
            use_count=1,
        )
        link, reason = resolve_invite_token(expected.token)
        assert link == expected
        assert reason is None


@pytest.mark.django_db
class TestRecordInviteLinkUse:
    def test_increments_use_count(self):
        link = ChallengeInviteLinkFactory(use_count=0)
        record_invite_link_use(link)
        link.refresh_from_db()
        assert link.use_count == 1

    def test_increments_from_a_nonzero_starting_count(self):
        link = ChallengeInviteLinkFactory(use_count=4)
        record_invite_link_use(link)
        link.refresh_from_db()
        assert link.use_count == 5


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
