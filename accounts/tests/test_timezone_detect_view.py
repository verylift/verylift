"""Tests for accounts.views.timezone_detect_view (TASK-273 R1)."""

import logging

import pytest
from django.test import Client
from django.urls import reverse

from accounts.timezones import DETECT_COOKIE_MAX_AGE, DETECT_COOKIE_NAME


@pytest.fixture
def client():
    return Client()


@pytest.mark.django_db
class TestTimezoneDetectView:
    def test_renders_200(self, client):
        response = client.get(reverse("accounts:timezone-detect"))
        assert response.status_code == 200

    def test_sets_detect_cookie_with_expected_max_age(self, client):
        response = client.get(reverse("accounts:timezone-detect"))
        cookie = response.cookies[DETECT_COOKIE_NAME]
        assert cookie.value == "1"
        assert cookie["max-age"] == DETECT_COOKIE_MAX_AGE

    def test_never_cache_headers_present(self, client):
        response = client.get(reverse("accounts:timezone-detect"))
        assert "no-store" in response["Cache-Control"]

    def test_next_url_carries_tzdetect_param(self, client):
        response = client.get(
            reverse("accounts:timezone-detect"), {"next": "/dashboard/"}
        )
        assert "/dashboard/?tzdetect=1" in response.content.decode()

    def test_next_url_with_existing_query_string_appends_param(self, client):
        response = client.get(
            reverse("accounts:timezone-detect"), {"next": "/dashboard/?a=1"}
        )
        content = response.content.decode()
        assert "/dashboard/?" in content
        assert "a=1" in content
        assert "tzdetect=1" in content

    def test_missing_next_falls_back_to_root(self, client):
        response = client.get(reverse("accounts:timezone-detect"))
        assert "/?tzdetect=1" in response.content.decode()

    def test_absolute_url_next_is_rejected(self, client, caplog):
        with caplog.at_level(logging.WARNING):
            response = client.get(
                reverse("accounts:timezone-detect"),
                {"next": "https://evil.example/"},
            )
        assert response.status_code == 200
        assert "evil.example" not in response.content.decode()
        assert "/?tzdetect=1" in response.content.decode()
        assert "Rejected unsafe next=" in caplog.text

    def test_scheme_relative_next_is_rejected(self, client, caplog):
        with caplog.at_level(logging.WARNING):
            response = client.get(
                reverse("accounts:timezone-detect"), {"next": "//evil.example/"}
            )
        assert response.status_code == 200
        assert "evil.example" not in response.content.decode()
        assert "/?tzdetect=1" in response.content.decode()
        assert "Rejected unsafe next=" in caplog.text

    def test_post_is_not_allowed(self, client):
        response = client.post(reverse("accounts:timezone-detect"))
        assert response.status_code == 405
