"""Tests for the onboarding Very Open invite step (final onboarding step)."""

from datetime import timedelta

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from accounts.tests.factories import UserFactory
from challenges.models import Challenge
from challenges.tests.factories import ChallengeFactory, ChallengeInviteLinkFactory
from core.models import SiteSettings


@pytest.fixture
def client():
    return Client()


@pytest.mark.django_db
class TestOnboardingVeryOpenView:
    def test_anonymous_get_redirects_to_login(self, client):
        response = client.get(reverse("accounts:onboarding-very-open"))
        assert response.status_code == 302
        assert response["Location"].startswith(reverse("accounts:login"))

    def test_get_skips_straight_to_dashboard_when_url_not_configured(self, client):
        client.force_login(UserFactory())

        response = client.get(reverse("accounts:onboarding-very-open"))

        assert response.status_code == 302
        assert response["Location"] == reverse("challenges:dashboard")

    def test_get_renders_invite_when_url_configured(self, client):
        site_settings = SiteSettings.load()
        site_settings.very_open_invite_url = "https://example.com/very-open-26"
        site_settings.very_open_label = "The Very Open '26"
        site_settings.save()
        client.force_login(UserFactory())

        response = client.get(reverse("accounts:onboarding-very-open"))

        assert response.status_code == 200
        assert (
            response.context["very_open_invite_url"]
            == "https://example.com/very-open-26"
        )
        assert response.context["very_open_label"] == "The Very Open '26"

    def test_post_redirects_to_dashboard_without_invite_token(self, client):
        site_settings = SiteSettings.load()
        site_settings.very_open_invite_url = "https://example.com/very-open-26"
        site_settings.save()
        client.force_login(UserFactory())

        response = client.post(reverse("accounts:onboarding-very-open"))

        assert response.status_code == 302
        assert response["Location"] == reverse("challenges:dashboard")

    def test_post_redirects_to_invite_link_when_session_has_usable_token(self, client):
        challenge = ChallengeFactory(status=Challenge.Status.ACTIVE)
        link = ChallengeInviteLinkFactory(
            challenge=challenge,
            revoked_at=None,
            expires_at=timezone.now() + timedelta(days=7),
        )
        client.force_login(UserFactory())
        session = client.session
        session["invite_token"] = link.token
        session.save()

        response = client.post(reverse("accounts:onboarding-very-open"))

        assert response.status_code == 302
        assert response["Location"] == reverse(
            "challenges:invite-link", args=[link.token]
        )
