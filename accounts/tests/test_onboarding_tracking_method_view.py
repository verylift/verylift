"""Tests for the onboarding tracking-method step."""

import pytest
from django.test import Client
from django.urls import reverse

from accounts.tests.factories import UserFactory


@pytest.fixture
def client():
    return Client()


@pytest.mark.django_db
class TestOnboardingTrackingMethodView:
    def test_anonymous_get_redirects_to_login(self, client):
        response = client.get(reverse("accounts:onboarding-tracking-method"))
        assert response.status_code == 302
        assert response["Location"].startswith(reverse("accounts:login"))

    def test_get_renders_form(self, client):
        client.force_login(UserFactory())
        response = client.get(reverse("accounts:onboarding-tracking-method"))
        assert response.status_code == 200
        assert b'name="tracking_method_choice"' in response.content

    def test_choosing_liftosaur_goes_to_liftosaur_step(self, client):
        client.force_login(UserFactory())
        response = client.post(
            reverse("accounts:onboarding-tracking-method"),
            {"tracking_method_choice": "liftosaur"},
        )
        assert response.status_code == 302
        assert response["Location"] == reverse("accounts:onboarding-liftosaur")

    @pytest.mark.parametrize("choice", ["manual", "csv", ""])
    def test_other_choices_skip_straight_to_units_step(self, client, choice):
        client.force_login(UserFactory())
        response = client.post(
            reverse("accounts:onboarding-tracking-method"),
            {"tracking_method_choice": choice},
        )
        assert response.status_code == 302
        assert response["Location"] == reverse("accounts:onboarding-units")
