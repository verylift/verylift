"""Timezone resolution helpers for TASK-273.

Owns the priority ladder that decides which IANA timezone (if any) applies to
a request -- a pinned ``User.timezone`` first, then the browser-detected
``pp_timezone`` cookie, then "no opinion" (the caller falls back to
``settings.TIME_ZONE``) -- plus the small URL/validation helpers the
detection round-trip (accounts.middleware.UserTimezoneMiddleware,
accounts.views.timezone_detect_view) needs to stay a single one-shot redirect.
"""

import logging
from datetime import date, datetime
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo, available_timezones

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


def cookie_timezone(request) -> str | None:
    """The browser-detected timezone from the ``pp_timezone`` cookie, if valid.

    Split out of ``resolve_timezone`` (TASK-300) so
    ``accounts.middleware.UserTimezoneMiddleware`` can also read just this
    part -- to opportunistically persist it onto ``User.detected_timezone``
    -- without re-deriving the pinned-``User.timezone`` priority above it.
    """
    raw_cookie_value = request.COOKIES.get(TIMEZONE_COOKIE_NAME)
    if not raw_cookie_value:
        return None
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

    return cookie_timezone(request)


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


def user_zoneinfo(user) -> ZoneInfo:
    """The IANA zone to interpret a user's civil dates in, with no request.

    Priority: their pinned ``accounts.User.timezone`` (an explicit Settings
    choice), then their opportunistically-persisted ``detected_timezone``
    (UserTimezoneMiddleware's best last-known browser zone for an
    "automatic" account), then UTC. This is ``resolve_timezone``'s ladder
    with the live ``pp_timezone`` cookie step replaced by its persisted
    stand-in, for background code -- cron jobs, sync threads -- that has a
    user but no request in sight.
    """
    for tz_name in (user.timezone, user.detected_timezone):
        if tz_name and is_valid_timezone(tz_name):
            return ZoneInfo(tz_name)
    return ZoneInfo("UTC")


def local_day(moment: datetime, tz: ZoneInfo) -> date:
    """The calendar day ``moment`` falls on in ``tz``.

    The single conversion point for turning a *timestamp* from an external
    workout source into the plain ``LiftHistory.performed_at`` DateField, which
    every downstream reader (leaderboard windows, the challenge detail page's
    Recent Activity dates) treats as the civil day the lifter trained. A source
    that reports UTC instants -- Hevy's ``start_time``, and any Wger instance
    left on ``TIME_ZONE = "UTC"`` -- would otherwise file a late-evening
    session in a western zone (or an early-morning one in an eastern zone)
    under the wrong day.

    A naive ``moment`` is taken at face value rather than converted: sources
    that hand back naive timestamps (a Wger instance with ``USE_TZ`` off, the
    Hevy and Strong CSV exports) are already reporting the lifter's own wall
    clock, and running those through ``astimezone`` would reinterpret them as
    server-local and shift a day the other way.
    """
    if moment.tzinfo is None:
        return moment.date()
    return moment.astimezone(tz).date()
