"""Tests for the close_challenges management command (TASK-35, TASK-300)."""

import datetime
from unittest.mock import patch

import pytest
from django.core.management import call_command

from accounts.tests.factories import UserFactory
from challenges.models import Challenge
from challenges.tests.factories import ChallengeFactory

YESTERDAY = datetime.date.today() - datetime.timedelta(days=1)
TOMORROW = datetime.date.today() + datetime.timedelta(days=1)
CLOSE_TARGET = "challenges.management.commands.close_challenges.close_challenge"


@pytest.mark.django_db
class TestCloseChallengesCommand:
    def test_closes_all_qualifying_challenges(self):
        expired = [
            ChallengeFactory(status=Challenge.Status.ACTIVE, end_date=YESTERDAY)
            for _ in range(3)
        ]

        with patch("challenges.services.sync_user_lifts"):
            call_command("close_challenges")

        for challenge in expired:
            challenge.refresh_from_db()
            assert challenge.status == Challenge.Status.COMPLETED

    def test_skips_active_challenges_not_yet_ended(self):
        ongoing = ChallengeFactory(status=Challenge.Status.ACTIVE, end_date=TOMORROW)

        call_command("close_challenges")

        ongoing.refresh_from_db()
        assert ongoing.status == Challenge.Status.ACTIVE

    def test_skips_already_completed_challenges(self):
        completed = ChallengeFactory(
            status=Challenge.Status.COMPLETED, end_date=YESTERDAY
        )

        with patch(CLOSE_TARGET) as mock_close:
            call_command("close_challenges")

        mock_close.assert_not_called()
        completed.refresh_from_db()
        assert completed.status == Challenge.Status.COMPLETED

    def test_no_qualifying_challenges_exits_cleanly(self):
        ChallengeFactory(status=Challenge.Status.ACTIVE, end_date=TOMORROW)

        with patch(CLOSE_TARGET) as mock_close:
            call_command("close_challenges")

        mock_close.assert_not_called()

    def test_idempotent_across_repeated_runs(self):
        challenge = ChallengeFactory(status=Challenge.Status.ACTIVE, end_date=YESTERDAY)

        with patch("challenges.services.sync_user_lifts") as mock_sync:
            call_command("close_challenges")
            call_command("close_challenges")

        challenge.refresh_from_db()
        assert challenge.status == Challenge.Status.COMPLETED
        assert mock_sync.call_count == 0  # no accepted participants to sync


@pytest.mark.django_db
class TestCloseChallengesRespectsCreatorTimezone:
    """A creator's pinned timezone (TASK-300), not a bare UTC date compare,
    decides whether end_date has actually finished -- close_challenges,
    challenge_end_instant, and the invite-link default expiry all agree on
    this. ``NOW`` is patched so the UTC-vs-local boundary is deterministic
    regardless of when the suite actually runs."""

    NOW = datetime.datetime(2024, 6, 2, 12, 0, 0, tzinfo=datetime.UTC)
    TODAY = NOW.date()

    def test_ahead_of_utc_creator_closes_same_utc_day(self):
        """Pacific/Kiritimati is UTC+14 -- end_date's local end-of-day is
        09:59:59 UTC *that same date*, already behind NOW (15:00 UTC), even
        though end_date == today so a bare UTC date compare would keep it
        open until tomorrow."""
        creator = UserFactory(timezone="Pacific/Kiritimati")
        challenge = ChallengeFactory(
            status=Challenge.Status.ACTIVE, creator=creator, end_date=self.TODAY
        )

        with patch("django.utils.timezone.now", return_value=self.NOW):
            call_command("close_challenges")

        challenge.refresh_from_db()
        assert challenge.status == Challenge.Status.COMPLETED

    def test_behind_utc_creator_stays_open_past_utc_midnight(self):
        """Pacific/Niue is UTC-11 -- end_date's local end-of-day doesn't land
        until 10:59:59 UTC the *next* date, well after NOW, even though
        end_date == today has already started in UTC terms."""
        creator = UserFactory(timezone="Pacific/Niue")
        challenge = ChallengeFactory(
            status=Challenge.Status.ACTIVE, creator=creator, end_date=self.TODAY
        )

        with patch("django.utils.timezone.now", return_value=self.NOW):
            call_command("close_challenges")

        challenge.refresh_from_db()
        assert challenge.status == Challenge.Status.ACTIVE

    def test_creator_with_no_pinned_timezone_uses_utc(self):
        creator = UserFactory(timezone="")
        challenge = ChallengeFactory(
            status=Challenge.Status.ACTIVE,
            creator=creator,
            end_date=self.TODAY - datetime.timedelta(days=1),
        )

        with patch("django.utils.timezone.now", return_value=self.NOW):
            call_command("close_challenges")

        challenge.refresh_from_db()
        assert challenge.status == Challenge.Status.COMPLETED
