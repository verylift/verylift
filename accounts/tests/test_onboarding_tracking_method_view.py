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
        assert b'name="tracking_app"' in response.content

    @pytest.mark.parametrize("app", ["liftosaur", "wger", "hevy"])
    def test_choosing_a_tracking_app_goes_to_its_connect_step(self, client, app):
        client.force_login(UserFactory())
        response = client.post(
            reverse("accounts:onboarding-tracking-method"),
            {"tracking_app": app},
        )
        assert response.status_code == 302
        assert response["Location"] == reverse(
            "accounts:onboarding-connect-tracker", args=[app]
        )

    def test_choosing_other_goes_to_the_feedback_step(self, client):
        client.force_login(UserFactory())
        response = client.post(
            reverse("accounts:onboarding-tracking-method"),
            {"tracking_app": "other"},
        )
        assert response.status_code == 302
        assert response["Location"] == reverse("accounts:onboarding-other-tracker")

    @pytest.mark.parametrize("choice", ["", "manual", "csv", "not-a-real-app"])
    def test_unrecognized_choices_skip_straight_to_units_step(self, client, choice):
        client.force_login(UserFactory())
        response = client.post(
            reverse("accounts:onboarding-tracking-method"),
            {"tracking_app": choice},
        )
        assert response.status_code == 302
        assert response["Location"] == reverse("accounts:onboarding-units")

    def test_skip_button_goes_to_units_regardless_of_dropdown_value(self, client):
        """The grey skip button is a distinctly-named submit control -- per
        HTML form semantics, only the clicked button's name/value is sent, so
        its presence must win even if a live-sync app is also selected."""
        client.force_login(UserFactory())
        response = client.post(
            reverse("accounts:onboarding-tracking-method"),
            {"tracking_app": "liftosaur", "skip": "1"},
        )
        assert response.status_code == 302
        assert response["Location"] == reverse("accounts:onboarding-units")
