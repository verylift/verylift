"""Tests for the Wger connect/remove/sync settings flow (TASK-311)."""

from unittest.mock import patch

import pytest
from django.db import OperationalError
from django.test import Client
from django.urls import reverse

from accounts.tests.factories import UserFactory
from challenges.models import Challenge, ChallengeParticipant
from challenges.tests.factories import ChallengeFactory, ChallengeParticipantFactory


@pytest.fixture
def client():
    return Client()


@pytest.fixture
def user(db):
    return UserFactory()


@pytest.fixture
def authed_client(user):
    c = Client()
    c.force_login(user)
    return c


class TestWgerCredentialsForm:
    def test_save_wger_credentials(self, authed_client, user, db):
        url = reverse("accounts:settings")
        authed_client.post(
            url,
            {
                "form_name": "wger_credentials",
                "wger_instance_url": "https://my-wger.example.com",
                "wger_api_token": "my-test-token",
            },
        )
        user.refresh_from_db()
        assert user.wger_instance_url == "https://my-wger.example.com"
        assert user.wger_api_token == "my-test-token"

    def test_partial_submission_does_not_save(self, authed_client, user, db):
        """Only a URL with no token (or vice versa) must not connect."""
        url = reverse("accounts:settings")
        authed_client.post(
            url,
            {
                "form_name": "wger_credentials",
                "wger_instance_url": "https://my-wger.example.com",
                "wger_api_token": "",
            },
        )
        user.refresh_from_db()
        assert user.wger_instance_url is None
        assert user.wger_api_token is None

    def test_remove_wger_credentials(self, authed_client, user, db):
        user.wger_instance_url = "https://my-wger.example.com"
        user.wger_api_token = "existing-token"
        user.save()
        url = reverse("accounts:settings")
        authed_client.post(url, {"form_name": "remove_wger_credentials"})
        user.refresh_from_db()
        assert user.wger_instance_url is None
        assert user.wger_api_token is None

    def test_connected_status_shown(self, authed_client, user, db):
        user.wger_instance_url = "https://my-wger.example.com"
        user.wger_api_token = "existing-token"
        user.save()
        response = authed_client.get(reverse("accounts:settings"))
        assert b"my-wger.example.com" in response.content

    def test_token_value_not_exposed_in_response(self, authed_client, user, db):
        user.wger_instance_url = "https://my-wger.example.com"
        user.wger_api_token = "super-secret-token-value"
        user.save()
        response = authed_client.get(reverse("accounts:settings"))
        assert b"super-secret-token-value" not in response.content

    def test_connecting_triggers_backfill(self, authed_client, user, db):
        url = reverse("accounts:settings")
        with patch("accounts.views.trigger_wger_lift_history_backfill") as mock_trigger:
            authed_client.post(
                url,
                {
                    "form_name": "wger_credentials",
                    "wger_instance_url": "https://my-wger.example.com",
                    "wger_api_token": "my-test-token",
                },
            )
        mock_trigger.assert_called_once()


class TestValidateWgerCredentialsView:
    def test_post_with_valid_credentials_returns_json_valid(
        self, authed_client, user, db
    ):
        url = reverse("accounts:validate_wger_credentials")
        with patch("accounts.views.validate_wger_credentials", return_value=True):
            response = authed_client.post(
                url,
                {
                    "wger_instance_url": "https://my-wger.example.com",
                    "wger_api_token": "good-token",
                },
            )
        assert response.status_code == 200
        assert response.json()["valid"] is True

    def test_post_with_invalid_credentials_returns_json_invalid(
        self, authed_client, user, db
    ):
        url = reverse("accounts:validate_wger_credentials")
        with patch("accounts.views.validate_wger_credentials", return_value=False):
            response = authed_client.post(
                url,
                {
                    "wger_instance_url": "https://my-wger.example.com",
                    "wger_api_token": "bad-token",
                },
            )
        assert response.json()["valid"] is False

    def test_missing_credentials_returns_invalid(self, authed_client, user, db):
        url = reverse("accounts:validate_wger_credentials")
        response = authed_client.post(url, {})
        assert response.json()["valid"] is False

    def test_unauthenticated_post_redirects(self, client, db):
        url = reverse("accounts:validate_wger_credentials")
        response = client.post(url, {})
        assert response.status_code == 302

    def test_get_not_allowed(self, authed_client, user, db):
        url = reverse("accounts:validate_wger_credentials")
        response = authed_client.get(url)
        assert response.status_code == 405

    def test_empty_fields_fall_back_to_saved_credentials(self, authed_client, user, db):
        user.wger_instance_url = "https://my-wger.example.com"
        user.wger_api_token = "saved-token"
        user.save()
        url = reverse("accounts:validate_wger_credentials")
        with patch(
            "accounts.views.validate_wger_credentials", return_value=True
        ) as mock_validate:
            response = authed_client.post(url, {})
        assert response.json()["valid"] is True
        mock_validate.assert_called_once_with(
            "https://my-wger.example.com", "saved-token"
        )


class TestWgerSyncNowEndpoint:
    def test_requires_login(self, client, db):
        url = reverse("accounts:wger_sync_now")
        response = client.post(url)
        assert response.status_code == 302

    def test_get_not_allowed(self, authed_client, user, db):
        url = reverse("accounts:wger_sync_now")
        response = authed_client.get(url)
        assert response.status_code == 405

    def test_without_credentials_redirects_with_error(self, authed_client, user, db):
        url = reverse("accounts:wger_sync_now")
        response = authed_client.post(url, follow=True)
        messages = list(response.context["messages"])
        assert any("Connect your Wger account" in str(m) for m in messages)

    def test_forces_sync_for_active_accepted_challenges(self, db):
        user = UserFactory(
            wger_instance_url="https://my-wger.example.com",
            wger_api_token="tok",
        )
        challenge = ChallengeFactory(status=Challenge.Status.ACTIVE)
        ChallengeParticipantFactory(
            user=user,
            challenge=challenge,
            invite_status=ChallengeParticipant.InviteStatus.ACCEPTED,
        )
        c = Client()
        c.force_login(user)

        with (
            patch("accounts.views.sync_wger_lifts") as mock_sync,
            patch("accounts.views.score_pooled_history") as mock_score,
        ):
            response = c.post(reverse("accounts:wger_sync_now"), follow=True)

        mock_sync.assert_called_once_with(user, force=True)
        mock_score.assert_called_once_with(user=user, challenge=challenge)
        messages = list(response.context["messages"])
        assert any("1 challenge" in str(m) for m in messages)

    def test_db_contention_reports_back_instead_of_500(self, db):
        user = UserFactory(
            wger_instance_url="https://my-wger.example.com",
            wger_api_token="tok",
        )
        c = Client()
        c.force_login(user)
        with patch("accounts.views.sync_wger_lifts", side_effect=OperationalError):
            response = c.post(reverse("accounts:wger_sync_now"), follow=True)
        assert response.status_code == 200
        messages = list(response.context["messages"])
        assert any("try again" in str(m) for m in messages)
