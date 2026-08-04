"""Timezone resolution helpers for TASK-273.

Owns the priority ladder that decides which IANA timezone (if any) applies to
a request -- a pinned ``User.timezone`` first, then the browser-detected
``pp_timezone`` cookie, then "no opinion" (the caller falls back to
``settings.TIME_ZONE``) -- plus the small URL/validation helpers the
detection round-trip (accounts.middleware.UserTimezoneMiddleware,
accounts.views.timezone_detect_view) needs to stay a single one-shot redirect.
"""

import logging
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit
from zoneinfo import available_timezones

logger = logging.getLogger(__name__)

TIMEZONE_COOKIE_NAME = "pp_timezone"  # browser-detected IANA zone
TIMEZONE_COOKIE_MAX_AGE = 60 * 60 * 24 * 365  # mirrors LANGUAGE_COOKIE_AGE
DETECT_COOKIE_NAME = "pp_tzdetect"  # "we already ran detection" marker
DETECT_COOKIE_MAX_AGE = 60 * 60 * 24  # one day -- see UserTimezoneMiddleware
DETECT_PARAM = "tzdetect"  # loop breaker -- see UserTimezoneMiddleware

# Computed once at import: zoneinfo.available_timezones() walks the tz
# database directory and is far too slow to call per-request.
_AVAILABLE_TIMEZONES = frozenset(available_timezones())


def is_valid_timezone(name: str) -> bool:
    """True when ``name`` is a known IANA zone.

    This is the security boundary for the client-controlled ``pp_timezone``
    cookie: an unknown or hostile value can only ever fall through to UTC.
    """
    return name in _AVAILABLE_TIMEZONES


def _grouped_timezones() -> list[tuple[str, list[str]]]:
    groups: dict[str, list[str]] = {}
    for name in sorted(_AVAILABLE_TIMEZONES):
        region = name.split("/")[0] if "/" in name else "Other"
        groups.setdefault(region, []).append(name)
    other = groups.pop("Other", None)
    ordered = [(region, groups[region]) for region in sorted(groups)]
    if other:
        ordered.append(("Other", other))
    return ordered


# Rendered on every settings-page GET; the underlying zone list only changes
# when the tzdata package is upgraded, so compute it once at import.
GROUPED_TIMEZONES = _grouped_timezones()


def grouped_timezones() -> list[tuple[str, list[str]]]:
    return GROUPED_TIMEZONES


def resolve_timezone(request) -> str | None:
    """Return the timezone that should be activated for ``request``.

    Priority: an authenticated user's pinned ``timezone`` field, then the
    browser-detected ``pp_timezone`` cookie, then ``None`` ("no opinion" --
    the caller is expected to fall back to ``settings.TIME_ZONE``).
    """
    user = request.user
    if user.is_authenticated and user.timezone:
        if is_valid_timezone(user.timezone):
            return user.timezone
        logger.warning(
            "User %s has an invalid pinned timezone %r; falling back",
            user.id,
            user.timezone,
        )

    raw_cookie_value = request.COOKIES.get(TIMEZONE_COOKIE_NAME)
    if raw_cookie_value:
        # request.COOKIES holds the raw cookie-header value verbatim -- Django
        # does not URL-decode it the way it does request.GET/POST. The
        # detection script writes it via encodeURIComponent (belt-and-suspenders
        # against a future browser reporting a zone name with characters that
        # do need escaping), so "/" comes back as "%2F" and every real zone
        # name with a "/" (i.e. all but "UTC" itself) would otherwise fail
        # validation and silently fall back to UTC.
        cookie_value = unquote(raw_cookie_value)
        if is_valid_timezone(cookie_value):
            return cookie_value
        logger.debug(
            "Ignoring invalid %s cookie: %r", TIMEZONE_COOKIE_NAME, raw_cookie_value
        )

    return None


def with_detect_param(url: str) -> str:
    """Append ``tzdetect=1`` to ``url``, which may already carry a query string.

    Not built with ``core.http.build_url`` -- that helper does a bare
    ``f"{base_url}?{qs}"`` and would double up the ``?`` when ``url`` already
    has one.
    """
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query))
    query[DETECT_PARAM] = "1"
    return urlunsplit(parts._replace(query=urlencode(query)))
