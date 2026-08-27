"""Tests for the "supported apps" page (TASK-254).

Hardcoded content (no model), so there's little left to test beyond "the
page renders" and "the reused, behavior-bearing pieces still work" -- the
copy itself isn't asserted on, per the project's copy-echo ban.
"""

import pytest
from django.test import Client
from django.urls import reverse


@pytest.fixture
def client():
    return Client()


class TestSupportedAppsView:
    def test_page_renders(self, client, db):
        response = client.get(reverse("core:supported-apps"))
        assert response.status_code == 200

    def test_liftosaur_coupon_chip_renders_exactly_once(self, client, db):
        """Liftosaur is the only tracker with a coupon code, so the
        copy-to-clipboard chip (shared with onboarding/settings) should
        appear exactly once on the page."""
        response = client.get(reverse("core:supported-apps"))
        content = response.content.decode()

        assert "VERYLIFT" in content
        assert content.count("data-copy-code>") == 1

    def test_each_tracker_links_to_its_own_site(self, client, db):
        response = client.get(reverse("core:supported-apps"))
        content = response.content.decode()

        for url in (
            "https://www.liftosaur.com",
            "https://www.hevyapp.com",
            "https://wger.de",
            "https://www.strongapp.io",
        ):
            assert url in content
