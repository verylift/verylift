"""Tests for the Hevy connect settings flow (TASK-320, TASK-324)."""

from unittest.mock import patch

import pytest
from django.db import OperationalError
from django.test import Client
from django.urls import reverse

from accounts.forms import HevyKeyForm
from accounts.tests.factories import UserFactory
from challenges.models import Challenge, ChallengeParticipant
from challenges.tests.factories import ChallengeFactory, ChallengeParticipantFactory
from hevy_api.services import HEVY_KEY_INVALID, HEVY_KEY_UNKNOWN, HEVY_KEY_VALID
from hevy_api.tests.factories import HevySyncLogFactory

HX = {"HTTP_HX_REQUEST": "true"}


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


class TestHevyKeySettingsBackfillTrigger:
    def test_saving_a_key_triggers_backfill_off_the_request(self, authed_client, user):
        """Saving a Hevy key must kick off the one-time backfill in the
        background, not run it inline -- that's the whole point of TASK-320's
        AC1. A regression back to a direct sync_user_lifts() call here would
        put the full HISTORY_BACKFILL_DAYS pull back on the request path."""
        url = reverse("accounts:settings")
        with (
            patch(
                "accounts.views.validate_hevy_key_status", return_value=HEVY_KEY_VALID
            ),
            patch("accounts.views.trigger_hevy_lift_history_backfill") as mock_trigger,
        ):
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


class TestHevyKeySaveValidation:
    """A key must be confirmed with Hevy before it's persisted -- see
    hevy_api.services.validate_hevy_key_status for the VALID/INVALID/UNKNOWN
    split this behavior hinges on."""

    def test_confirmed_invalid_key_is_not_saved(self, authed_client, user):
        url = reverse("accounts:settings")
        with patch(
            "accounts.views.validate_hevy_key_status", return_value=HEVY_KEY_INVALID
        ):
            response = authed_client.post(
                url, {"form_name": "hevy_key", "hevy_api_key": "bad-key"}, follow=True
            )

        user.refresh_from_db()
        assert user.hevy_api_key is None
        assert b"Could not validate this Hevy API key." in response.content

    def test_confirmed_invalid_key_does_not_trigger_backfill(self, authed_client, user):
        url = reverse("accounts:settings")
        with (
            patch(
                "accounts.views.validate_hevy_key_status", return_value=HEVY_KEY_INVALID
            ),
            patch("accounts.views.trigger_hevy_lift_history_backfill") as mock_trigger,
        ):
            authed_client.post(
                url, {"form_name": "hevy_key", "hevy_api_key": "bad-key"}
            )

        mock_trigger.assert_not_called()

    def test_confirmed_valid_key_is_saved(self, authed_client, user):
        url = reverse("accounts:settings")
        with patch(
            "accounts.views.validate_hevy_key_status", return_value=HEVY_KEY_VALID
        ):
            authed_client.post(
                url, {"form_name": "hevy_key", "hevy_api_key": "good-key"}
            )

        user.refresh_from_db()
        assert user.hevy_api_key == "good-key"

    def test_unconfirmed_key_is_still_saved(self, authed_client, user):
        """A network hiccup while probing Hevy must not block saving a key
        that may well be good -- rejecting on every transient failure would
        be its own UX regression. A genuinely bad key is still caught later,
        when the next sync attempt fails (see TestHevySyncNowEndpoint)."""
        url = reverse("accounts:settings")
        with patch(
            "accounts.views.validate_hevy_key_status", return_value=HEVY_KEY_UNKNOWN
        ):
            response = authed_client.post(
                url, {"form_name": "hevy_key", "hevy_api_key": "maybe-key"}, follow=True
            )

        user.refresh_from_db()
        assert user.hevy_api_key == "maybe-key"
        assert b"couldn" in response.content.lower()

    def test_unconfirmed_key_still_triggers_backfill(self, authed_client, user):
        url = reverse("accounts:settings")
        with (
            patch(
                "accounts.views.validate_hevy_key_status", return_value=HEVY_KEY_UNKNOWN
            ),
            patch("accounts.views.trigger_hevy_lift_history_backfill") as mock_trigger,
        ):
            authed_client.post(
                url, {"form_name": "hevy_key", "hevy_api_key": "maybe-key"}
            )

        mock_trigger.assert_called_once_with(user)


class TestRemoveHevyKeySettings:
    def test_removing_key_clears_it_from_db(self, authed_client, user):
        user.hevy_api_key = "existing-key"
        user.save(update_fields=["hevy_api_key"])
        url = reverse("accounts:settings")

        authed_client.post(url, {"form_name": "remove_hevy_key"})

        user.refresh_from_db()
        assert user.hevy_api_key is None


class TestHevyKeyFormSave:
    def test_save_with_empty_key_returns_false_and_does_not_persist(self, user):
        form = HevyKeyForm({"hevy_api_key": ""})
        assert form.is_valid()

        result = form.save(user)

        assert result is False
        assert user.hevy_api_key is None


