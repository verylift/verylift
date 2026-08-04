from datetime import timedelta
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import pytest
from django.contrib.auth import get_user_model
from django.contrib.sessions.backends.db import SessionStore
from django.http import HttpResponseRedirect
from django.shortcuts import resolve_url
from django.test import RequestFactory
from django.urls import reverse
from django.utils import timezone

from accounts.auth import (
    OIDCBackend,
    OIDCCallbackView,
    build_oidc_logout_url,
    generate_username,
)
from accounts.tests.factories import UserFactory
from challenges.models import Challenge
from challenges.tests.factories import ChallengeFactory, ChallengeInviteLinkFactory

User = get_user_model()


class TestGenerateUsername:
    def test_splits_email_at_at_sign(self):
        assert generate_username("alice@example.com") == "alice"

    def test_empty_string_returns_user(self):
        assert generate_username("") == "user"

    def test_none_returns_user(self):
        assert generate_username(None) == "user"


@pytest.mark.django_db
class TestOIDCBackend:
    def setup_method(self):
        self.backend = OIDCBackend()

    def test_get_username_from_email_claim(self):
        claims = {"email": "bob@example.com"}
        assert self.backend.get_username(claims) == "bob"

    def test_create_user_sets_email_and_display_name(self, settings):
        settings.REGISTRATION_OPEN = True
        claims = {
            "email": "carol@example.com",
            "sub": "sub-carol",
            "preferred_username": "Carol C",
        }
        user = self.backend.create_user(claims)
        assert user.email == "carol@example.com"
        assert user.display_name == "Carol C"
        assert user.oidc_sub == "sub-carol"

    def test_create_user_falls_back_name_claim(self, settings):
        settings.REGISTRATION_OPEN = True
        claims = {
            "email": "dave@example.com",
            "sub": "sub-dave",
            "name": "Dave D",
        }
        user = self.backend.create_user(claims)
        assert user.display_name == "Dave D"

    def test_update_user_updates_email_and_display_name(self):
        user = UserFactory(email="old@example.com", display_name="Old Name")
        claims = {"email": "new@example.com", "preferred_username": "New Name"}
        updated = self.backend.update_user(user, claims)
        assert updated.email == "new@example.com"
        assert updated.display_name == "New Name"

    def test_update_user_keeps_existing_email_if_not_in_claims(self):
        user = UserFactory(email="keep@example.com", display_name="Keep Name")
        updated = self.backend.update_user(user, {})
        assert updated.email == "keep@example.com"

    def test_filter_users_by_claims_returns_matching_user(self):
        user = UserFactory(oidc_sub="sub-123")
        result = self.backend.filter_users_by_claims({"sub": "sub-123"})
        assert user in result

    def test_filter_users_by_claims_returns_empty_when_no_sub(self):
        result = self.backend.filter_users_by_claims({})
        assert result.count() == 0

    def test_filter_users_by_claims_matches_existing_user_regardless_of_flag(
        self, settings
    ):
        settings.REGISTRATION_OPEN = False
        user = UserFactory(oidc_sub="sub-existing")
        result = self.backend.filter_users_by_claims({"sub": "sub-existing"})
        assert user in result


