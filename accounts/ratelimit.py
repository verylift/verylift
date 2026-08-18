"""Rate-limiting helpers for the auth endpoints (TASK-153).

Throttling is layered on with django-ratelimit. The pieces here are kept in one
module so the policy (which key, which rate, how a block is surfaced) is easy to
read and tune:

- ``client_ip`` derives the real client IP behind a reverse proxy by trusting
  the *last* hop of ``X-Forwarded-For`` — the entry the proxy appends from its
  own observed connection, which a client cannot spoof — falling back to
  ``REMOTE_ADDR`` when the header is absent (e.g. direct requests in
  development and tests).
- The ``*_rate`` callables resolve the configured rate string at request time so
  the thresholds stay env-tunable (see settings) and overridable in tests.
- ``ratelimited_429`` renders a real ``429 Too Many Requests`` for the
  ``Ratelimited`` exception raised by the blocking decorators, wired in via
  middleware rather than the default ``PermissionDenied`` 403.
"""

import logging

from django.conf import settings
from django.http import JsonResponse
from django.shortcuts import render
from django.utils.translation import gettext

logger = logging.getLogger(__name__)

_JSON_VIEW_NAMES = frozenset(
    {"accounts:validate_liftosaur_key", "accounts:validate_hevy_key"}
)


def client_ip(group, request):
    """Return the requesting client's IP, honouring X-Forwarded-For.

    Behind a reverse proxy ``REMOTE_ADDR`` is the proxy itself, so per-IP limits
    would be shared across every user. A proxy following the usual convention
    *appends* the client IP it observes as the right-most ``X-Forwarded-For``
    entry, leaving any client-supplied hops to its left. Trusting the last hop
    therefore uses the nearest proxy's own observation and ignores anything an
    attacker could pre-set to rotate rate-limit buckets. Falls back to
    ``REMOTE_ADDR`` when the header is absent.

    This assumes a single proxy in front. Chain two (say a tunnel plus a local
    reverse proxy) and the last hop is the inner proxy's address rather than the
    client's, putting every user behind it in one bucket again.
    """
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded:
        return forwarded.split(",")[-1].strip()
    return request.META.get("REMOTE_ADDR", "")


def login_ip_rate(group, request):
    return settings.RATELIMIT_LOGIN_IP


def login_username_rate(group, request):
    return settings.RATELIMIT_LOGIN_USERNAME


def register_ip_rate(group, request):
    return settings.RATELIMIT_REGISTER_IP


def validate_key_user_rate(group, request):
    return settings.RATELIMIT_VALIDATE_KEY_USER


def password_reset_ip_rate(group, request):
    return settings.RATELIMIT_PASSWORD_RESET_IP


def password_reset_email_rate(group, request):
    return settings.RATELIMIT_PASSWORD_RESET_EMAIL


def _wants_json(request):
    """True when the throttled request expects a JSON body, not an HTML page.

    The Liftosaur and Hevy key-validation endpoints are consumed by
    ``fetch()`` and always parse the response as JSON, so a rendered 429 page
    would surface as an opaque parse error. Match it by resolved view name;
    also honour an explicit ``Accept: application/json``.
    """
    view_name = getattr(request.resolver_match, "view_name", None)
    if view_name in _JSON_VIEW_NAMES:
        return True
    return "application/json" in request.headers.get("Accept", "")


def ratelimited_429(request):
    """Build the ``429 Too Many Requests`` response for a blocked request.

    No secrets (usernames, keys) are logged — only the path and derived client
    IP — per the project logging rules.
    """
    logger.warning(
        "Rate limit hit on %s from %s", request.path, client_ip(None, request)
    )
    if _wants_json(request):
        return JsonResponse(
            {
                "valid": False,
                "message": gettext("Too many attempts. Try again in a minute."),
            },
            status=429,
        )
    return render(request, "429.html", status=429)
