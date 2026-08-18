"""Tests for the onboarding units step."""

import pytest
from django.test import Client
from django.urls import reverse

from accounts.tests.factories import UserFactory


@pytest.fixture
def client():
    return Client()


@pytest.mark.django_db
class TestOnboardingUnitsView:
    def test_anonymous_get_redirects_to_login(self, client):
        response = client.get(reverse("accounts:onboarding-units"))
        assert response.status_code == 302
        assert response["Location"].startswith(reverse("accounts:login"))

    def test_get_shows_lb_for_a_fresh_account(self, client):
        user = UserFactory()
        client.force_login(user)

        response = client.get(reverse("accounts:onboarding-units"))

        assert response.status_code == 200
        assert response.context["unit_preference"] == "lb"

    def test_get_shows_stored_kg_preference_instead_of_hardcoded_lb(self, client):
        user = UserFactory(unit_preference="kg")
        client.force_login(user)

        response = client.get(reverse("accounts:onboarding-units"))

        assert response.status_code == 200
        assert response.context["unit_preference"] == "kg"

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

    def test_post_redirects_to_very_open_step(self, client):
        client.force_login(UserFactory())

        response = client.post(
            reverse("accounts:onboarding-units"), {"unit_preference": "lb"}
        )

        assert response.status_code == 302
        assert response["Location"] == reverse("accounts:onboarding-very-open")

    def test_get_does_not_render_app_sidebar_or_mobile_nav(self, client):
        user = UserFactory()
        client.force_login(user)

        response = client.get(reverse("accounts:onboarding-units"))

        content = response.content.decode()
        assert 'id="app-sidebar"' not in content
        assert 'id="mobile-nav-panel"' not in content
