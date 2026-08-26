"""Tests for the Hevy connect settings flow's background backfill trigger
(TASK-320)."""

from unittest.mock import patch

import pytest
from django.test import Client
from django.urls import reverse

from accounts.tests.factories import UserFactory


@pytest.fixture
def user(db):
    return UserFactory()


@pytest.fixture
def authed_client(user):
    c = Client()
    c.force_login(user)
    return c


class TestHevyKeySettingsBackfillTrigger:
    def test_saving_a_key_triggers_backfill_off_the_request(self, authed_client, user):
        """Saving a Hevy key must kick off the one-time backfill in the
        background, not run it inline -- that's the whole point of TASK-320's
        AC1. A regression back to a direct sync_user_lifts() call here would
        put the full HISTORY_BACKFILL_DAYS pull back on the request path."""
        url = reverse("accounts:settings")
        with patch("accounts.views.trigger_hevy_lift_history_backfill") as mock_trigger:
            authed_client.post(
                url, {"form_name": "hevy_key", "hevy_api_key": "my-test-key"}
            )

        mock_trigger.assert_called_once_with(user)

    def test_empty_submission_does_not_save_or_trigger(self, authed_client, user):
        url = reverse("accounts:settings")
        with patch("accounts.views.trigger_hevy_lift_history_backfill") as mock_trigger:
            authed_client.post(url, {"form_name": "hevy_key", "hevy_api_key": ""})

        user.refresh_from_db()
        assert user.hevy_api_key is None
        mock_trigger.assert_not_called()
