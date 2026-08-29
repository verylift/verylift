"""HTTP client module for the Hevy API.

This module owns all HTTP concerns (auth headers, pagination, error handling,
response parsing) and nothing else — no Django ORM, no business logic.

Endpoint shapes and auth scheme verified against Hevy's published OpenAPI spec
(https://api.hevyapp.com/docs/, TASK-312). Access requires an active Hevy Pro
subscription; there is no free tier for this API, unlike the CSV export
workout_imports.importers.hevy already supports.
"""

import logging
import urllib.error
import urllib.request

from django.conf import settings

from core.http import build_url, read_error_body, send_request

logger = logging.getLogger(__name__)

# Hevy caps pageSize at 10 (verified against the published OpenAPI spec) --
# far smaller than Liftosaur's 200, so a real sync walks noticeably more pages
# for the same amount of history.
MAX_PAGE_SIZE = 10


class HevyAPIError(Exception):
    """Raised when the Hevy API returns a non-2xx response."""

    def __init__(self, status_code: int, body: str) -> None:
        self.status_code = status_code
        self.body = body
        super().__init__(f"Hevy API error {status_code}: {body}")


class HevyClient:
    """HTTP client for the Hevy API.

    All methods are synchronous. No business logic lives here — callers are
    responsible for interpreting results.
    """

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key
        self._base_url = settings.HEVY_API_BASE

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _request(self, method: str, path: str, params: dict | None = None) -> dict:
        """Make an authenticated HTTP request and return the parsed JSON body.

        Raises:
            HevyAPIError: for any non-2xx HTTP response.
            urllib.error.URLError: for network-level failures (propagated as-is).
        """
        url = build_url(f"{self._base_url}{path}", params)
        req = urllib.request.Request(
            url,
            method=method,
            headers={"api-key": self._api_key},
        )
        logger.info("Hevy API %s %s", method, url)
        try:
            _status, data = send_request(req, timeout=settings.HEVY_API_TIMEOUT)
        except urllib.error.HTTPError as exc:
            logger.warning("Hevy API returned %s for %s %s", exc.code, method, path)
            raise HevyAPIError(exc.code, read_error_body(exc)) from exc
        return data

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_workouts(self, page: int = 1, page_size: int = MAX_PAGE_SIZE) -> dict:
        """Fetch one page of the user's workouts, newest-page-1 order unspecified.

        Returns the raw ``{"page", "page_count", "workouts"}`` payload.

        Raises:
            HevyAPIError: on non-2xx responses.
            urllib.error.URLError: on network failures.
        """
        return self._request(
            "GET", "/v1/workouts", {"page": page, "pageSize": page_size}
        )

    def get_body_measurements(
        self, page: int = 1, page_size: int = MAX_PAGE_SIZE
    ) -> dict:
        """Fetch one page of the user's body measurements.

        Hevy DOES expose bodyweight, contrary to the assumption this app
        carried while only workouts were wired up: ``GET
        /v1/body_measurements`` is present in Hevy's live OpenAPI spec at
        https://api.hevyapp.com/docs/ (operationId ``getV1BodyMeasurements``,
        tag "Measurements", summary "Get a paginated list of body
        measurements for the authenticated user"), alongside a
        ``/v1/body_measurements/{date}`` detail route. Each row is a
        ``BodyMeasurement``: ``date`` (``YYYY-MM-DD``, the only required
        field) plus a set of nullable metrics of which ``weight_kg`` is the
        one this app has any use for. Note the unit is fixed in the field
        name -- Hevy normalises to kg on the wire regardless of the display
        unit the lifter uses in the app.

        The spec documents ``page``/``pageSize`` (max 10, same cap as
        workouts) and a ``{"page", "page_count", "body_measurements"}``
        envelope, but -- unlike ``/v1/workouts/events``, whose summary states
        its ordering outright -- says nothing about what order rows come back
        in. Callers must therefore not assume page 1 holds the newest
        reading; see ``hevy_api.services.fetch_latest_bodyweight``.

        Returns the raw ``{"page", "page_count", "body_measurements"}``
        payload.

        Raises:
            HevyAPIError: on non-2xx responses.
            urllib.error.URLError: on network failures.
        """
        return self._request(
            "GET", "/v1/body_measurements", {"page": page, "pageSize": page_size}
        )

    def get_workout_events(
        self, since: str, page: int = 1, page_size: int = MAX_PAGE_SIZE
    ) -> dict:
        """Fetch one page of workout create/update/delete events since ``since``.

        ``since`` is an ISO 8601 timestamp. This is the endpoint that drives
        both the initial backfill (``since="1970-01-01T00:00:00Z"``) and every
        subsequent delta sync (``since=<watermark>``) — Hevy's own
        recommended way for a client to keep a local cache in sync without
        refetching the full workout list.

        Events are returned newest-first, not oldest-first (TASK-325).
        Confirmed directly against Hevy's own published OpenAPI spec served
        at https://api.hevyapp.com/docs/ (title "Hevy API Docs", server
        api.hevyapp.com) — the ``GET /v1/workouts/events`` operation summary
        reads verbatim: "Retrieve a paged list of workout events (updates or
        deletes) since a given date. Events are ordered from newest to
        oldest." (cross-checked against the same spec as mirrored in
        https://github.com/chrisdoc/hevy-mcp/blob/main/openapi-spec.json,
        since Hevy's Swagger UI renders client-side and doesn't serve the
        raw JSON at a stable URL). hevy_api.services.pull_events_into_pool
        does not rely on this ordering for correctness — see that module's
        docstring — but it explains why a truncated page walk pools the
        newest slice of what changed, not the oldest.

        Returns the raw ``{"page", "page_count", "events"}`` payload. Each
        event is either ``{"type": "updated", "workout": {...}}`` or
        ``{"type": "deleted", "id": ..., "deleted_at": ...}``.

        Raises:
            HevyAPIError: on non-2xx responses.
            urllib.error.URLError: on network failures.
        """
        return self._request(
            "GET",
            "/v1/workouts/events",
            {"since": since, "page": page, "pageSize": page_size},
        )
