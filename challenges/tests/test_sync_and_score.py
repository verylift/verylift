"""Tests for challenges.services.sync_and_score's Hevy failure handling (TASK-318)."""

import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from accounts.tests.factories import UserFactory
from challenges.services import sync_and_score
from challenges.tests.factories import ChallengeFactory

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
