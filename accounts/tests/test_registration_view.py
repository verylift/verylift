"""Tests for the self-serve registration view (TASK-68)."""

from datetime import timedelta
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from accounts.tests.factories import UserFactory
from challenges.models import Challenge
from challenges.tests.factories import ChallengeFactory, ChallengeInviteLinkFactory
from policies.models import Policy, PolicyConsent
from policies.tests.factories import PolicyFactory, PolicyVersionFactory

User = get_user_model()


@pytest.fixture
def client():
    return Client()


def _post_data(**overrides):
    data = {
        "username": "newlifter",
        "password": "s3cret-pass",
        "password_confirm": "s3cret-pass",
        "liftosaur_api_key": "valid-key",
        "accept_terms": "on",
    }
    data.update(overrides)
    return data


@pytest.mark.django_db
class TestRegistrationGet:
    def test_get_renders_form(self, client):
        response = client.get(reverse("accounts:register"))
        assert response.status_code == 200
        assert b"Create your account" in response.content

    def test_login_page_links_to_register(self, client):
        response = client.get(reverse("accounts:login"))
        assert reverse("accounts:register").encode() in response.content

    def test_authenticated_user_redirected_away(self, client):
        client.force_login(UserFactory())
        response = client.get(reverse("accounts:register"))
        assert response.status_code == 302
        assert response["Location"] == reverse("challenges:dashboard")

    def test_login_page_links_to_legal_pages(self, client):
        response = client.get(reverse("accounts:login"))
        assert reverse("terms").encode() in response.content
        assert reverse("privacy").encode() in response.content


@pytest.mark.django_db
class TestRegistrationSuccess:
    @patch("accounts.views.trigger_lift_history_backfill")
    @patch("accounts.views.validate_liftosaur_key", return_value=True)
    def test_valid_registration_with_key_triggers_backfill(
        self, mock_validate, mock_backfill, client
    ):
        response = client.post(reverse("accounts:register"), _post_data())

        assert response.status_code == 302
        assert response["Location"] == reverse("challenges:dashboard")
        mock_validate.assert_called_once_with("valid-key")
        user = User.objects.get(username="newlifter")
        mock_backfill.assert_called_once_with(user)
        assert user.liftosaur_api_key == "valid-key"
        assert user.check_password("s3cret-pass")
        # Accepting the terms during registration stamps the acknowledgement time.
        assert user.tos_accepted_at is not None
        # User is logged in automatically.
        assert client.session["_auth_user_id"] == str(user.id)

    @patch("accounts.views.trigger_lift_history_backfill")
    @patch("accounts.views.validate_liftosaur_key", return_value=True)
    def test_agreeing_at_signup_satisfies_the_policy_consent_gate(
        self, mock_validate, mock_backfill, client
    ):
        """The registration checkbox must not leave the new user immediately
        redirected to /policies/consent/ on their very next request."""
        tos_version = PolicyVersionFactory(
            policy=PolicyFactory(policy_type=Policy.PolicyType.TOS),
            is_active=True,
        )
        privacy_version = PolicyVersionFactory(
            policy=PolicyFactory(policy_type=Policy.PolicyType.PRIVACY),
            is_active=True,
        )

        client.post(reverse("accounts:register"), _post_data())

        user = User.objects.get(username="newlifter")
        assert PolicyConsent.objects.filter(
            user=user, policy_version=tos_version, method=PolicyConsent.Method.SIGNUP
        ).exists()
        assert PolicyConsent.objects.filter(
            user=user,
            policy_version=privacy_version,
            method=PolicyConsent.Method.SIGNUP,
        ).exists()

        response = client.get(reverse("challenges:dashboard"))
        assert response.status_code == 200


@pytest.mark.django_db
class TestRegistrationWithoutKey:
    """The Liftosaur API key is optional at signup (TASK-250)."""

    @patch("accounts.views.trigger_lift_history_backfill")
    @patch("accounts.views.validate_liftosaur_key")
    def test_registration_completes_without_a_key(
        self, mock_validate, mock_backfill, client
    ):
        response = client.post(
            reverse("accounts:register"), _post_data(liftosaur_api_key="")
        )

        assert response.status_code == 302
        assert response["Location"] == reverse("challenges:dashboard")
        user = User.objects.get(username="newlifter")
        assert user.liftosaur_api_key is None
        assert client.session["_auth_user_id"] == str(user.id)
        # No external call and no backfill attempted without a key.
        mock_validate.assert_not_called()
        mock_backfill.assert_not_called()

    def test_keyless_registration_shows_no_error(self, client):
        response = client.post(
            reverse("accounts:register"), _post_data(liftosaur_api_key="")
        )
        assert b"Liftosaur API key is required" not in response.content
        assert response.status_code == 302


