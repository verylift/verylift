"""Tests for the onboarding Liftosaur-connect step."""

from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse

from accounts.tests.factories import UserFactory

User = get_user_model()


@pytest.fixture
def client():
    return Client()


@pytest.mark.django_db
class TestOnboardingLiftosaurView:
    def test_anonymous_get_redirects_to_login(self, client):
        response = client.get(reverse("accounts:onboarding-liftosaur"))
        assert response.status_code == 302
        assert response["Location"].startswith(reverse("accounts:login"))

    @patch("accounts.views.trigger_lift_history_backfill")
    @patch("accounts.views.validate_liftosaur_key", return_value=True)
    def test_valid_key_saves_triggers_backfill_and_redirects(
        self, mock_validate, mock_backfill, client
    ):
        user = UserFactory()
        client.force_login(user)

        response = client.post(
            reverse("accounts:onboarding-liftosaur"),
            {"liftosaur_api_key": "valid-key"},
        )

        assert response.status_code == 302
        assert response["Location"] == reverse("accounts:onboarding-units")
        mock_validate.assert_called_once_with("valid-key")
        user.refresh_from_db()
        assert user.liftosaur_api_key == "valid-key"
        mock_backfill.assert_called_once_with(user)

    @patch("accounts.views.trigger_lift_history_backfill")
    @patch("accounts.views.validate_liftosaur_key")
    def test_blank_key_skips_validation_and_backfill_but_still_redirects(
        self, mock_validate, mock_backfill, client
    ):
        user = UserFactory()
        client.force_login(user)

        response = client.post(
            reverse("accounts:onboarding-liftosaur"), {"liftosaur_api_key": ""}
        )

        assert response.status_code == 302
        assert response["Location"] == reverse("accounts:onboarding-units")
        mock_validate.assert_not_called()
        mock_backfill.assert_not_called()
        user.refresh_from_db()
        assert user.liftosaur_api_key is None

    @patch("accounts.views.trigger_lift_history_backfill")
    @patch("accounts.views.validate_liftosaur_key", return_value=False)
    def test_invalid_key_rerenders_with_error_and_does_not_touch_account(
        self, mock_validate, mock_backfill, client
    ):
        user = UserFactory()
        client.force_login(user)

        response = client.post(
            reverse("accounts:onboarding-liftosaur"),
            {"liftosaur_api_key": "bad-key"},
        )

        assert response.status_code == 200
        assert b"Could not validate this Liftosaur API key." in response.content
        user.refresh_from_db()
        assert user.liftosaur_api_key is None
        mock_backfill.assert_not_called()

    @patch("accounts.views.trigger_lift_history_backfill")
    @patch("accounts.views.validate_liftosaur_key", return_value=True)
    def test_resubmitting_an_unchanged_key_does_not_retrigger_backfill(
        self, mock_validate, mock_backfill, client
    ):
        """had_key_before: revisiting/resubmitting this step must not re-seed
        LiftHistory that's already been pulled."""
        user = UserFactory(liftosaur_api_key="already-connected-key")
        client.force_login(user)

        response = client.post(
            reverse("accounts:onboarding-liftosaur"),
            {"liftosaur_api_key": "already-connected-key"},
        )

        assert response.status_code == 302
        mock_backfill.assert_not_called()
