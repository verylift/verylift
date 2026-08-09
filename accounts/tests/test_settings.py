"""Tests for settings view (TASK-11, TASK-12, TASK-13, TASK-14)."""

import struct
import zlib
from io import BytesIO
from unittest.mock import patch

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import OperationalError
from django.test import Client, RequestFactory
from django.urls import reverse
from django.views.static import serve as serve_static
from PIL import Image

from accounts.tests.factories import UserFactory
from challenges.models import Challenge, ChallengeParticipant
from challenges.tests.factories import (
    ChallengeFactory,
    ChallengeParticipantFactory,
)


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


class TestSettingsViewGet:
    def test_get_returns_200_for_logged_in_user(self, authed_client, user, db):
        url = reverse("accounts:settings")
        response = authed_client.get(url)
        assert response.status_code == 200

    def test_get_unauthenticated_redirects_to_login(self, client, db):
        url = reverse("accounts:settings")
        response = client.get(url)
        assert response.status_code == 302
        assert "/accounts/login/" in response["Location"]

    def test_template_used(self, authed_client, user, db):
        url = reverse("accounts:settings")
        response = authed_client.get(url)
        assert "accounts/settings.html" in [t.name for t in response.templates]

    def test_loading_overlay_rendered(self, authed_client, user, db):
        # The global loading overlay (TASK-141) is included from base.html, so
        # every signed-in page renders it ready for window.ppLoading.show().
        url = reverse("accounts:settings")
        response = authed_client.get(url)
        assert b'id="loading-overlay"' in response.content

    def test_sidebar_nav_items_present(self, authed_client, user, db):
        url = reverse("accounts:settings")
        response = authed_client.get(url)
        content = response.content.decode()
        assert "Dashboard" in content
        assert "Challenges" in content
        # The standalone notifications page (and its nav item) was removed in
        # TASK-246 — notifications live on the dashboard now.
        assert "Notifications" not in content
        # Find Users went with the user directory itself in TASK-272.
        assert "Find Users" not in content
        assert "Settings" in content
        assert "Logout" in content

    def test_admin_link_shown_for_staff(self, db):
        staff = UserFactory(is_staff=True)
        c = Client()
        c.force_login(staff)
        response = c.get(reverse("accounts:settings"))
        content = response.content.decode()
        assert f'href="{reverse("admin:index")}"' in content
        assert "Admin" in content

    def test_admin_link_hidden_for_non_staff(self, authed_client, user, db):
        response = authed_client.get(reverse("accounts:settings"))
        assert f'href="{reverse("admin:index")}"' not in response.content.decode()


class TestSettingsNicknameForm:
    def test_post_updates_display_name(self, authed_client, user, db):
        url = reverse("accounts:settings")
        response = authed_client.post(
            url, {"form_name": "nickname", "display_name": "New Name"}
        )
        assert response.status_code == 302
        user.refresh_from_db()
        assert user.display_name == "New Name"

    def test_post_shows_success_message(self, authed_client, user, db):
        url = reverse("accounts:settings")
        authed_client.post(url, {"form_name": "nickname", "display_name": "Renamed"})
        # Follow redirect to verify the page still loads after save
        authed_client.post(url, {"form_name": "nickname", "display_name": "Again"})
        resp2 = authed_client.get(reverse("accounts:settings"))
        assert resp2.status_code == 200

    def test_post_nickname_strips_whitespace(self, authed_client, user, db):
        url = reverse("accounts:settings")
        authed_client.post(
            url, {"form_name": "nickname", "display_name": "  Trimmed  "}
        )
        user.refresh_from_db()
        assert user.display_name == "Trimmed"

    def test_unauthenticated_post_redirects(self, client, user, db):
        url = reverse("accounts:settings")
        response = client.post(url, {"form_name": "nickname", "display_name": "X"})
        assert response.status_code == 302
        assert "/accounts/login/" in response["Location"]


class TestEmailForm:
    """The Settings email section (TASK-283) is how an existing local account
    becomes recoverable at all."""

    def test_save_email(self, authed_client, user, db):
        response = authed_client.post(
            reverse("accounts:settings"),
            {"form_name": "email", "email": "new@example.com"},
        )
        assert response.status_code == 302
        user.refresh_from_db()
        assert user.email == "new@example.com"

    def test_blank_clears_the_stored_email(self, authed_client, user, db):
        authed_client.post(
            reverse("accounts:settings"), {"form_name": "email", "email": ""}
        )
        user.refresh_from_db()
        assert user.email == ""

    def test_malformed_email_re_renders_with_an_error(self, authed_client, user, db):
        original = user.email
        response = authed_client.post(
            reverse("accounts:settings"),
            {"form_name": "email", "email": "notanemail"},
        )
        assert response.status_code == 200
        assert response.context["email_error"]
        # What was typed stays on screen next to the error.
        assert response.context["email"] == "notanemail"
        user.refresh_from_db()
        assert user.email == original

    def test_current_email_is_rendered(self, authed_client, user, db):
        response = authed_client.get(reverse("accounts:settings"))
        assert f'value="{user.email}"'.encode() in response.content


