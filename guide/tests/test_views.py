"""Tests for the in-app user guide views (TASK-110)."""

import pytest
from django.test import Client
from django.urls import reverse

from accounts.tests.factories import UserFactory
from guide.views import DOC_PAGES, _rewrite_doc_links


@pytest.fixture
def user(db):
    return UserFactory()


@pytest.fixture
def authed_client(user):
    c = Client()
    c.force_login(user)
    return c


@pytest.mark.django_db
class TestGuideViews:
    def test_index_login_required(self):
        response = Client().get(reverse("guide:index"))
        assert response.status_code == 302
        assert "/accounts/login/" in response["Location"]

    def test_index_renders_markdown(self, authed_client):
        response = authed_client.get(reverse("guide:index"))
        assert response.status_code == 200
        assert b"<h2" in response.content or b"<h1" in response.content

    @pytest.mark.parametrize("slug", [s for s in DOC_PAGES if s != "index"])
    def test_page_renders_markdown(self, authed_client, slug):
        response = authed_client.get(reverse("guide:page", kwargs={"slug": slug}))
        assert response.status_code == 200
        assert b"<h2" in response.content

    def test_unknown_slug_returns_404(self, authed_client):
        response = authed_client.get(
            reverse("guide:page", kwargs={"slug": "not-a-real-page"})
        )
        assert response.status_code == 404

    def test_page_login_required(self):
        response = Client().get(reverse("guide:page", kwargs={"slug": "scoring"}))
        assert response.status_code == 302
        assert "/accounts/login/" in response["Location"]

    def test_sidebar_link_present(self, authed_client):
        response = authed_client.get(reverse("challenges:dashboard"))
        assert response.status_code == 200
        content = response.content.decode()
        assert reverse("guide:index") in content

    def test_cross_doc_links_rewritten_to_guide_urls(self, authed_client):
        response = authed_client.get(reverse("guide:page", kwargs={"slug": "scoring"}))
        content = response.content.decode()
        assert 'href="challenges.md"' not in content
        assert reverse("guide:page", kwargs={"slug": "challenges"}) in content

    def test_rewrite_doc_links_leaves_unknown_md_links_untouched(self):
        html = '<a href="unknown-page.md">Somewhere else</a>'
        assert _rewrite_doc_links(html) == html
