"""Tests for auth-endpoint rate limiting (TASK-153).

The project-wide autouse fixture in the root ``conftest.py`` disables
django-ratelimit so the ordinary auth suites can POST repeatedly. Every test
here re-enables it with ``override_settings(RATELIMIT_ENABLE=True)`` and clears
the shared ``ratelimit`` cache between tests so counters never leak across
cases. The ``_enable_ratelimit`` fixture also pins django-ratelimit's fixed-window
clock so a burst of requests can't straddle a window rollover mid-test (TASK-210).
"""

from unittest.mock import patch

import pytest
from django.contrib.auth.models import AnonymousUser
from django.core.cache import caches
from django.core.exceptions import PermissionDenied
from django.test import Client, RequestFactory, override_settings
from django.urls import reverse
from django_ratelimit.exceptions import Ratelimited

from accounts.middleware import RatelimitMiddleware
from accounts.ratelimit import client_ip, ratelimited_429
from accounts.tests.factories import UserFactory


@pytest.fixture
def _enable_ratelimit(settings):
    settings.RATELIMIT_ENABLE = True
    caches["ratelimit"].clear()
    # django-ratelimit is a fixed-window limiter: its per-request cache key embeds
    # a time window derived from ``int(time.time())`` (see ``core._get_window``).
    # A burst of requests that straddles a window rollover lands the later ones in
    # a fresh window, resetting the counter and letting a request through that
    # should have been throttled (a real-wall-clock 200-instead-of-429 flake in
    # CI, TASK-210). Pin the limiter's clock for the duration of each test so all
    # requests share one window; this touches only the test's reliability, not
    # production throttling behaviour.
    with patch("django_ratelimit.core.time") as mock_ratelimit_time:
        mock_ratelimit_time.time.return_value = 1_700_000_000
        yield
    caches["ratelimit"].clear()


@pytest.mark.django_db
@pytest.mark.usefixtures("_enable_ratelimit")
class TestLoginThrottling:
    @override_settings(RATELIMIT_LOGIN_USERNAME="3/m", RATELIMIT_LOGIN_IP="100/m")
    def test_repeated_failed_logins_same_username_are_blocked(self):
        client = Client()
        url = reverse("accounts:login")
        data = {"username": "victim", "password": "wrong"}
        for _ in range(3):
            assert client.post(url, data).status_code == 200
        assert client.post(url, data).status_code == 429

    @override_settings(RATELIMIT_LOGIN_IP="3/m", RATELIMIT_LOGIN_USERNAME="100/m")
    def test_many_usernames_from_one_ip_capped_by_ip_rate(self):
        client = Client()
        url = reverse("accounts:login")
        for i in range(3):
            resp = client.post(url, {"username": f"user{i}", "password": "wrong"})
            assert resp.status_code == 200
        resp = client.post(url, {"username": "user99", "password": "wrong"})
        assert resp.status_code == 429

    @override_settings(RATELIMIT_LOGIN_IP="100/m", RATELIMIT_LOGIN_USERNAME="100/m")
    def test_legitimate_login_under_threshold_succeeds(self):
        user = UserFactory(username="realuser")
        user.set_password("s3cret-pass")
        user.save()
        resp = Client().post(
            reverse("accounts:login"),
            {"username": "realuser", "password": "s3cret-pass"},
        )
        assert resp.status_code == 302

    @override_settings(RATELIMIT_LOGIN_IP="1/m", RATELIMIT_LOGIN_USERNAME="1/m")
    def test_get_requests_never_throttled(self):
        client = Client()
        url = reverse("accounts:login")
        for _ in range(5):
            assert client.get(url).status_code == 200


@pytest.mark.django_db
@pytest.mark.usefixtures("_enable_ratelimit")
class TestRegistrationThrottling:
    @override_settings(RATELIMIT_REGISTER_IP="2/h")
    @patch("accounts.views.trigger_lift_history_backfill")
    @patch("accounts.views.validate_liftosaur_key", return_value=True)
    def test_excess_registrations_from_one_ip_blocked(self, *_mocks):
        client = Client()
        url = reverse("accounts:register")

        def _data(i):
            return {
                "username": f"lifter{i}",
                "password": "s3cret-pass",
                "password_confirm": "s3cret-pass",
                "liftosaur_api_key": "valid-key",
                "accept_terms": "on",
            }

        assert client.post(url, _data(0)).status_code == 302
        assert client.post(url, _data(1)).status_code == 302
        assert client.post(url, _data(2)).status_code == 429

    @override_settings(RATELIMIT_REGISTER_IP="1/h")
    def test_get_still_renders_after_post_throttled(self):
        client = Client()
        url = reverse("accounts:register")
        # Malformed POST still counts against the IP limit (rate decorator runs
        # before the view body), so the next POST is blocked...
        client.post(url, {})
        assert client.post(url, {}).status_code == 429
        # ...but GET is never throttled.
        assert client.get(url).status_code == 200


