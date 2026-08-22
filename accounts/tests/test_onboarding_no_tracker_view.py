"""Tests for the onboarding "no tracking app" Liftosaur suggestion step."""

import pytest
from django.test import Client
from django.urls import reverse

from accounts.tests.factories import UserFactory


@pytest.fixture
def client():
    return Client()


@pytest.mark.django_db
class TestOnboardingNoTrackerView:
    def test_anonymous_get_redirects_to_login(self, client):
        response = client.get(reverse("accounts:onboarding-no-tracker"))
        assert response.status_code == 302
        assert response["Location"].startswith(reverse("accounts:login"))

    def test_get_renders_page(self, client):
        client.force_login(UserFactory())
        response = client.get(reverse("accounts:onboarding-no-tracker"))
        assert response.status_code == 200

    def test_post_redirects_to_units_step(self, client):
        client.force_login(UserFactory())
        response = client.post(reverse("accounts:onboarding-no-tracker"))
        assert response.status_code == 302
        assert response["Location"] == reverse("accounts:onboarding-units")
