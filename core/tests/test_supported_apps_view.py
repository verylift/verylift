"""Tests for the "supported apps" page (TASK-254)."""

import pytest
from django.test import Client
from django.urls import reverse

from core.models import SupportedApp, SupportedAppMode
from core.tests.factories import SupportedAppFactory, SupportedAppModeFactory


@pytest.fixture
def client():
    return Client()


class TestSupportedAppQuerySet:
    def test_featured_returns_only_affiliate_apps(self, db):
        affiliate = SupportedAppFactory(is_affiliate=True)
        non_affiliate = SupportedAppFactory(is_affiliate=False)
        featured = SupportedApp.objects.featured()
        assert affiliate in featured
        assert non_affiliate not in featured

    def test_other_returns_only_non_affiliate_apps(self, db):
        affiliate = SupportedAppFactory(is_affiliate=True)
        non_affiliate = SupportedAppFactory(is_affiliate=False)
        other = SupportedApp.objects.other()
        assert non_affiliate in other
        assert affiliate not in other


class TestSupportedAppsView:
    def test_page_renders(self, client, db):
        response = client.get(reverse("core:supported-apps"))
        assert response.status_code == 200

    def test_featured_and_other_apps_split_in_context(self, client, db):
        featured = SupportedAppFactory(is_affiliate=True, name="Featured Co")
        other = SupportedAppFactory(is_affiliate=False, name="Other Co")

        response = client.get(reverse("core:supported-apps"))

        assert featured in response.context["featured_apps"]
        assert featured not in response.context["other_apps"]
        assert other in response.context["other_apps"]
        assert other not in response.context["featured_apps"]

    def test_mode_tags_render_from_real_data_not_hardcoded(self, client, db):
        app = SupportedAppFactory(is_affiliate=False, name="Two Mode Co")
        SupportedAppModeFactory(supported_app=app, mode=SupportedAppMode.Mode.LIVE_SYNC)
        SupportedAppModeFactory(
            supported_app=app, mode=SupportedAppMode.Mode.CSV_UPLOAD
        )

        response = client.get(reverse("core:supported-apps"))
        content = response.content.decode()

        assert "live sync" in content
        assert "csv upload" in content

    def test_app_with_no_modes_renders_without_error(self, client, db):
        SupportedAppFactory(is_affiliate=False, name="No Mode Co")
        response = client.get(reverse("core:supported-apps"))
        assert response.status_code == 200

    def test_affiliate_disclosure_shown_only_for_affiliate_apps(self, client, db):
        affiliate = SupportedAppFactory(is_affiliate=True, name="Affiliate Co")
        non_affiliate = SupportedAppFactory(is_affiliate=False, name="Plain Co")

        response = client.get(reverse("core:supported-apps"))
        content = response.content.decode()

        assert f"affiliate relationship with {affiliate.name}" in content
        assert f"affiliate relationship with {non_affiliate.name}" not in content

    def test_app_links_use_their_own_url(self, client, db):
        app = SupportedAppFactory(is_affiliate=False, url="https://example.com/tracker")
        response = client.get(reverse("core:supported-apps"))
        assert app.url in response.content.decode()