@pytest.mark.django_db
@pytest.mark.usefixtures("_enable_ratelimit")
class TestValidateKeyThrottling:
    @override_settings(RATELIMIT_VALIDATE_KEY_USER="2/m")
    @patch("accounts.views.validate_liftosaur_key", return_value=True)
    def test_excess_validations_blocked_with_json_body(self, _mock):
        user = UserFactory()
        user.liftosaur_api_key = "saved-key"
        user.save()
        client = Client()
        client.force_login(user)
        url = reverse("accounts:validate_liftosaur_key")
        assert client.post(url).status_code == 200
        assert client.post(url).status_code == 200
        resp = client.post(url)
        assert resp.status_code == 429
        assert resp["Content-Type"] == "application/json"
        assert resp.json()["valid"] is False

    @override_settings(RATELIMIT_VALIDATE_KEY_USER="100/m")
    @patch("accounts.views.validate_liftosaur_key", return_value=True)
    def test_separate_users_have_independent_counters(self, _mock):
        user_a = UserFactory(liftosaur_api_key="key-a")
        user_b = UserFactory(liftosaur_api_key="key-b")
        url = reverse("accounts:validate_liftosaur_key")
        with override_settings(RATELIMIT_VALIDATE_KEY_USER="1/m"):
            client_a = Client()
            client_a.force_login(user_a)
            assert client_a.post(url).status_code == 200
            assert client_a.post(url).status_code == 429
            # A different user is unaffected by user_a's counter.
            client_b = Client()
            client_b.force_login(user_b)
            assert client_b.post(url).status_code == 200


class TestClientIp:
    def setup_method(self):
        self.rf = RequestFactory()

    def test_returns_last_forwarded_hop(self):
        # The proxy appends its observed client IP as the right-most hop, so the
        # last entry is the trusted one — earlier hops could be client-spoofed.
        request = self.rf.get(
            "/", HTTP_X_FORWARDED_FOR="203.0.113.7, 70.41.3.18, 10.0.0.1"
        )
        assert client_ip("g", request) == "10.0.0.1"

    def test_spoofed_leading_hop_is_ignored(self):
        # A client sending its own X-Forwarded-For only prepends to the left; the
        # proxy-appended tail still wins, so the bucket key cannot be rotated.
        spoofed = self.rf.get("/", HTTP_X_FORWARDED_FOR="1.2.3.4, 198.51.100.9")
        legitimate = self.rf.get("/", HTTP_X_FORWARDED_FOR="198.51.100.9")
        assert client_ip("g", spoofed) == client_ip("g", legitimate) == "198.51.100.9"

    def test_strips_whitespace_around_forwarded_hop(self):
        request = self.rf.get("/", HTTP_X_FORWARDED_FOR="  198.51.100.2  ")
        assert client_ip("g", request) == "198.51.100.2"

    def test_falls_back_to_remote_addr(self):
        request = self.rf.get("/", REMOTE_ADDR="192.0.2.55")
        assert client_ip("g", request) == "192.0.2.55"


class TestRatelimited429Handler:
    def setup_method(self):
        self.rf = RequestFactory()

    def test_html_variant_returns_429(self):
        request = self.rf.get("/accounts/login/")
        request.resolver_match = None
        request.user = AnonymousUser()
        response = ratelimited_429(request)
        assert response.status_code == 429
        assert "text/html" in response["Content-Type"]

    def test_json_variant_when_accept_header_present(self):
        request = self.rf.post("/anything/", HTTP_ACCEPT="application/json")
        request.resolver_match = None
        response = ratelimited_429(request)
        assert response.status_code == 429
        assert response["Content-Type"] == "application/json"


class TestRatelimitMiddleware:
    def setup_method(self):
        self.rf = RequestFactory()

    def test_ratelimited_exception_becomes_429(self):
        middleware = RatelimitMiddleware(lambda r: None)
        request = self.rf.post("/accounts/login/")
        request.resolver_match = None
        request.user = AnonymousUser()
        response = middleware.process_exception(request, Ratelimited())
        assert response.status_code == 429

    def test_plain_permission_denied_falls_through(self):
        middleware = RatelimitMiddleware(lambda r: None)
        request = self.rf.get("/")
        assert middleware.process_exception(request, PermissionDenied()) is None
