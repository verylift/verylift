"""Tests for the landing-page newsletter signup form (TASK-254)."""

from django.test import Client
from django.urls import reverse

from core.models import NewsletterSubscriber


class TestNewsletterSubscribe:
    def test_valid_email_creates_subscriber_and_redirects(self, db):
        response = Client().post(
            reverse("challenges:newsletter-subscribe"), {"email": "new@example.com"}
        )
        assert response.status_code == 302
        assert NewsletterSubscriber.objects.filter(email="new@example.com").exists()

    def test_invalid_email_shows_validation_error_and_creates_nothing(self, db):
        response = Client().post(
            reverse("challenges:newsletter-subscribe"),
            {"email": "not-an-email"},
            follow=True,
        )
        messages = [str(m) for m in response.context["messages"]]
        assert any("valid" in m.lower() for m in messages)
        assert not NewsletterSubscriber.objects.exists()

    def test_duplicate_email_is_idempotent(self, db):
        client = Client()
        url = reverse("challenges:newsletter-subscribe")
        client.post(url, {"email": "dup@example.com"})
        response = client.post(url, {"email": "dup@example.com"})
        assert response.status_code == 302
        assert NewsletterSubscriber.objects.filter(email="dup@example.com").count() == 1
