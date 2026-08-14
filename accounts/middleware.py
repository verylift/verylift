"""Middleware for rate-limit responses, language, and timezone."""

from django.conf import settings
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone, translation
from django_ratelimit.exceptions import Ratelimited

from accounts.ratelimit import ratelimited_429
from accounts.timezones import (
    DETECT_COOKIE_NAME,
    DETECT_PARAM,
    cookie_timezone,
    resolve_timezone,
)
from core.http import build_url, is_htmx


class RatelimitMiddleware:
    """Turn django-ratelimit's ``Ratelimited`` into a real 429 response.

    ``Ratelimited`` subclasses ``PermissionDenied``, which Django would
    otherwise render as a 403. Catching it here lets throttled auth requests
    return ``429 Too Many Requests`` without also intercepting genuine
    permission denials.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_exception(self, request, exception):
        if isinstance(exception, Ratelimited):
            return ratelimited_429(request)
        return None


class UserLanguageMiddleware:
    """Activate an authenticated user's saved language preference.

    Runs after AuthenticationMiddleware (request.user is available) and after
    LocaleMiddleware's request-side detection, so an explicit User.language
    choice overrides the cookie/Accept-Language guess LocaleMiddleware already
    made. LocaleMiddleware's process_response reads translation.get_language()
    on the way out, so activating here is still honored in Content-Language.
    An empty User.language ("automatic") leaves LocaleMiddleware's choice in
    place.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.user.is_authenticated and request.user.language:
            translation.activate(request.user.language)
            request.LANGUAGE_CODE = translation.get_language()
        return self.get_response(request)


class UserTimezoneMiddleware:
    """Activate the viewer's timezone, and detour a first hit through detection.

    Runs after AuthenticationMiddleware (request.user is available) and after
    UserLanguageMiddleware. Two responsibilities live in one class because
    they share the same priority ladder (accounts.timezones.resolve_timezone):

    Activation: when a timezone resolves (a pinned User.timezone, or the
    browser-detected pp_timezone cookie), it is activated so every
    USE_TZ-aware |date/|time template filter renders in it. When nothing
    resolves, ``timezone.deactivate()`` is called explicitly -- this is not
    optional. ``timezone.activate()`` sets thread-local state that Django
    never clears between requests (unlike LocaleMiddleware, which deactivates
    translations on the response), so a request that resolves to "no opinion"
    must reset to settings.TIME_ZONE or it would inherit whatever the
    previous request on a reused worker thread activated.
    ``deactivate()`` restores settings.TIME_ZONE ("UTC"), which is exactly the
    fallback AC#3 asks for.

    Detection detour: when nothing resolves, most qualifying requests are
    redirected once to accounts:timezone-detect, which sets the pp_timezone
    cookie from the browser's Intl.DateTimeFormat and bounces back -- so the
    originally requested page is never rendered in UTC just because detection
    hasn't happened yet (TASK-273 R1). See ``_detection_redirect`` for the
    exemptions and the two loop-breaking guards. Note the ordering below:
    ``deactivate()`` runs before the redirect check, so even a request that
    gets detoured leaves clean thread-local state behind.

    Detected-timezone persistence (TASK-300): separately from activation,
    an authenticated user's ``detected_timezone`` is opportunistically kept
    in sync with the ``pp_timezone`` cookie. This exists for code with no
    live request to read that cookie from (the close_challenges cron) --
    it's a fallback for an "automatic" account (blank pinned ``timezone``),
    not a substitute for it, so it never affects activation above.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user_timezone = resolve_timezone(request)
        if user_timezone:
            timezone.activate(user_timezone)
        else:
            timezone.deactivate()
            detour = self._detection_redirect(request)
            if detour is not None:
                return detour
        self._persist_detected_timezone(request)
        return self.get_response(request)

    def _persist_detected_timezone(self, request):
        """Save the browser-detected zone onto ``request.user`` if it changed.

        A no-op write on every request except the rare one where the
        detected zone is new (first-ever detection, a new device, actual
        travel) -- comparing before saving keeps this cheap on the hot path.
        """
        user = request.user
        if not user.is_authenticated:
            return
        detected = cookie_timezone(request)
        if detected and detected != user.detected_timezone:
            user.detected_timezone = detected
            user.save(update_fields=["detected_timezone"])

    def _detection_redirect(self, request):
        """A 302 to the detection endpoint, or ``None`` when exempt.

        Every condition below must hold before a request is detoured:
        """
        if request.method != "GET":
            # A 302 turns a POST into a GET and drops the body. A first-hit
            # POST is served normally and accepts one UTC render (AC#3).
            return None
        if is_htmx(request):
            # htmx follows redirects transparently and would swap the
            # detection page's markup into the target element.
            return None
        accept = request.headers.get("Accept", "")
        if accept and "text/html" not in accept:
            # An explicit Accept header that excludes HTML means a non-HTML
            # consumer (fetch/XHR callers) -- never detour those. A missing
            # Accept header (real browsers always send one; this is mostly a
            # bare test client) is treated as HTML-capable.
            return None
        if request.GET.get(DETECT_PARAM) == "1":
            # Guard A: this request is itself the return trip from detection.
            return None
        if DETECT_COOKIE_NAME in request.COOKIES:
            # Guard B: detection was already offered to this browser today.
            return None
        detect_path = reverse("accounts:timezone-detect")
        exempt_prefixes = (settings.STATIC_URL, settings.MEDIA_URL, "/oidc/")
        if request.path == detect_path or request.path.startswith(exempt_prefixes):
            return None
        return redirect(build_url(detect_path, {"next": request.get_full_path()}))