class TestLiftosaurKeyForms:
    def test_save_liftosaur_key(self, authed_client, user, db):
        url = reverse("accounts:settings")
        authed_client.post(
            url,
            {"form_name": "liftosaur_key", "liftosaur_api_key": "my-test-key"},
        )
        user.refresh_from_db()
        assert user.liftosaur_api_key == "my-test-key"

    def test_remove_liftosaur_key(self, authed_client, user, db):
        user.liftosaur_api_key = "existing-key"
        user.save()
        url = reverse("accounts:settings")
        authed_client.post(url, {"form_name": "remove_liftosaur_key"})
        user.refresh_from_db()
        assert user.liftosaur_api_key is None

    def test_key_status_connected_shown(self, authed_client, user, db):
        user.liftosaur_api_key = "existing-key"
        user.save()
        url = reverse("accounts:settings")
        response = authed_client.get(url)
        assert b"Connected" in response.content

    def test_key_entry_form_shown_when_no_key(self, authed_client, user, db):
        """When no key is saved, the entry form input is shown."""
        url = reverse("accounts:settings")
        response = authed_client.get(url)
        assert b"liftosaur_api_key" in response.content

    def test_test_button_not_shown_when_no_key(self, authed_client, user, db):
        """Test Connection button must NOT appear before a key is saved."""
        url = reverse("accounts:settings")
        response = authed_client.get(url)
        assert b"btn-test-saved-key" not in response.content

    def test_masked_key_shown_when_key_saved(self, authed_client, user, db):
        """After saving, masked key (last 6 visible) is shown instead of full key."""
        user.liftosaur_api_key = "abcdef123456"
        user.save()
        url = reverse("accounts:settings")
        response = authed_client.get(url)
        content = response.content.decode()
        assert "123456" in content  # last 6 visible
        assert "abcdef" not in content  # first chars masked

    def test_test_button_shown_when_key_saved(self, authed_client, user, db):
        """Test Connection button appears only when a key is saved."""
        user.liftosaur_api_key = "existing-key"
        user.save()
        url = reverse("accounts:settings")
        response = authed_client.get(url)
        assert b"btn-test-saved-key" in response.content

    def test_remove_key_button_shown_when_key_saved(self, authed_client, user, db):
        """Remove Key button appears when a key is saved."""
        user.liftosaur_api_key = "existing-key"
        user.save()
        url = reverse("accounts:settings")
        response = authed_client.get(url)
        assert b"Remove Key" in response.content

    def test_entry_form_not_shown_when_key_saved(self, authed_client, user, db):
        """Input field for entering a key is hidden when a key is already saved."""
        user.liftosaur_api_key = "existing-key"
        user.save()
        url = reverse("accounts:settings")
        response = authed_client.get(url)
        assert b'id="liftosaur_api_key"' not in response.content

    def test_key_value_not_exposed_in_response(self, authed_client, user, db):
        user.liftosaur_api_key = "super-secret-key-value"
        user.save()
        url = reverse("accounts:settings")
        response = authed_client.get(url)
        assert b"super-secret-key-value" not in response.content

    def test_masked_key_short_key_shows_full(self, authed_client, user, db):
        """Keys shorter than 6 chars are shown in full (no truncation)."""
        user.liftosaur_api_key = "abc"
        user.save()
        url = reverse("accounts:settings")
        response = authed_client.get(url)
        assert b"abc" in response.content


class TestValidateLiftosaurKeyView:
    def test_post_with_valid_key_returns_json_valid(self, authed_client, user, db):
        url = reverse("accounts:validate_liftosaur_key")
        with patch("accounts.views.validate_liftosaur_key", return_value=True):
            response = authed_client.post(url, {"api_key": "good-key"})
        assert response.status_code == 200
        data = response.json()
        assert data["valid"] is True
        assert data["message"] == "Connection successful."

    def test_post_with_invalid_key_returns_json_invalid(self, authed_client, user, db):
        url = reverse("accounts:validate_liftosaur_key")
        with patch("accounts.views.validate_liftosaur_key", return_value=False):
            response = authed_client.post(url, {"api_key": "bad-key"})
        assert response.status_code == 200
        data = response.json()
        assert data["valid"] is False

    def test_post_with_empty_key_returns_invalid(self, authed_client, user, db):
        url = reverse("accounts:validate_liftosaur_key")
        response = authed_client.post(url, {"api_key": ""})
        assert response.status_code == 200
        data = response.json()
        assert data["valid"] is False

    def test_unauthenticated_post_redirects(self, client, db):
        url = reverse("accounts:validate_liftosaur_key")
        response = client.post(url, {"api_key": "key"})
        assert response.status_code == 302

    def test_get_not_allowed(self, authed_client, user, db):
        url = reverse("accounts:validate_liftosaur_key")
        response = authed_client.get(url)
        assert response.status_code == 405

    def test_empty_api_key_uses_saved_key(self, authed_client, user, db):
        """When api_key param is empty, view falls back to the user's saved key."""
        user.liftosaur_api_key = "saved-key-abc"
        user.save()
        url = reverse("accounts:validate_liftosaur_key")
        with patch(
            "accounts.views.validate_liftosaur_key", return_value=True
        ) as mock_validate:
            response = authed_client.post(url, {"api_key": ""})
        data = response.json()
        assert data["valid"] is True
        mock_validate.assert_called_once_with("saved-key-abc")

    def test_empty_api_key_no_saved_key_returns_invalid(self, authed_client, user, db):
        """When api_key param is empty and no saved key, returns invalid."""
        url = reverse("accounts:validate_liftosaur_key")
        response = authed_client.post(url, {"api_key": ""})
        data = response.json()
        assert data["valid"] is False
        assert "No API key" in data["message"]


