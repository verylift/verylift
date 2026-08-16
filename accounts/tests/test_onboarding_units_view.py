"""Tests for the onboarding units step (final onboarding step)."""

from datetime import timedelta

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from accounts.tests.factories import UserFactory
from challenges.models import Challenge
from challenges.tests.factories import ChallengeFactory, ChallengeInviteLinkFactory


@pytest.fixture
def client():
    return Client()


@pytest.mark.django_db
class TestOnboardingUnitsView:
    def test_anonymous_get_redirects_to_login(self, client):
        response = client.get(reverse("accounts:onboarding-units"))
        assert response.status_code == 302
        assert response["Location"].startswith(reverse("accounts:login"))

    def test_get_defaults_to_lb_pre_checked_regardless_of_stored_preference(
        self, client
    ):
        user = UserFactory()
        assert user.unit_preference == "kg"
        client.force_login(user)

        response = client.get(reverse("accounts:onboarding-units"))

        assert response.status_code == 200
        assert response.context["unit_preference"] == "lb"

    def test_post_saves_lb_preference(self, client):
        user = UserFactory()
        client.force_login(user)

        client.post(reverse("accounts:onboarding-units"), {"unit_preference": "lb"})

        user.refresh_from_db()
        assert user.unit_preference == "lb"

    def test_post_saves_kg_preference(self, client):
        user = UserFactory()
        client.force_login(user)

        client.post(reverse("accounts:onboarding-units"), {"unit_preference": "kg"})

        user.refresh_from_db()
        assert user.unit_preference == "kg"

    def test_post_redirects_to_dashboard_without_invite_token(self, client):
        client.force_login(UserFactory())

        response = client.post(
            reverse("accounts:onboarding-units"), {"unit_preference": "lb"}
        )

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

        response = client.post(
            reverse("accounts:onboarding-units"), {"unit_preference": "lb"}
        )

        assert response.status_code == 302
        assert response["Location"] == reverse(
            "challenges:invite-link", args=[link.token]
        )
