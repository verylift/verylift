"""Tests for the static Terms of Service and Privacy Policy pages (TASK-157)."""

import pytest
from django.test import Client
from django.urls import reverse

from accounts.tests.factories import UserFactory


@pytest.fixture
def client():
    return Client()


@pytest.mark.django_db
class TestLegalPagesAnonymous:
    def test_terms_page_renders_for_anonymous(self, client):
        response = client.get(reverse("terms"))
        assert response.status_code == 200
        assert b"Terms of Service" in response.content

    def test_privacy_page_renders_for_anonymous(self, client):
        response = client.get(reverse("privacy"))
        assert response.status_code == 200
        assert b"Privacy Policy" in response.content


@pytest.mark.django_db
class TestLegalPagesNotOnboarded:
    """A signed-in but not-yet-onboarded user must not be bounced off the legal
    pages by OnboardingGateMiddleware."""

    def test_terms_page_not_redirected_for_incomplete_user(self, client):
        client.force_login(UserFactory())
        response = client.get(reverse("terms"))
        assert response.status_code == 200

    def test_privacy_page_not_redirected_for_incomplete_user(self, client):
        client.force_login(UserFactory())
        response = client.get(reverse("privacy"))
        assert response.status_code == 200