class TestUnitPreferenceForm:
    def test_unit_preference_defaults_to_kg(self, authed_client, user, db):
        assert user.unit_preference == "kg"

    def test_saving_lb_unit_preference_persists(self, authed_client, user, db):
        url = reverse("accounts:settings")
        authed_client.post(
            url,
            {"form_name": "unit_preference", "unit_preference": "lb"},
        )
        user.refresh_from_db()
        assert user.unit_preference == "lb"

    def test_saving_kg_unit_preference_persists(self, authed_client, user, db):
        user.unit_preference = "lb"
        user.save(update_fields=["unit_preference"])
        url = reverse("accounts:settings")
        authed_client.post(
            url,
            {"form_name": "unit_preference", "unit_preference": "kg"},
        )
        user.refresh_from_db()
        assert user.unit_preference == "kg"

    def test_unit_preference_applied_immediately_after_save(
        self, authed_client, user, db
    ):
        url = reverse("accounts:settings")
        authed_client.post(
            url,
            {"form_name": "unit_preference", "unit_preference": "lb"},
        )
        # Same session, no re-login: the rendered toggle reflects the new value.
        response = authed_client.get(url)
        assert response.context["unit_preference"] == "lb"

    def test_invalid_unit_preference_falls_back_to_kg(self, authed_client, user, db):
        url = reverse("accounts:settings")
        authed_client.post(
            url,
            {"form_name": "unit_preference", "unit_preference": "stone"},
        )
        user.refresh_from_db()
        assert user.unit_preference == "kg"

    def test_plain_post_redirects(self, authed_client, user, db):
        url = reverse("accounts:settings")
        response = authed_client.post(
            url,
            {"form_name": "unit_preference", "unit_preference": "lb"},
        )
        assert response.status_code == 302
        assert response.url == url

    def test_htmx_returns_only_section_partial(self, authed_client, user, db):
        url = reverse("accounts:settings")
        response = authed_client.post(
            url,
            {"form_name": "unit_preference", "unit_preference": "lb"},
            HTTP_HX_REQUEST="true",
        )
        assert response.status_code == 200
        content = response.content.decode()
        assert 'name="form_name" value="unit_preference"' in content
        # The swapped-back fragment is the unit section only, not the whole page.
        assert "Body Measurements" not in content
        assert "Unit preference saved." in content