@pytest.mark.django_db
class TestRegistrationEmail:
    """Email is optional at signup (TASK-283) and is the only thing that makes
    an account self-serve recoverable."""

    def test_email_is_persisted(self, client):
        client.post(
            reverse("accounts:register"),
            _post_data(liftosaur_api_key="", email="lifter@example.com"),
        )
        assert User.objects.get(username="newlifter").email == "lifter@example.com"

    def test_registration_still_succeeds_without_an_email(self, client):
        response = client.post(
            reverse("accounts:register"), _post_data(liftosaur_api_key="")
        )
        assert response.status_code == 302
        assert User.objects.get(username="newlifter").email == ""

    def test_malformed_email_blocks_account_creation(self, client):
        response = client.post(
            reverse("accounts:register"),
            _post_data(liftosaur_api_key="", email="notanemail"),
        )
        assert response.status_code == 200
        assert response.context["errors"]["email"]
        assert not User.objects.filter(username="newlifter").exists()

    def test_rejected_submission_keeps_the_typed_email(self, client):
        response = client.post(
            reverse("accounts:register"),
            _post_data(
                liftosaur_api_key="", email="lifter@example.com", accept_terms=""
            ),
        )
        assert response.context["values"]["email"] == "lifter@example.com"

    def test_registration_form_offers_an_email_field(self, client):
        response = client.get(reverse("accounts:register"))
        assert b'name="email"' in response.content


@pytest.mark.django_db
class TestRegistrationValidation:
    @patch("accounts.views.validate_liftosaur_key", return_value=False)
    def test_invalid_liftosaur_key_blocks_account_creation(self, mock_validate, client):
        response = client.post(reverse("accounts:register"), _post_data())

        assert response.status_code == 200
        assert b"Could not validate this Liftosaur API key." in response.content
        assert not User.objects.filter(username="newlifter").exists()

    @patch("accounts.views.validate_liftosaur_key")
    def test_duplicate_username_rejected(self, mock_validate, client):
        UserFactory(username="taken")
        response = client.post(
            reverse("accounts:register"), _post_data(username="taken")
        )

        assert response.status_code == 200
        assert b"already taken" in response.content
        # The Liftosaur API is never hit when cheap validation fails.
        mock_validate.assert_not_called()
        assert User.objects.filter(username="taken").count() == 1

    @patch("accounts.views.validate_liftosaur_key")
    def test_password_mismatch_rejected(self, mock_validate, client):
        response = client.post(
            reverse("accounts:register"),
            _post_data(password_confirm="different"),
        )

        assert response.status_code == 200
        assert b"Passwords do not match." in response.content
        mock_validate.assert_not_called()
        assert not User.objects.filter(username="newlifter").exists()

    @patch("accounts.views.validate_liftosaur_key")
    def test_unaccepted_terms_rejected(self, mock_validate, client):
        response = client.post(
            reverse("accounts:register"), _post_data(accept_terms="")
        )

        assert response.status_code == 200
        assert b"must accept the Terms of Service" in response.content
        assert not User.objects.filter(username="newlifter").exists()
        # The acknowledgement is checked before the (expensive) Liftosaur key check.
        mock_validate.assert_not_called()

    def test_register_page_links_to_legal_pages(self, client):
        response = client.get(reverse("accounts:register"))
        assert reverse("terms").encode() in response.content
        assert reverse("privacy").encode() in response.content

    @patch("accounts.views.validate_liftosaur_key")
    def test_short_password_rejected_by_validator(self, mock_validate, client):
        response = client.post(
            reverse("accounts:register"),
            _post_data(password="ab1", password_confirm="ab1"),
        )

        assert response.status_code == 200
        assert b"too short" in response.content
        assert not User.objects.filter(username="newlifter").exists()
        # The password is rejected before the (expensive) Liftosaur key check.
        mock_validate.assert_not_called()

    @patch("accounts.views.validate_liftosaur_key")
    def test_all_numeric_password_rejected_by_validator(self, mock_validate, client):
        response = client.post(
            reverse("accounts:register"),
            _post_data(password="24681012", password_confirm="24681012"),
        )

        assert response.status_code == 200
        assert b"entirely numeric" in response.content
        assert not User.objects.filter(username="newlifter").exists()
        mock_validate.assert_not_called()


