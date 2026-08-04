"""Tests for the close_challenges management command (TASK-35)."""

import datetime
from unittest.mock import patch

import pytest
from django.core.management import call_command

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