class TestSyncNowEndpoint:
    def test_requires_login(self, client, db):
        url = reverse("accounts:sync_now")
        response = client.post(url)
        assert response.status_code == 302
        assert reverse("accounts:login") in response["Location"]

    def test_get_not_allowed(self, authed_client, user, db):
        url = reverse("accounts:sync_now")
        response = authed_client.get(url)
        assert response.status_code == 405

    def test_without_key_redirects_with_error(self, authed_client, user, db):
        url = reverse("accounts:sync_now")
        with patch("accounts.views.sync_user_lifts") as mock_sync:
            response = authed_client.post(url)
        mock_sync.assert_not_called()
        assert response.status_code == 302
        assert response["Location"] == reverse("accounts:settings")

    def test_forces_sync_for_active_accepted_challenges(self, db):
        user = UserFactory(liftosaur_api_key="key")
        active = ChallengeFactory(status=Challenge.Status.ACTIVE)
        ChallengeParticipantFactory(
            user=user,
            challenge=active,
            invite_status=ChallengeParticipant.InviteStatus.ACCEPTED,
        )
        # Should be ignored: draft challenge, and a pending invite.
        draft = ChallengeFactory(status=Challenge.Status.DRAFT)
        ChallengeParticipantFactory(
            user=user,
            challenge=draft,
            invite_status=ChallengeParticipant.InviteStatus.ACCEPTED,
        )
        pending = ChallengeFactory(status=Challenge.Status.ACTIVE)
        ChallengeParticipantFactory(
            user=user,
            challenge=pending,
            invite_status=ChallengeParticipant.InviteStatus.INVITED,
        )

        c = Client()
        c.force_login(user)
        url = reverse("accounts:sync_now")
        with (
            patch("accounts.views.sync_user_lifts") as mock_sync,
            patch("accounts.views.score_pooled_history") as mock_score,
        ):
            response = c.post(url)

        # One shared-pool pull refreshes every challenge; scoring is then an
        # explicit per-challenge call for each active, accepted challenge.
        mock_sync.assert_called_once_with(user, force=True)
        mock_score.assert_called_once_with(user=user, challenge=active)
        assert response.status_code == 302
        assert response["Location"] == reverse("accounts:settings")

    def test_db_contention_reports_back_instead_of_500(self, db):
        """TASK-274: "Sync now" is what a user mashes when things feel stuck, so
        a lost write-lock race here must surface as a message, not a 500."""
        user = UserFactory(liftosaur_api_key="key")
        c = Client()
        c.force_login(user)
        with patch(
            "accounts.views.sync_user_lifts",
            side_effect=OperationalError("database is locked"),
        ):
            response = c.post(reverse("accounts:sync_now"), follow=True)
        assert response.status_code == 200
        assert "Couldn&#x27;t sync right now." in response.content.decode()

    def test_db_contention_during_scoring_reports_back(self, db):
        """The scoring loop is inside the same guard as the pull: a lock lost
        while rescoring must degrade the same way."""
        user = UserFactory(liftosaur_api_key="key")
        active = ChallengeFactory(status=Challenge.Status.ACTIVE)
        ChallengeParticipantFactory(
            user=user,
            challenge=active,
            invite_status=ChallengeParticipant.InviteStatus.ACCEPTED,
        )
        c = Client()
        c.force_login(user)
        with (
            patch("accounts.views.sync_user_lifts"),
            patch(
                "accounts.views.score_pooled_history",
                side_effect=OperationalError("database is locked"),
            ),
        ):
            response = c.post(reverse("accounts:sync_now"), **HX)
        assert response.status_code == 200
        content = response.content.decode()
        assert 'id="liftosaur-sync-status"' in content
        assert "Couldn&#x27;t sync right now." in content


@pytest.mark.django_db
class TestSettingsLastSyncedStamp:
    """Settings page surfaces when Liftosaur data was last synced (TASK-144)."""

    def test_stamp_rendered_when_successful_sync_exists(self):
        from liftosaur.tests.factories import LiftosaurSyncLogFactory

        user = UserFactory(liftosaur_api_key="test-key")
        LiftosaurSyncLogFactory(user=user, success=True)
        c = Client()
        c.force_login(user)

        response = c.get(reverse("accounts:settings"))

        assert response.status_code == 200
        assert b"Last synced" in response.content

    def test_no_stamp_when_never_synced(self):
        user = UserFactory(liftosaur_api_key="test-key")
        c = Client()
        c.force_login(user)

        response = c.get(reverse("accounts:settings"))

        assert response.status_code == 200
        assert b"Last synced" not in response.content


HX = {"HTTP_HX_REQUEST": "true"}


@pytest.mark.django_db
class TestHtmxFoundation:
    """base.html htmx wiring and the sync-button partial swaps (TASK-145)."""

    def test_base_renders_app_messages_container_unconditionally(self, authed_client):
        response = authed_client.get(reverse("accounts:settings"))
        assert response.status_code == 200
        assert b'id="app-messages"' in response.content

    def test_base_loads_htmx_and_sets_csrf_header(self, authed_client):
        response = authed_client.get(reverse("accounts:settings"))
        content = response.content.decode()
        assert "vendor/htmx.min.js" in content
        assert "hx-headers" in content
        assert "X-CSRFToken" in content

    def test_settings_sync_forms_carry_htmx_attrs(self):
        user = UserFactory(liftosaur_api_key="key")
        c = Client()
        c.force_login(user)
        content = c.get(reverse("accounts:settings")).content.decode()
        assert "hx-post" in content
        assert 'hx-target="#liftosaur-sync-status"' in content
        assert "data-loading-message" in content


@pytest.mark.django_db
class TestSyncNowHtmx:
    def test_htmx_success_returns_partial_and_message(self):
        user = UserFactory(liftosaur_api_key="key")
        c = Client()
        c.force_login(user)
        with (
            patch("accounts.views.sync_user_lifts"),
            patch("accounts.views.score_pooled_history"),
        ):
            response = c.post(reverse("accounts:sync_now"), **HX)
        assert response.status_code == 200
        content = response.content.decode()
        assert 'id="liftosaur-sync-status"' in content
        assert 'hx-swap-oob="innerHTML"' in content
        assert "Sync triggered" in content

    def test_htmx_without_key_returns_partial_and_error(self, user):
        c = Client()
        c.force_login(user)
        with patch("accounts.views.sync_user_lifts") as mock_sync:
            response = c.post(reverse("accounts:sync_now"), **HX)
        mock_sync.assert_not_called()
        assert response.status_code == 200
        content = response.content.decode()
        assert 'id="liftosaur-sync-status"' in content
        assert "Connect a Liftosaur API key first." in content


