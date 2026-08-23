"""Tests for the onboarding "a different tracker" feedback step."""

import pytest
from django.test import Client
from django.urls import reverse

from accounts.models import TrackerRequest
from accounts.tests.factories import UserFactory


@pytest.fixture
def client():
    return Client()


@pytest.mark.django_db
class TestOnboardingOtherTrackerView:
    def test_anonymous_get_redirects_to_login(self, client):
        response = client.get(reverse("accounts:onboarding-other-tracker"))
        assert response.status_code == 302
        assert response["Location"].startswith(reverse("accounts:login"))

    def test_named_app_creates_a_tracker_request_and_redirects(self, client):
        user = UserFactory()
        client.force_login(user)

        response = client.post(
            reverse("accounts:onboarding-other-tracker"), {"app_name": "TrainerRoad"}
        )

        assert response.status_code == 302
        assert response["Location"] == reverse("accounts:onboarding-units")
        request = TrackerRequest.objects.get(user=user)
        assert request.app_name == "TrainerRoad"

    def test_blank_app_name_creates_nothing_but_still_redirects(self, client):
        user = UserFactory()
        client.force_login(user)

        response = client.post(
            reverse("accounts:onboarding-other-tracker"), {"app_name": ""}
        )

        assert response.status_code == 302
        assert response["Location"] == reverse("accounts:onboarding-units")
        assert not TrackerRequest.objects.filter(user=user).exists()
