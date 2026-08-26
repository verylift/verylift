"""Tests for challenges.services.sync_and_score's Hevy failure handling (TASK-318)
and its Liftosaur-then-Hevy sync ordering (TASK-319)."""

import urllib.error
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from accounts.tests.factories import UserFactory
from challenges.services import sync_and_score
from challenges.tests.factories import ChallengeFactory
from hevy_api.services import HISTORY_BACKFILL_DAYS

pytestmark = pytest.mark.django_db


def _stub_client(events_side_effect):
    client = MagicMock()
    client.get_workout_events.side_effect = events_side_effect
    client.get_workouts.return_value = {"page": 1, "page_count": 1, "workouts": []}
    return client


class TestSyncAndScoreHevyNetworkFailure:
    def test_url_error_does_not_propagate(self):
        user = UserFactory(hevy_api_key="key")
        challenge = ChallengeFactory()
        client = _stub_client(urllib.error.URLError("no network"))

        with patch("hevy_api.services.HevyClient", return_value=client):
            sync_and_score(user, challenge)

    def test_timeout_error_does_not_propagate(self):
        user = UserFactory(hevy_api_key="key")
        challenge = ChallengeFactory()
        client = _stub_client(TimeoutError("timed out"))

        with patch("hevy_api.services.HevyClient", return_value=client):
            sync_and_score(user, challenge)


class TestSyncAndScoreOrdering:
    def test_liftosaur_pull_does_not_truncate_hevy_first_backfill(self):
        """TASK-319: sync_and_score runs the Liftosaur pull first, which writes
        rows and could move a shared watermark before the Hevy pull ever reads
        it. A dual-connected user's first-ever Hevy sync must still request the
        full backfill window, not since=today derived from the Liftosaur pull
        that just ran moments earlier in the same call."""
        user = UserFactory(liftosaur_api_key="key", hevy_api_key="key")
        challenge = ChallengeFactory()

        liftosaur_client = MagicMock()
        today = datetime.now(tz=UTC).date().isoformat()
        liftosaur_client.get_history.side_effect = [
            (
                [
                    f'{today}T12:00:00Z / program: "P" / exercises: {{\n'
                    "  Back Squat / 1x5 100kg\n"
                    "}"
                ],
                False,
                None,
            )
        ]
        liftosaur_client.get_weight_measurements.side_effect = [([], False, None)]

        hevy_client = _stub_client([{"page": 1, "page_count": 1, "events": []}])

        with (
            patch("liftosaur.services.LiftosaurClient", return_value=liftosaur_client),
            patch("hevy_api.services.HevyClient", return_value=hevy_client),
        ):
            sync_and_score(user, challenge)

        _, kwargs = hevy_client.get_workout_events.call_args
        expected_start = (
            datetime.now(tz=UTC) - timedelta(days=HISTORY_BACKFILL_DAYS)
        ).date().isoformat() + "T00:00:00Z"
        assert kwargs["since"] == expected_start