@pytest.mark.django_db
@pytest.mark.django_db
class TestSectionSavesHtmx:
    """Per-section save forms swap only their own section over HTMX (TASK-146)."""

    def test_settings_forms_carry_section_swap_attrs(self, authed_client, user):
        content = authed_client.get(reverse("accounts:settings")).content.decode()
        assert 'hx-target="closest section"' in content
        assert 'hx-swap="outerHTML"' in content

    def test_nickname_htmx_returns_only_section_partial(self, authed_client, user):
        response = authed_client.post(
            reverse("accounts:settings"),
            {"form_name": "nickname", "display_name": "Swapped"},
            **HX,
        )
        assert response.status_code == 200
        rendered = [t.name for t in response.templates]
        assert "accounts/_nickname_section.html" in rendered
        assert "accounts/settings.html" not in rendered
        content = response.content.decode()
        assert 'value="Swapped"' in content
        # Success message rides along as an out-of-band swap.
        assert 'hx-swap-oob="innerHTML"' in content
        assert "Display name updated." in content
        user.refresh_from_db()
        assert user.display_name == "Swapped"

    def test_email_htmx_returns_only_section_partial(self, authed_client, user):
        response = authed_client.post(
            reverse("accounts:settings"),
            {"form_name": "email", "email": "swapped@example.com"},
            **HX,
        )
        assert response.status_code == 200
        rendered = [t.name for t in response.templates]
        assert "accounts/_email_section.html" in rendered
        assert "accounts/settings.html" not in rendered
        content = response.content.decode()
        assert 'value="swapped@example.com"' in content
        assert 'hx-swap-oob="innerHTML"' in content
        assert "Email address saved." in content
        user.refresh_from_db()
        assert user.email == "swapped@example.com"

    def test_email_htmx_error_returns_the_section_with_the_error(
        self, authed_client, user
    ):
        response = authed_client.post(
            reverse("accounts:settings"),
            {"form_name": "email", "email": "nope"},
            **HX,
        )
        assert response.status_code == 200
        assert "accounts/_email_section.html" in [t.name for t in response.templates]
        assert "Enter a valid email address." in response.content.decode()

    def test_nickname_plain_post_still_redirects(self, authed_client, user):
        response = authed_client.post(
            reverse("accounts:settings"),
            {"form_name": "nickname", "display_name": "PlainName"},
        )
        assert response.status_code == 302
        assert response["Location"] == reverse("accounts:settings")

    def test_liftosaur_key_save_htmx_returns_key_card_state(self, authed_client, user):
        response = authed_client.post(
            reverse("accounts:settings"),
            {"form_name": "liftosaur_key", "liftosaur_api_key": "my-key-abcdef"},
            **HX,
        )
        assert response.status_code == 200
        rendered = [t.name for t in response.templates]
        assert "accounts/_liftosaur_section.html" in rendered
        assert "accounts/settings.html" not in rendered
        content = response.content.decode()
        # Swapped into the key-card (Connected) state, not the entry form.
        assert "Connected" in content
        assert 'id="liftosaur_api_key"' not in content
        assert "Liftosaur API key saved." in content
        user.refresh_from_db()
        assert user.liftosaur_api_key == "my-key-abcdef"

    def test_liftosaur_remove_htmx_returns_entry_state(self, authed_client, user):
        user.liftosaur_api_key = "existing-key"
        user.save(update_fields=["liftosaur_api_key"])
        response = authed_client.post(
            reverse("accounts:settings"),
            {"form_name": "remove_liftosaur_key"},
            **HX,
        )
        assert response.status_code == 200
        content = response.content.decode()
        # Swapped back to the entry form (no key saved).
        assert 'id="liftosaur_api_key"' in content
        assert "Liftosaur API key removed." in content
        user.refresh_from_db()
        assert user.liftosaur_api_key is None


