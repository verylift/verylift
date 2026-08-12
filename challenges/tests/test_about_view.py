"""Tests for the public About Us page (TASK-254)."""

from django.test import Client
from django.urls import reverse


class TestAboutPage:
    def test_get_renders_about_template(self, db):
        response = Client().get(reverse("challenges:about"))
        assert response.status_code == 200
        assert "about.html" in [t.name for t in response.templates]

    def test_content_and_links(self, db):
        content = Client().get(reverse("challenges:about")).content.decode()
        assert "About Us" in content
        assert "Tomi" in content
        assert "Jules" in content
        assert reverse("challenges:landing") in content

    def test_reachable_from_landing_footer(self, db):
        landing_content = Client().get(reverse("challenges:landing")).content.decode()
        assert reverse("challenges:about") in landing_content
