"""Tests for the flat-file robots.txt and llms.txt routes."""

from django.test import Client
from django.urls import reverse


class TestRobotsTxt:
    def test_denies_by_default_and_allows_public_pages(self, db):
        response = Client().get(reverse("robots-txt"))

        assert response.status_code == 200
        assert response["Content-Type"] == "text/plain"
        content = response.content.decode()
        assert "Disallow: /" in content
        assert "Allow: /terms/" in content

    def test_never_allows_the_bearer_invite_link_path(self):
        content = Client().get(reverse("robots-txt")).content.decode()
        assert "Allow: /join/" not in content


class TestLlmsTxt:
    def test_lists_the_public_docs_and_site_links(self, db):
        response = Client().get(reverse("llms-txt"))

        assert response.status_code == 200
        assert response["Content-Type"] == "text/plain"
        content = response.content.decode()
        assert "raw.githubusercontent.com/verylift/verylift" in content
        assert reverse("terms") in content
        assert reverse("privacy") in content