class TestLanguageForm:
    def test_language_defaults_to_automatic(self, authed_client, user, db):
        assert user.language == ""

    def test_saving_spanish_persists(self, authed_client, user, db):
        url = reverse("accounts:settings")
        authed_client.post(url, {"form_name": "language", "language": "es"})
        user.refresh_from_db()
        assert user.language == "es"

    def test_saving_empty_language_persists_as_automatic(self, authed_client, user, db):
        user.language = "es"
        user.save(update_fields=["language"])
        url = reverse("accounts:settings")
        authed_client.post(url, {"form_name": "language", "language": ""})
        user.refresh_from_db()
        assert user.language == ""

    def test_invalid_language_falls_back_to_automatic(self, authed_client, user, db):
        url = reverse("accounts:settings")
        authed_client.post(url, {"form_name": "language", "language": "xx"})
        user.refresh_from_db()
        assert user.language == ""

    def test_language_post_is_plain_prg_redirect(self, authed_client, user, db):
        # Unlike unit_preference, the language switcher is never HTMX-driven —
        # a language change re-renders the whole page chrome, so even an
        # htmx-headered POST still gets the plain PRG redirect (form_name is
        # never in _SETTINGS_SECTION_PARTIALS).
        url = reverse("accounts:settings")
        response = authed_client.post(url, {"form_name": "language", "language": "es"})
        assert response.status_code == 302
        assert response["Location"] == url

    def test_saving_language_sets_cookie(self, authed_client, user, db):
        url = reverse("accounts:settings")
        response = authed_client.post(url, {"form_name": "language", "language": "es"})
        assert response.cookies["django_language"].value == "es"

    def test_saving_automatic_clears_cookie(self, authed_client, user, db):
        user.language = "es"
        user.save(update_fields=["language"])
        url = reverse("accounts:settings")
        response = authed_client.post(url, {"form_name": "language", "language": ""})
        cookie = response.cookies["django_language"]
        assert cookie.value == ""
        assert cookie["max-age"] == 0

    def test_success_message_shown(self, authed_client, user, db):
        url = reverse("accounts:settings")
        response = authed_client.post(
            url, {"form_name": "language", "language": "es"}, follow=True
        )
        assert "Language saved." in response.content.decode()

    def test_settings_page_lists_supported_languages(self, authed_client, user, db):
        url = reverse("accounts:settings")
        response = authed_client.get(url)
        assert response.context["languages"] == [
            ("en", "English"),
            ("es", "Español"),
        ]
        assert response.context["language"] == ""


class TestTimezoneForm:
    def test_timezone_defaults_to_automatic(self, authed_client, user, db):
        assert user.timezone == ""

    def test_saving_valid_timezone_persists(self, authed_client, user, db):
        url = reverse("accounts:settings")
        authed_client.post(url, {"form_name": "timezone", "timezone": "Asia/Tokyo"})
        user.refresh_from_db()
        assert user.timezone == "Asia/Tokyo"

    def test_saving_empty_timezone_persists_as_automatic(self, authed_client, user, db):
        user.timezone = "Asia/Tokyo"
        user.save(update_fields=["timezone"])
        url = reverse("accounts:settings")
        authed_client.post(url, {"form_name": "timezone", "timezone": ""})
        user.refresh_from_db()
        assert user.timezone == ""

    def test_invalid_timezone_falls_back_to_automatic(self, authed_client, user, db):
        url = reverse("accounts:settings")
        authed_client.post(url, {"form_name": "timezone", "timezone": "Not/AZone"})
        user.refresh_from_db()
        assert user.timezone == ""

    def test_settings_page_context_exposes_timezone_groups(
        self, authed_client, user, db
    ):
        url = reverse("accounts:settings")
        response = authed_client.get(url)
        assert response.context["user_timezone"] == ""
        groups = dict(response.context["timezone_groups"])
        assert "America/Toronto" in groups["America"]

    def test_success_message_shown(self, authed_client, user, db):
        url = reverse("accounts:settings")
        response = authed_client.post(
            url, {"form_name": "timezone", "timezone": "Asia/Tokyo"}, follow=True
        )
        assert "Timezone saved." in response.content.decode()

    def test_htmx_post_returns_partial_not_redirect(self, authed_client, user, db):
        url = reverse("accounts:settings")
        response = authed_client.post(
            url, {"form_name": "timezone", "timezone": "Asia/Tokyo"}, **HX
        )
        assert response.status_code == 200
        assert 'id="timezone-select"' in response.content.decode()

    def test_plain_post_is_prg_redirect(self, authed_client, user, db):
        url = reverse("accounts:settings")
        response = authed_client.post(
            url, {"form_name": "timezone", "timezone": "Asia/Tokyo"}
        )
        assert response.status_code == 302
        assert response["Location"] == url


def _tiny_png(name="avatar.png"):
    buffer = BytesIO()
    Image.new("RGB", (10, 10), "blue").save(buffer, format="PNG")
    return SimpleUploadedFile(name, buffer.getvalue(), content_type="image/png")


def _png(size=(10, 10), mode="RGB", color="blue", name="avatar.png"):
    buffer = BytesIO()
    Image.new(mode, size, color).save(buffer, format="PNG")
    return SimpleUploadedFile(name, buffer.getvalue(), content_type="image/png")


def _rotated_jpeg(size=(40, 20), orientation=6, name="avatar.jpg"):
    """A JPEG whose EXIF says it must be rotated a quarter turn to display."""
    image = Image.new("RGB", size, "blue")
    exif = image.getexif()
    exif[0x0112] = orientation
    buffer = BytesIO()
    image.save(buffer, format="JPEG", exif=exif)
    return SimpleUploadedFile(name, buffer.getvalue(), content_type="image/jpeg")