class TestHevySyncNowEndpoint:
    def test_requires_login(self, client, db):
        url = reverse("accounts:hevy_sync_now")
        response = client.post(url)
        assert response.status_code == 302

    def test_get_not_allowed(self, authed_client, user):
        url = reverse("accounts:hevy_sync_now")
        response = authed_client.get(url)
        assert response.status_code == 405

    def test_without_key_redirects_with_error(self, authed_client, user):
        url = reverse("accounts:hevy_sync_now")
        response = authed_client.post(url, follow=True)
        messages = list(response.context["messages"])
        assert any("Connect a Hevy API key" in str(m) for m in messages)

    def test_forces_sync_for_active_accepted_challenges(self, db):
        user = UserFactory(hevy_api_key="tok")
        challenge = ChallengeFactory(status=Challenge.Status.ACTIVE)
        ChallengeParticipantFactory(
            user=user,
            challenge=challenge,
            invite_status=ChallengeParticipant.InviteStatus.ACCEPTED,
        )
        c = Client()
        c.force_login(user)

        with (
            patch("accounts.views.sync_hevy_lifts") as mock_sync,
            patch("accounts.views.score_pooled_history") as mock_score,
        ):
            response = c.post(reverse("accounts:hevy_sync_now"), follow=True)

        mock_sync.assert_called_once_with(user, force=True)
        mock_score.assert_called_once_with(user=user, challenge=challenge)
        messages = list(response.context["messages"])
        assert any("1 challenge" in str(m) for m in messages)

    def test_db_contention_reports_back_instead_of_500(self, db):
        user = UserFactory(hevy_api_key="tok")
        c = Client()
        c.force_login(user)
        with patch("accounts.views.sync_hevy_lifts", side_effect=OperationalError):
            response = c.post(reverse("accounts:hevy_sync_now"), follow=True)
        assert response.status_code == 200
        messages = list(response.context["messages"])
        assert any("try again" in str(m) for m in messages)

    def test_failed_pull_does_not_report_sync_triggered(self, db):
        """sync_user_lifts swallows HevyAPIError/network failures and returns
        0 -- the same value it returns for "nothing new". Without checking
        the HevySyncLog it wrote, a failed pull would be reported to the user
        as a success (the bug TASK-324 fixes)."""
        user = UserFactory(hevy_api_key="tok")
        c = Client()
        c.force_login(user)

        def fake_sync(user, force=False):
            HevySyncLogFactory(user=user, success=False, error_detail="boom")
            return 0

        with (
            patch("accounts.views.sync_hevy_lifts", side_effect=fake_sync),
            patch("accounts.views.score_pooled_history"),
        ):
            response = c.post(reverse("accounts:hevy_sync_now"), follow=True)

        messages = list(response.context["messages"])
        assert not any("Sync triggered" in str(m) for m in messages)
        assert any("try again" in str(m) for m in messages)

    def test_successful_pull_reports_sync_triggered(self, db):
        user = UserFactory(hevy_api_key="tok")
        c = Client()
        c.force_login(user)

        def fake_sync(user, force=False):
            HevySyncLogFactory(user=user, success=True)
            return 3

        with (
            patch("accounts.views.sync_hevy_lifts", side_effect=fake_sync),
            patch("accounts.views.score_pooled_history"),
        ):
            response = c.post(reverse("accounts:hevy_sync_now"), follow=True)

        messages = list(response.context["messages"])
        assert any("Sync triggered" in str(m) for m in messages)

    def test_failed_pull_shown_on_reload_via_settings_page(self, db):
        """The failure must persist past the one-time flash message -- a
        user who syncs then navigates away and back should still see it, not
        just whoever was looking at the page the moment it happened."""
        user = UserFactory(hevy_api_key="tok")
        HevySyncLogFactory(user=user, success=False, error_detail="boom")
        c = Client()
        c.force_login(user)

        response = c.get(reverse("accounts:settings"))

        assert b"Last sync failed" in response.content

    def test_htmx_request_returns_partial_instead_of_redirect(self, db):
        user = UserFactory(hevy_api_key="tok")
        c = Client()
        c.force_login(user)

        with (
            patch("accounts.views.sync_hevy_lifts"),
            patch("accounts.views.score_pooled_history"),
        ):
            response = c.post(reverse("accounts:hevy_sync_now"), **HX)

        assert response.status_code == 200
        assert 'id="hevy-sync-status"' in response.content.decode()

    def test_htmx_request_without_key_returns_partial_not_redirect(
        self, authed_client, user
    ):
        response = authed_client.post(reverse("accounts:hevy_sync_now"), **HX)

        assert response.status_code == 200
        assert 'id="hevy-sync-status"' in response.content.decode()


class TestValidateHevyKeyView:
    def test_post_with_valid_key_returns_json_valid(self, authed_client, user, db):
        url = reverse("accounts:validate_hevy_key")
        with patch("accounts.views.validate_hevy_key", return_value=True):
            response = authed_client.post(url, {"api_key": "good-key"})
        assert response.status_code == 200
        assert response.json()["valid"] is True

    def test_post_with_invalid_key_returns_json_invalid(self, authed_client, user, db):
        url = reverse("accounts:validate_hevy_key")
        with patch("accounts.views.validate_hevy_key", return_value=False):
            response = authed_client.post(url, {"api_key": "bad-key"})
        assert response.json()["valid"] is False

    def test_unauthenticated_post_redirects(self, client, db):
        url = reverse("accounts:validate_hevy_key")
        response = client.post(url, {"api_key": "key"})
        assert response.status_code == 302

    def test_get_not_allowed(self, authed_client, user, db):
        url = reverse("accounts:validate_hevy_key")
        response = authed_client.get(url)
        assert response.status_code == 405

    def test_empty_api_key_falls_back_to_saved_key(self, authed_client, user, db):
        user.hevy_api_key = "saved-key-abc"
        user.save()
        url = reverse("accounts:validate_hevy_key")
        with patch(
            "accounts.views.validate_hevy_key", return_value=True
        ) as mock_validate:
            response = authed_client.post(url, {"api_key": ""})
        assert response.json()["valid"] is True
        mock_validate.assert_called_once_with("saved-key-abc")

    def test_empty_api_key_no_saved_key_returns_invalid(self, authed_client, user, db):
        url = reverse("accounts:validate_hevy_key")
        response = authed_client.post(url, {"api_key": ""})
        assert response.json()["valid"] is False
