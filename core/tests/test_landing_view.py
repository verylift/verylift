"""Tests for the public landing page view (TASK-254)."""

import pytest
from django.test import Client
from django.urls import reverse

from accounts.tests.factories import UserFactory


@pytest.fixture
def user(db):
    return UserFactory()


@pytest.fixture
def client(user):
    c = Client()
    c.force_login(user)
    return c


class TestLandingPage:
    def test_anonymous_gets_landing_page(self, db):
        response = Client().get(reverse("core:landing"))
        assert response.status_code == 200
        assert "landing.html" in [t.name for t in response.templates]

    def test_landing_page_links(self, db):
        content = Client().get(reverse("core:landing")).content.decode()
        assert reverse("accounts:register") in content
        assert reverse("accounts:login") in content
        assert reverse("terms") in content
        assert reverse("privacy") in content
        assert reverse("core:newsletter-subscribe") in content
        assert reverse("set_language") in content
        assert reverse("core:supported-apps") in content

    def test_authenticated_user_redirected_to_dashboard(self, client):
        response = client.get(reverse("core:landing"))
        assert response.status_code == 302
        assert response["Location"] == reverse("challenges:dashboard")