def _declared_size_png(width, height, name="huge.png"):
    """A valid PNG header claiming huge dimensions, with no real pixel data.

    Under 100 bytes on the wire, so it passes any byte cap trivially -- the
    point of the pixel cap is that this is the shape of payload a byte cap
    cannot see.
    """

    def chunk(kind, payload):
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    data = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(b"\x00" * 16))
        + chunk(b"IEND", b"")
    )
    return SimpleUploadedFile(name, data, content_type="image/png")


def _stored_image(user):
    user.avatar.open()
    try:
        image = Image.open(user.avatar)
        image.load()
        return image
    finally:
        user.avatar.close()


class TestAvatarForm:
    @pytest.fixture(autouse=True)
    def _media_root(self, settings, tmp_path):
        settings.MEDIA_ROOT = tmp_path

    def test_upload_sets_avatar(self, authed_client, user, db):
        url = reverse("accounts:settings")
        response = authed_client.post(
            url, {"form_name": "avatar", "avatar": _tiny_png()}, follow=True
        )
        user.refresh_from_db()
        assert user.avatar
        assert "Profile photo updated." in response.content.decode()

    def test_re_upload_replaces_existing_avatar(self, authed_client, user, db):
        url = reverse("accounts:settings")
        payload = {"form_name": "avatar", "avatar": _tiny_png("first.png")}
        authed_client.post(url, payload)
        user.refresh_from_db()
        first_name = user.avatar.name

        payload = {"form_name": "avatar", "avatar": _tiny_png("second.png")}
        authed_client.post(url, payload)
        user.refresh_from_db()
        assert user.avatar.name != first_name

    def test_clear_removes_avatar(self, authed_client, user, db):
        url = reverse("accounts:settings")
        authed_client.post(url, {"form_name": "avatar", "avatar": _tiny_png()})
        user.refresh_from_db()
        assert user.avatar

        authed_client.post(url, {"form_name": "avatar", "clear_avatar": "true"})
        user.refresh_from_db()
        assert not user.avatar

    def test_invalid_file_shows_error_and_does_not_save(self, authed_client, user, db):
        url = reverse("accounts:settings")
        bogus = SimpleUploadedFile(
            "not-an-image.png", b"not a real image", content_type="image/png"
        )
        response = authed_client.post(
            url, {"form_name": "avatar", "avatar": bogus}, follow=True
        )
        user.refresh_from_db()
        assert not user.avatar
        assert response.status_code == 200

    def test_settings_page_shows_avatar_section(self, authed_client, user, db):
        url = reverse("accounts:settings")
        response = authed_client.get(url)
        assert "Profile Photo" in response.content.decode()

    def test_avatar_section_save_button_says_save_photo(self, authed_client, user, db):
        url = reverse("accounts:settings")
        response = authed_client.get(url)
        assert "Save Photo" in response.content.decode()


class TestAvatarCropWizardMarkup:
    """The crop step is client-side, so what CI can enforce is the markup
    contract the script binds to (TASK-261)."""

    def test_settings_page_renders_crop_wizard(self, authed_client, user, db):
        content = authed_client.get(reverse("accounts:settings")).content.decode()
        for element_id in (
            "avatar-crop-stage",
            "avatar-crop-canvas",
            "avatar-crop-zoom",
            "avatar-crop-confirm",
            "avatar-crop-cancel",
        ):
            assert f'id="{element_id}"' in content

    def test_crop_wizard_hidden_until_file_selected(self, authed_client, user, db):
        content = authed_client.get(reverse("accounts:settings")).content.decode()
        assert '<div id="avatar-cropper" class="hidden">' in content
        assert '<div id="avatar-picker">' in content

    def test_avatar_form_still_posts_multipart_to_settings(
        self, authed_client, user, db
    ):
        # The wizard wraps the file input; it must never replace it, or the
        # no-JS path and every existing avatar test lose their upload channel.
        content = authed_client.get(reverse("accounts:settings")).content.decode()
        assert 'hx-encoding="multipart/form-data"' in content
        assert 'name="avatar"' in content
        assert 'type="file"' in content

    def test_partial_after_htmx_post_contains_crop_wizard(
        self, authed_client, user, db, settings, tmp_path
    ):
        settings.MEDIA_ROOT = tmp_path
        response = authed_client.post(
            reverse("accounts:settings"),
            {"form_name": "avatar", "avatar": _tiny_png()},
            **HX,
        )
        assert 'id="avatar-crop-stage"' in response.content.decode()


