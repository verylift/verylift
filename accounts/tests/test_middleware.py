"""Tests for UserLanguageMiddleware and UserTimezoneMiddleware.
OnboardingGateMiddleware was removed in TASK-248 — it gated on User.sex and a
BodyweightLog, neither of which exists anymore."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock
from urllib.parse import parse_qs, urlsplit

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone, translation

from accounts.middleware import UserLanguageMiddleware, UserTimezoneMiddleware
from accounts.tests.factories import UserFactory
from accounts.timezones import DETECT_COOKIE_NAME
from notifications.models import Notification
from notifications.tests.factories import NotificationFactory


def _make_request(path="/", user=None):
    """Build a mock request with a real or mock user."""
    req = MagicMock()
    req.path = path
    if user is None:
        # Unauthenticated
        req.user = MagicMock()
        req.user.is_authenticated = False
    else:
        req.user = user
    return req


@pytest.mark.django_db
class TestUserLanguageMiddleware:
    def test_activates_users_saved_language(self):
        user = UserFactory(language="es")
        get_response = MagicMock(return_value=MagicMock(status_code=200))
        middleware = UserLanguageMiddleware(get_response)
        req = _make_request(path="/dashboard/", user=user)
        try:
            middleware(req)
            assert translation.get_language() == "es"
            assert req.LANGUAGE_CODE == "es"
        finally:
            translation.deactivate()

    def test_automatic_language_leaves_locale_middleware_choice(self):
        # Empty User.language means "automatic" — LocaleMiddleware's own
        # request-side detection (already run earlier in the chain) is left
        # untouched rather than being overridden here.
        user = UserFactory(language="")
        get_response = MagicMock(return_value=MagicMock(status_code=200))
        middleware = UserLanguageMiddleware(get_response)
        req = SimpleNamespace(path="/dashboard/", user=user)
        translation.activate("en")
        try:
            middleware(req)
            assert translation.get_language() == "en"
            assert not hasattr(req, "LANGUAGE_CODE")
        finally:
            translation.deactivate()

    def test_unauthenticated_request_untouched(self):
        get_response = MagicMock(return_value=MagicMock(status_code=200))
        middleware = UserLanguageMiddleware(get_response)
        req = _make_request(path="/accounts/login/", user=None)
        translation.activate("en")
        try:
            middleware(req)
            assert translation.get_language() == "en"
        finally:
            translation.deactivate()


@pytest.mark.django_db
class TestLocaleMiddlewareIntegration:
    """End-to-end checks that LocaleMiddleware + UserLanguageMiddleware are
    wired up correctly in MIDDLEWARE (settings), not just unit-testable in
    isolation."""

    def test_authenticated_user_gets_spanish_rendered_page(self):
        user = UserFactory(language="es")
        client = Client()
        client.force_login(user)
        response = client.get(reverse("accounts:settings"))
        assert response.status_code == 200
        assert response["Content-Language"] == "es"
        assert "Configuración" in response.content.decode()

    def test_authenticated_user_automatic_defaults_to_english(self):
        user = UserFactory(language="")
        client = Client()
        client.force_login(user)
        response = client.get(reverse("accounts:settings"))
        assert response.status_code == 200
        assert "Settings" in response.content.decode()

    def test_anonymous_accept_language_header_renders_spanish_login(self):
        client = Client()
        response = client.get(reverse("accounts:login"), HTTP_ACCEPT_LANGUAGE="es")
        assert response.status_code == 200
        assert response["Content-Language"] == "es"
        assert "Iniciar sesión" in response.content.decode()

    def test_anonymous_request_without_header_defaults_to_english(self):
        client = Client()
        response = client.get(reverse("accounts:login"))
        assert response.status_code == 200
        assert "Sign in" in response.content.decode()


def _tz_request(path="/", user=None, cookies=None, method="GET"):
    """Minimal request double for UserTimezoneMiddleware's activation half.

    ``_detection_redirect`` is exercised through this too (it's the same
    ``__call__``), but a plain MagicMock's ``request.method`` never equals
    the literal string "GET", so it exits on the first guard without needing
    GET/headers/path set up -- these tests only care about the activated
    timezone, not the detour response.
    """
    req = MagicMock()
    req.path = path
    req.method = method
    req.COOKIES = cookies or {}
    if user is None:
        req.user = MagicMock(is_authenticated=False)
    else:
        req.user = user
    return req


@pytest.mark.django_db
class TestUserTimezoneMiddleware:
    """Activation half of UserTimezoneMiddleware (TASK-273)."""

    def test_pinned_timezone_wins_over_conflicting_cookie(self):
        user = UserFactory(timezone="America/Toronto")
        get_response = MagicMock(return_value=MagicMock(status_code=200))
        middleware = UserTimezoneMiddleware(get_response)
        req = _tz_request(user=user, cookies={"pp_timezone": "Asia/Tokyo"})
        try:
            middleware(req)
            assert timezone.get_current_timezone_name() == "America/Toronto"
        finally:
            timezone.deactivate()

    def test_cookie_used_when_no_pinned_timezone(self):
        user = UserFactory(timezone="")
        get_response = MagicMock(return_value=MagicMock(status_code=200))
        middleware = UserTimezoneMiddleware(get_response)
        req = _tz_request(user=user, cookies={"pp_timezone": "Asia/Tokyo"})
        try:
            middleware(req)
            assert timezone.get_current_timezone_name() == "Asia/Tokyo"
        finally:
            timezone.deactivate()

    def test_anonymous_request_with_valid_cookie_uses_it(self):
        get_response = MagicMock(return_value=MagicMock(status_code=200))
        middleware = UserTimezoneMiddleware(get_response)
        req = _tz_request(user=None, cookies={"pp_timezone": "Asia/Tokyo"})
        try:
            middleware(req)
            assert timezone.get_current_timezone_name() == "Asia/Tokyo"
        finally:
            timezone.deactivate()

    def test_invalid_cookie_falls_back_to_utc_without_error(self):
        user = UserFactory(timezone="")
        get_response = MagicMock(return_value=MagicMock(status_code=200))
        middleware = UserTimezoneMiddleware(get_response)
        req = _tz_request(user=user, cookies={"pp_timezone": "Not/AZone"})
        try:
            middleware(req)
            assert timezone.get_current_timezone_name() == "UTC"
        finally:
            timezone.deactivate()

    def test_activated_timezone_does_not_leak_into_next_request(self):
        # C3: timezone.activate() is thread-local and Django never resets it
        # between requests, so a request resolving to "no opinion" must
        # explicitly deactivate() or it inherits the previous request's zone
        # on a reused worker thread.
        user_with_zone = UserFactory(timezone="Pacific/Auckland")
        get_response = MagicMock(return_value=MagicMock(status_code=200))
        middleware = UserTimezoneMiddleware(get_response)
        try:
            middleware(_tz_request(user=user_with_zone))
            assert timezone.get_current_timezone_name() == "Pacific/Auckland"

            bare_user = UserFactory(timezone="")
            middleware(_tz_request(user=bare_user))
            assert timezone.get_current_timezone_name() == "UTC"
        finally:
            timezone.deactivate()


@pytest.mark.django_db
class TestTimezoneDetectionRedirect:
    """Guard matrix for UserTimezoneMiddleware._detection_redirect (TASK-273 R1)."""

    def _cookieless_client(self):
        client = Client()
        del client.cookies[DETECT_COOKIE_NAME]
        return client

    def test_cookieless_html_get_redirects_to_detect(self):
        client = self._cookieless_client()
        response = client.get(reverse("accounts:login"))
        assert response.status_code == 302
        parsed = urlsplit(response["Location"])
        assert parsed.path == reverse("accounts:timezone-detect")
        assert parse_qs(parsed.query)["next"] == [reverse("accounts:login")]

    def test_redirect_preserves_original_query_string(self):
        client = self._cookieless_client()
        response = client.get(reverse("accounts:login") + "?next=/settings/")
        parsed = urlsplit(response["Location"])
        assert parse_qs(parsed.query)["next"] == [
            reverse("accounts:login") + "?next=/settings/"
        ]

    def test_guard_a_tzdetect_param_skips_redirect(self):
        client = self._cookieless_client()
        response = client.get(reverse("accounts:login") + "?tzdetect=1")
        assert response.status_code == 200

    def test_guard_b_tzdetect_cookie_skips_redirect(self):
        # The autouse _skip_timezone_detection fixture already seeds this
        # cookie on a plain Client() -- this test is exactly that default.
        client = Client()
        response = client.get(reverse("accounts:login"))
        assert response.status_code == 200

    def test_end_to_end_no_loop(self):
        client = self._cookieless_client()
        response = client.get(reverse("challenges:dashboard"), follow=True)
        assert response.status_code == 200
        detect_path = reverse("accounts:timezone-detect")
        detect_hops = [
            hop for hop in response.redirect_chain if hop[0].startswith(detect_path)
        ]
        assert len(response.redirect_chain) == 1
        assert len(detect_hops) == 1

    def test_post_is_never_redirected(self):
        client = self._cookieless_client()
        response = client.post(
            reverse("accounts:login"), {"username": "nope", "password": "nope"}
        )
        assert response.status_code == 200

    def test_htmx_get_is_never_redirected(self):
        client = self._cookieless_client()
        response = client.get(reverse("accounts:login"), HTTP_HX_REQUEST="true")
        assert response.status_code == 200

    def test_non_html_accept_is_never_redirected(self):
        client = self._cookieless_client()
        response = client.get(reverse("accounts:login"), HTTP_ACCEPT="application/json")
        assert response.status_code == 200

    def test_media_path_is_never_redirected(self):
        # 403 rather than 404 since TASK-277: media is served behind an
        # authenticated view, so this anonymous request never reaches the
        # filesystem. What matters here is unchanged -- the MEDIA_URL exemption
        # in accounts/middleware.py means the request is answered by the media
        # view itself instead of being detoured to timezone detection, which
        # matters most for exactly this anonymous case. Keep it logged out.
        client = self._cookieless_client()
        response = client.get("/media/does-not-exist.png")
        assert response.status_code == 403

    def test_oidc_path_is_never_redirected(self):
        client = self._cookieless_client()
        response = client.get("/oidc/authenticate/")
        assert response.status_code != 302 or "/tz/detect/" not in response["Location"]

    def test_detect_endpoint_itself_is_never_redirected(self):
        client = self._cookieless_client()
        response = client.get(reverse("accounts:timezone-detect"))
        assert response.status_code == 200

    def test_resolved_cookie_timezone_skips_redirect(self):
        client = self._cookieless_client()
        client.cookies["pp_timezone"] = "Asia/Tokyo"
        response = client.get(reverse("accounts:login"))
        assert response.status_code == 200

    def test_resolved_pinned_timezone_skips_redirect(self):
        user = UserFactory(timezone="Asia/Tokyo")
        client = self._cookieless_client()
        client.force_login(user)
        response = client.get(reverse("accounts:settings"))
        assert response.status_code == 200


@pytest.mark.django_db
class TestUserTimezoneMiddlewareIntegration:
    """End-to-end check that UserTimezoneMiddleware is wired up in MIDDLEWARE
    (settings), not just unit-testable in isolation -- mirrors
    TestLocaleMiddlewareIntegration above."""

    def _dashboard_with_notification(self, client, user):
        NotificationFactory(user=user)
        notification = Notification.objects.filter(user=user).first()
        # created_at is auto_now_add=True, so the boundary-straddling
        # timestamp has to be forced in after creation, not via the
        # constructor.
        boundary = datetime(2026, 1, 15, 23, 30, tzinfo=UTC)
        Notification.objects.filter(pk=notification.pk).update(created_at=boundary)
        return client.get(reverse("challenges:dashboard"))

    def test_browser_cookie_timezone_shifts_rendered_notification_time(self):
        user = UserFactory(timezone="")
        client = Client()
        client.force_login(user)
        client.cookies["pp_timezone"] = "Pacific/Auckland"
        response = self._dashboard_with_notification(client, user)
        assert response.status_code == 200
        content = response.content.decode()
        assert "Jan 16, 2026, 12:30 p.m." in content
        assert "Jan 15, 2026, 11:30 p.m." not in content

    def test_url_encoded_browser_cookie_shifts_rendered_notification_time(self):
        """Regression: a real browser's encodeURIComponent(tz) means the
        Cookie header actually carries "America%2FEdmonton", not
        "America/Edmonton" -- request.COOKIES does not URL-decode cookie
        values the way request.GET/POST decode query/form values. Setting
        client.cookies["pp_timezone"] to an already-decoded string (as the
        other tests in this class do) never exercises that wire format."""
        user = UserFactory(timezone="")
        client = Client()
        client.force_login(user)
        client.cookies["pp_timezone"] = "Pacific%2FAuckland"
        response = self._dashboard_with_notification(client, user)
        assert response.status_code == 200
        content = response.content.decode()
        assert "Jan 16, 2026, 12:30 p.m." in content
        assert "Jan 15, 2026, 11:30 p.m." not in content

    def test_pinned_timezone_overrides_conflicting_cookie(self):
        user = UserFactory(timezone="Pacific/Auckland")
        client = Client()
        client.force_login(user)
        client.cookies["pp_timezone"] = "America/Los_Angeles"
        response = self._dashboard_with_notification(client, user)
        assert response.status_code == 200
        content = response.content.decode()
        assert "Jan 16, 2026, 12:30 p.m." in content
        assert "Jan 15, 2026, 11:30 p.m." not in content

    def test_no_timezone_resolved_renders_utc(self):
        user = UserFactory(timezone="")
        client = Client()
        client.force_login(user)
        response = self._dashboard_with_notification(client, user)
        assert response.status_code == 200
        content = response.content.decode()
        assert "Jan 15, 2026, 11:30 p.m." in content
