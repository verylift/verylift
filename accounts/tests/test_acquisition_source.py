"""Tests for User.acquisition_source across every account-creation site (TASK-249)."""

from datetime import timedelta
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import Client, RequestFactory
from django.urls import reverse
from django.utils import timezone

from accounts.auth import OIDCBackend
from accounts.tests.factories import UserFactory
from challenges.models import Challenge, ChallengeParticipant
from challenges.tests.factories import ChallengeFactory, ChallengeInviteLinkFactory

User = get_user_model()


def _register_data(**overrides):
    data = {
        "username": "newlifter",
        "password": "s3cret-pass",
        "password_confirm": "s3cret-pass",
        "liftosaur_api_key": "",
        "accept_terms": "on",
    }
    data.update(overrides)
    return data


@pytest.mark.django_db
class TestRegisterViewAcquisitionSource:
    @patch("accounts.views.validate_liftosaur_key")
    def test_direct_registration_records_direct(self, mock_validate):
        c = Client()
        c.post(reverse("accounts:register"), _register_data())
        user = User.objects.get(username="newlifter")
        assert user.acquisition_source == User.AcquisitionSource.DIRECT

    def test_invite_link_registration_records_invite_link_and_redirects_to_join(
        self,
    ):
        challenge = ChallengeFactory(status=Challenge.Status.ACTIVE)
        link = ChallengeInviteLinkFactory(
            challenge=challenge,
            revoked_at=None,
            expires_at=timezone.now() + timedelta(days=7),
        )
        c = Client()
        session = c.session
        session["invite_token"] = link.token
        session.save()

        response = c.post(reverse("accounts:register"), _register_data())

        assert response.status_code == 302
        assert response["Location"] == reverse(
            "challenges:invite-link", args=[link.token]
        )
        user = User.objects.get(username="newlifter")
        assert user.acquisition_source == User.AcquisitionSource.INVITE_LINK

    def test_invite_challenge_banner_shown_on_get(self):
        challenge = ChallengeFactory(
            status=Challenge.Status.ACTIVE, name="Spring Showdown"
        )
        link = ChallengeInviteLinkFactory(
            challenge=challenge,
            revoked_at=None,
            expires_at=timezone.now() + timedelta(days=7),
        )
        c = Client()
        session = c.session
        session["invite_token"] = link.token
        session.save()

        response = c.get(reverse("accounts:register"))
        assert b"Spring Showdown" in response.content

    def test_invite_link_bypasses_registration_closed(self, settings):
        settings.REGISTRATION_OPEN = False
        challenge = ChallengeFactory(status=Challenge.Status.ACTIVE)
        link = ChallengeInviteLinkFactory(
            challenge=challenge,
            revoked_at=None,
            expires_at=timezone.now() + timedelta(days=7),
        )
        c = Client()
        session = c.session
        session["invite_token"] = link.token
        session.save()

        response = c.get(reverse("accounts:register"))
        assert response.status_code == 200
        assert b"Registration is currently closed" not in response.content

    def test_stale_token_does_not_bypass_registration_closed(self, settings):
        settings.REGISTRATION_OPEN = False
        c = Client()
        session = c.session
        session["invite_token"] = "does-not-exist"
        session.save()

        response = c.get(reverse("accounts:register"))
        assert response.status_code == 200
        assert b"Registration is currently closed" in response.content


@pytest.mark.django_db
class TestOIDCBackendAcquisitionSource:
    def setup_method(self):
        self.backend = OIDCBackend()

    def test_oidc_signup_without_invite_records_oidc(self, settings):
        settings.REGISTRATION_OPEN = True
        user = self.backend.create_user(
            {"email": "oscar@example.com", "sub": "sub-oscar"}
        )
        assert user.acquisition_source == User.AcquisitionSource.OIDC

    def test_oidc_signup_with_invite_token_records_invite_link(self, settings):
        settings.REGISTRATION_OPEN = True
        challenge = ChallengeFactory(status=Challenge.Status.ACTIVE)
        link = ChallengeInviteLinkFactory(
            challenge=challenge,
            revoked_at=None,
            expires_at=timezone.now() + timedelta(days=7),
        )
        request = RequestFactory().get("/oidc/callback/")
        request.session = Client().session
        request.session["invite_token"] = link.token
        request.session.save()
        self.backend.request = request

        user = self.backend.create_user(
            {"email": "priya@example.com", "sub": "sub-priya"}
        )
        assert user.acquisition_source == User.AcquisitionSource.INVITE_LINK

    def test_invite_token_bypasses_closed_registration(self, settings):
        settings.REGISTRATION_OPEN = False
        settings.OIDC_AUTO_ENROLL_GROUP = ""
        challenge = ChallengeFactory(status=Challenge.Status.ACTIVE)
        link = ChallengeInviteLinkFactory(
            challenge=challenge,
            revoked_at=None,
            expires_at=timezone.now() + timedelta(days=7),
        )
        request = RequestFactory().get("/oidc/callback/")
        request.session = Client().session
        request.session["invite_token"] = link.token
        request.session.save()
        self.backend.request = request

        user = self.backend.create_user(
            {"email": "quinn@example.com", "sub": "sub-quinn"}
        )
        assert user is not None
        assert user.acquisition_source == User.AcquisitionSource.INVITE_LINK

    def test_stale_token_does_not_bypass_closed_registration(self, settings):
        settings.REGISTRATION_OPEN = False
        settings.OIDC_AUTO_ENROLL_GROUP = ""
        request = RequestFactory().get("/oidc/callback/")
        request.session = Client().session
        request.session["invite_token"] = "does-not-exist"
        request.session.save()
        self.backend.request = request

        user = self.backend.create_user({"email": "rex@example.com", "sub": "sub-rex"})
        assert user is None


@pytest.mark.django_db
class TestManagementCommandAcquisitionSource:
    def test_create_local_user_records_admin(self, capsys):
        call_command("create_local_user", username="cliadmin", password="pass")
        user = User.objects.get(username="cliadmin")
        assert user.acquisition_source == User.AcquisitionSource.ADMIN

    def test_seed_demo_data_records_admin(self):
        call_command("seed_demo_data")
        user = User.objects.filter(is_active=True).exclude(
            acquisition_source=User.AcquisitionSource.ADMIN
        )
        assert not user.exists()


@pytest.mark.django_db
class TestJoinedViaLinkProvenance:
    def test_participant_records_the_link_that_admitted_them(self):
        challenge = ChallengeFactory(status=Challenge.Status.ACTIVE)
        link = ChallengeInviteLinkFactory(
            challenge=challenge,
            revoked_at=None,
            expires_at=timezone.now() + timedelta(days=7),
        )
        user = UserFactory(liftosaur_api_key="test-key")
        c = Client()
        c.force_login(user)
        c.post(reverse("challenges:invite-accept", args=[link.token]))

        participant = ChallengeParticipant.objects.get(challenge=challenge, user=user)
        assert participant.joined_via_link_id == link.pk