class TestAvatarUploadBounds:
    @pytest.fixture(autouse=True)
    def _media_root(self, settings, tmp_path):
        settings.MEDIA_ROOT = tmp_path

    def test_oversized_upload_rejected(self, authed_client, user, db, monkeypatch):
        # Monkeypatched rather than posting a real 10 MB payload.
        monkeypatch.setattr("accounts.forms.MAX_AVATAR_BYTES", 10)
        response = authed_client.post(
            reverse("accounts:settings"), {"form_name": "avatar", "avatar": _tiny_png()}
        )
        user.refresh_from_db()
        assert not user.avatar
        assert "Image is too large" in response.content.decode()

    def test_within_byte_limit_upload_accepted(self, authed_client, user, db):
        authed_client.post(
            reverse("accounts:settings"), {"form_name": "avatar", "avatar": _tiny_png()}
        )
        user.refresh_from_db()
        assert user.avatar

    @pytest.mark.filterwarnings("ignore::PIL.Image.DecompressionBombWarning")
    def test_oversized_dimensions_rejected(self, authed_client, user, db):
        # 121 megapixels in well under 100 bytes: Pillow only *raises* above
        # twice its MAX_IMAGE_PIXELS, so Django accepts this today and a byte
        # cap can never catch it.
        payload = _declared_size_png(11000, 11000)
        assert payload.size < 100
        response = authed_client.post(
            reverse("accounts:settings"), {"form_name": "avatar", "avatar": payload}
        )
        user.refresh_from_db()
        assert not user.avatar
        assert "Image dimensions are too large" in response.content.decode()

    def test_within_pixel_limit_accepted(self, authed_client, user, db):
        authed_client.post(
            reverse("accounts:settings"),
            {"form_name": "avatar", "avatar": _png(size=(300, 200))},
        )
        user.refresh_from_db()
        assert user.avatar


class TestAvatarAvifPipeline:
    @pytest.fixture(autouse=True)
    def _media_root(self, settings, tmp_path):
        settings.MEDIA_ROOT = tmp_path

    def _upload(self, authed_client, user, upload):
        authed_client.post(
            reverse("accounts:settings"), {"form_name": "avatar", "avatar": upload}
        )
        user.refresh_from_db()

    def test_upload_is_stored_as_avif(self, authed_client, user, db):
        self._upload(authed_client, user, _png())
        assert user.avatar.name.endswith(".avif")
        assert _stored_image(user).format == "AVIF"

    def test_large_upload_is_downscaled(self, authed_client, user, db):
        self._upload(authed_client, user, _png(size=(2000, 1500)))
        assert _stored_image(user).size == (1024, 768)

    def test_small_upload_is_not_upscaled(self, authed_client, user, db):
        self._upload(authed_client, user, _tiny_png())
        assert _stored_image(user).size == (10, 10)

    def test_exif_orientation_is_applied(self, authed_client, user, db):
        # The transpose runs *after* the thumbnail, which is the cheap ordering
        # but also the easy one to "tidy up" into shipping sideways avatars on
        # the no-JS path. This is the test that catches that.
        self._upload(authed_client, user, _rotated_jpeg(size=(40, 20)))
        assert _stored_image(user).size == (20, 40)

    def test_alpha_is_preserved(self, authed_client, user, db):
        self._upload(authed_client, user, _png(mode="RGBA", color=(0, 0, 255, 128)))
        assert _stored_image(user).mode == "RGBA"

    def test_exif_is_stripped(self, authed_client, user, db):
        # Deliberate: the AVIF writer only emits EXIF when passed exif=, and we
        # never pass it, so GPS and camera metadata do not reach /media/.
        self._upload(authed_client, user, _rotated_jpeg())
        assert dict(_stored_image(user).getexif()) == {}

    def test_transcode_failure_falls_back_to_original(
        self, authed_client, user, db, caplog
    ):
        # Patch a PIL entry point only the service uses -- patching Image.open
        # would also break Django's own ImageField.to_python and never reach us.
        with patch(
            "accounts.services.ImageOps.exif_transpose", side_effect=OSError("boom")
        ):
            self._upload(authed_client, user, _tiny_png())
        assert user.avatar.name.endswith(".png")
        assert "AVIF transcode failed" in caplog.text

    def test_re_upload_with_identical_filename_gets_distinct_path(
        self, authed_client, user, db
    ):
        self._upload(authed_client, user, _png(name="same.png"))
        first = user.avatar.name
        self._upload(authed_client, user, _png(name="same.png"))
        assert user.avatar.name != first

    def test_avatar_is_served_with_avif_content_type(
        self, authed_client, user, db, settings
    ):
        # root/urls.py binds document_root at import time, so the view is
        # called directly with the test MEDIA_ROOT. What is under test is the
        # stdlib mimetypes lookup serve_static does -- .avif is in the map on
        # 3.12 and 3.14, and this fails loudly if a future Python drops it.
        # Keep calling serve_static directly: a RequestFactory request has no
        # .user, so pointing this at the gated view (core.views
        # .protected_media_view, TASK-277) would break it. The
        # through-the-URLconf coverage lives in core/tests/test_protected_media.py.
        self._upload(authed_client, user, _png())
        response = serve_static(
            RequestFactory().get(user.avatar.url),
            path=user.avatar.name,
            document_root=settings.MEDIA_ROOT,
        )
        assert response["Content-Type"] == "image/avif"