@pytest.mark.django_db
class TestOIDCBackendRegistrationGate:
    """AC#2/#3/#4: closed-registration gate and grant-based OIDC exception."""

    def setup_method(self):
        self.backend = OIDCBackend()

    def test_create_user_allowed_when_registration_open(self, settings):
        settings.REGISTRATION_OPEN = True
        settings.OIDC_AUTO_ENROLL_GROUP = ""
        claims = {"email": "erin@example.com", "sub": "sub-erin"}

        user = self.backend.create_user(claims)

        assert user is not None
        assert User.objects.filter(username="erin").exists()

    def test_create_user_rejected_when_closed_and_no_group_configured(self, settings):
        settings.REGISTRATION_OPEN = False
        settings.OIDC_AUTO_ENROLL_GROUP = ""
        claims = {"email": "frank@example.com", "sub": "sub-frank"}

        user = self.backend.create_user(claims)

        assert user is None
        assert not User.objects.filter(username="frank").exists()

    def test_create_user_rejected_when_closed_and_claims_lack_qualifying_group(
        self, settings
    ):
        settings.REGISTRATION_OPEN = False
        settings.OIDC_AUTO_ENROLL_GROUP = "lifters"
        claims = {
            "email": "grace@example.com",
            "sub": "sub-grace",
            "groups": ["other-group"],
        }

        user = self.backend.create_user(claims)

        assert user is None
        assert not User.objects.filter(username="grace").exists()

    def test_create_user_allowed_when_closed_but_claims_have_qualifying_group(
        self, settings
    ):
        settings.REGISTRATION_OPEN = False
        settings.OIDC_AUTO_ENROLL_GROUP = "lifters"
        claims = {
            "email": "heidi@example.com",
            "sub": "sub-heidi",
            "groups": ["lifters", "other-group"],
        }

        user = self.backend.create_user(claims)

        assert user is not None
        assert User.objects.filter(username="heidi").exists()

    def test_create_user_ignores_group_check_when_registration_open(self, settings):
        settings.REGISTRATION_OPEN = True
        settings.OIDC_AUTO_ENROLL_GROUP = "lifters"
        claims = {"email": "ivan@example.com", "sub": "sub-ivan"}

        user = self.backend.create_user(claims)

        assert user is not None
        assert User.objects.filter(username="ivan").exists()

    def test_create_user_flags_request_when_rejected(self, settings):
        settings.REGISTRATION_OPEN = False
        settings.OIDC_AUTO_ENROLL_GROUP = ""
        request = RequestFactory().get("/oidc/callback/")
        self.backend.request = request

        self.backend.create_user({"email": "judy@example.com", "sub": "sub-judy"})

        assert request.oidc_registration_closed is True

    def test_create_user_does_not_crash_without_a_request_attribute(self, settings):
        settings.REGISTRATION_OPEN = False
        settings.OIDC_AUTO_ENROLL_GROUP = ""

        user = self.backend.create_user(
            {"email": "kevin@example.com", "sub": "sub-kevin"}
        )

        assert user is None

    def _request_with_invite_token(self, token):
        request = RequestFactory().get("/oidc/callback/")
        request.session = SessionStore()
        request.session["invite_token"] = token
        request.session.save()
        return request

    def test_create_user_allowed_when_closed_with_usable_invite_token(self, settings):
        """TASK-249: a challenge invite link doubles as a beta invite."""
        settings.REGISTRATION_OPEN = False
        settings.OIDC_AUTO_ENROLL_GROUP = ""
        challenge = ChallengeFactory(status=Challenge.Status.ACTIVE)
        link = ChallengeInviteLinkFactory(
            challenge=challenge,
            revoked_at=None,
            expires_at=timezone.now() + timedelta(days=7),
        )
        self.backend.request = self._request_with_invite_token(link.token)

        user = self.backend.create_user(
            {"email": "olga@example.com", "sub": "sub-olga"}
        )

        assert user is not None
        assert User.objects.filter(username="olga").exists()

    def test_create_user_rejected_when_closed_with_stale_invite_token(self, settings):
        settings.REGISTRATION_OPEN = False
        settings.OIDC_AUTO_ENROLL_GROUP = ""
        self.backend.request = self._request_with_invite_token("does-not-exist")

        user = self.backend.create_user(
            {"email": "pete@example.com", "sub": "sub-pete"}
        )

        assert user is None

    def test_create_user_does_not_crash_when_request_has_no_session(self, settings):
        """A bare RequestFactory request has no .session until SessionMiddleware
        runs -- OIDCBackend.create_user must not assume one is present."""
        settings.REGISTRATION_OPEN = False
        settings.OIDC_AUTO_ENROLL_GROUP = ""
        request = RequestFactory().get("/oidc/callback/")
        self.backend.request = request

        user = self.backend.create_user(
            {"email": "quincy@example.com", "sub": "sub-quincy"}
        )

        assert user is None
        assert request.oidc_registration_closed is True


@pytest.mark.django_db
class TestOIDCBackendAdminGroupSync:
    """OIDC_ADMIN_GROUP: grants/revokes full admin access from a groups claim."""

    def setup_method(self):
        self.backend = OIDCBackend()

    def test_create_user_grants_admin_when_claims_have_admin_group(self, settings):
        settings.REGISTRATION_OPEN = True
        settings.OIDC_ADMIN_GROUP = "verylift-admins"
        claims = {
            "email": "laura@example.com",
            "sub": "sub-laura",
            "groups": ["verylift-admins", "other-group"],
        }

        user = self.backend.create_user(claims)

        assert user.is_staff is True
        assert user.is_superuser is True

    def test_create_user_does_not_grant_admin_without_qualifying_group(self, settings):
        settings.REGISTRATION_OPEN = True
        settings.OIDC_ADMIN_GROUP = "verylift-admins"
        claims = {
            "email": "mallory@example.com",
            "sub": "sub-mallory",
            "groups": ["other-group"],
        }

        user = self.backend.create_user(claims)

        assert user.is_staff is False
        assert user.is_superuser is False

    def test_create_user_does_not_grant_admin_when_feature_unset(self, settings):
        settings.REGISTRATION_OPEN = True
        settings.OIDC_ADMIN_GROUP = ""
        claims = {
            "email": "nathan@example.com",
            "sub": "sub-nathan",
            "groups": ["verylift-admins"],
        }

        user = self.backend.create_user(claims)

        assert user.is_staff is False
        assert user.is_superuser is False

    def test_update_user_grants_admin_when_membership_gained(self, settings):
        settings.OIDC_ADMIN_GROUP = "verylift-admins"
        user = UserFactory(is_staff=False, is_superuser=False)
        claims = {"groups": ["verylift-admins"]}

        updated = self.backend.update_user(user, claims)

        assert updated.is_staff is True
        assert updated.is_superuser is True

    def test_update_user_revokes_admin_when_membership_lost(self, settings):
        settings.OIDC_ADMIN_GROUP = "verylift-admins"
        user = UserFactory(is_staff=True, is_superuser=True)
        claims = {"groups": ["other-group"]}

        updated = self.backend.update_user(user, claims)

        assert updated.is_staff is False
        assert updated.is_superuser is False

    def test_update_user_skips_sync_when_groups_claim_entirely_absent(self, settings):
        """A missing "groups" key (vs. present-but-empty) means the Authentik
        scope prerequisite likely isn't set up -- must not be treated as
        "user is in zero groups" and silently revoke an existing admin."""
        settings.OIDC_ADMIN_GROUP = "verylift-admins"
        user = UserFactory(is_staff=True, is_superuser=True)
        claims = {"email": user.email}

        updated = self.backend.update_user(user, claims)

        assert updated.is_staff is True
        assert updated.is_superuser is True

    def test_update_user_does_not_touch_admin_flags_when_feature_unset(self, settings):
        settings.OIDC_ADMIN_GROUP = ""
        user = UserFactory(is_staff=True, is_superuser=True)
        claims = {"groups": []}

        updated = self.backend.update_user(user, claims)

        assert updated.is_staff is True
        assert updated.is_superuser is True