@pytest.mark.django_db
class TestRegistrationClosed:
    def test_get_shows_closed_message_instead_of_form(self, client, settings):
        settings.REGISTRATION_OPEN = False
        response = client.get(reverse("accounts:register"))

        assert response.status_code == 200
        assert b"Registration is currently closed" in response.content
        assert b"Create your account" not in response.content

    @patch("accounts.views.validate_liftosaur_key")
    def test_post_does_not_create_account(self, mock_validate, client, settings):
        settings.REGISTRATION_OPEN = False
        response = client.post(reverse("accounts:register"), _post_data())

        assert response.status_code == 200
        assert b"Registration is currently closed" in response.content
        assert not User.objects.filter(username="newlifter").exists()
        mock_validate.assert_not_called()

    def test_authenticated_user_still_redirected_away_when_closed(
        self, client, settings
    ):
        settings.REGISTRATION_OPEN = False
        client.force_login(UserFactory())
        response = client.get(reverse("accounts:register"))

        assert response.status_code == 302
        assert response["Location"] == reverse("challenges:dashboard")

    def test_closed_page_links_to_login(self, client, settings):
        settings.REGISTRATION_OPEN = False
        response = client.get(reverse("accounts:register"))

        assert reverse("accounts:login").encode() in response.content

    @patch("accounts.views.trigger_lift_history_backfill")
    @patch("accounts.views.validate_liftosaur_key", return_value=True)
    def test_registration_open_true_preserves_current_behavior(
        self, mock_validate, mock_backfill, client, settings
    ):
        settings.REGISTRATION_OPEN = True
        response = client.post(reverse("accounts:register"), _post_data())

        assert response.status_code == 302
        assert response["Location"] == reverse("challenges:dashboard")
        assert User.objects.filter(username="newlifter").exists()

    def test_existing_local_user_can_log_in_when_registration_closed(
        self, client, settings
    ):
        settings.REGISTRATION_OPEN = False
        user = UserFactory(username="existing")
        user.set_password("s3cret-pass")
        user.save()

        response = client.post(
            reverse("accounts:login"),
            {"username": "existing", "password": "s3cret-pass"},
        )

        assert response.status_code == 302
        assert client.session["_auth_user_id"] == str(user.id)


@pytest.mark.django_db
class TestRegistrationClosedByOIDCOnlyLogin:
    """OIDC-only mode force-closes local signup regardless of REGISTRATION_OPEN.

    A local account created while the local login form is hidden could never be
    used to sign in through the app UI (TASK-233, D1).
    """

    def test_get_shows_closed_message_even_when_registration_open(
        self, client, settings
    ):
        settings.REGISTRATION_OPEN = True
        settings.OIDC_ONLY_LOGIN = True
        response = client.get(reverse("accounts:register"))

        assert response.status_code == 200
        assert b"Registration is currently closed" in response.content
        assert b"Create your account" not in response.content

    def test_usable_invite_token_does_not_bypass_oidc_only_login(
        self, client, settings
    ):
        """TASK-249 correction 0: the invite-link bypass is REGISTRATION_OPEN-
        only. OIDC_ONLY_LOGIN force-closes local registration unconditionally
        -- pinned here so a future change to that fallback is deliberate, not
        accidental (proper support is TASK-271)."""
        settings.OIDC_ONLY_LOGIN = True
        challenge = ChallengeFactory(status=Challenge.Status.ACTIVE)
        link = ChallengeInviteLinkFactory(
            challenge=challenge,
            revoked_at=None,
            expires_at=timezone.now() + timedelta(days=7),
        )
        session = client.session
        session["invite_token"] = link.token
        session.save()

        response = client.get(reverse("accounts:register"))

        assert response.status_code == 200
        assert b"Registration is currently closed" in response.content
        assert b"Create your account" not in response.content

    @patch("accounts.views.validate_liftosaur_key")
    def test_post_does_not_create_account_even_when_registration_open(
        self, mock_validate, client, settings
    ):
        settings.REGISTRATION_OPEN = True
        settings.OIDC_ONLY_LOGIN = True
        response = client.post(reverse("accounts:register"), _post_data())

        assert response.status_code == 200
        assert not User.objects.filter(username="newlifter").exists()
        mock_validate.assert_not_called()