class TestOIDCCallbackView:
    def test_login_failure_redirects_to_register_when_registration_closed(self):
        request = RequestFactory().get("/oidc/callback/")
        request.oidc_registration_closed = True
        view = OIDCCallbackView()
        view.request = request

        response = view.login_failure()

        assert response.status_code == 302
        assert response.url == reverse("accounts:register")

    def test_login_failure_falls_back_to_default_for_other_failures(self):
        request = RequestFactory().get("/oidc/callback/")
        view = OIDCCallbackView()
        view.request = request

        response = view.login_failure()

        assert response.status_code == 302
        assert response.url == "/"

    @pytest.mark.django_db
    def test_login_success_redirects_to_invite_link_when_token_usable(self):
        """TASK-249: an SSO signup/login started from a challenge invite link
        must land back in the join flow, not wherever OIDC normally sends it."""
        challenge = ChallengeFactory(status=Challenge.Status.ACTIVE)
        link = ChallengeInviteLinkFactory(
            challenge=challenge,
            revoked_at=None,
            expires_at=timezone.now() + timedelta(days=7),
        )
        request = RequestFactory().get("/oidc/callback/")
        request.session = SessionStore()
        request.session["invite_token"] = link.token
        request.session.save()
        view = OIDCCallbackView()
        view.request = request

        with patch(
            "mozilla_django_oidc.views.OIDCAuthenticationCallbackView.login_success",
            return_value=HttpResponseRedirect("/somewhere/"),
        ):
            response = view.login_success()

        assert response.status_code == 302
        assert response.url == reverse("challenges:invite-link", args=[link.token])

    def test_login_success_falls_back_to_default_without_a_token(self):
        request = RequestFactory().get("/oidc/callback/")
        request.session = SessionStore()
        view = OIDCCallbackView()
        view.request = request

        default_response = HttpResponseRedirect("/dashboard/")
        with patch(
            "mozilla_django_oidc.views.OIDCAuthenticationCallbackView.login_success",
            return_value=default_response,
        ):
            response = view.login_success()

        assert response is default_response


@pytest.mark.django_db
class TestBuildOIDCLogoutURL:
    def _make_request(self, settings, id_token=None):
        settings.OIDC_OP_LOGOUT_ENDPOINT = (
            "https://auth.example.com/application/o/verylift/end-session/"
        )
        settings.OIDC_RP_CLIENT_ID = "test-client-id"
        request = RequestFactory().get("/oidc/logout/")
        request.user = UserFactory(oidc_sub="sub-1")
        request.session = SessionStore()
        if id_token is not None:
            request.session["oidc_id_token"] = id_token
        return request

    def test_returns_end_session_url_with_hint_and_client_id_when_token_present(
        self, settings
    ):
        request = self._make_request(settings, id_token="tok-123")
        url = build_oidc_logout_url(request)

        assert url.startswith(settings.OIDC_OP_LOGOUT_ENDPOINT)
        query = parse_qs(urlparse(url).query)
        assert query["id_token_hint"] == ["tok-123"]
        assert query["client_id"] == [settings.OIDC_RP_CLIENT_ID]
        # TASK-270: omitted deliberately -- sending it makes Authentik 2026.5.6
        # reject the whole end-session request as malformed (400).
        assert "post_logout_redirect_uri" not in query

    def test_falls_back_to_local_logout_url_when_id_token_missing(self, settings):
        request = self._make_request(settings, id_token=None)
        url = build_oidc_logout_url(request)

        assert url == resolve_url(settings.LOGOUT_REDIRECT_URL)
        assert settings.OIDC_OP_LOGOUT_ENDPOINT not in url
