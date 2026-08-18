"""HTTP client module for the Wger REST API.

This module owns all HTTP concerns (auth headers, pagination, error handling,
response parsing) and nothing else -- no Django ORM, no business logic.

Unlike Liftosaur, Wger is self-hostable: there is no single fixed base URL, so
the base URL is per-user, supplied alongside the API token.

Verified against Wger's published docs
(https://wger.readthedocs.io/en/latest/api/api.html) and the wger-project/wger
source on GitHub (wger/manager/api/{views,filtersets,serializers}.py,
wger/exercises/api/{views,serializers}.py):

- Auth: ``Authorization: Token <token>`` header (the "permanent token" scheme;
  Wger's docs mark this deprecated in favor of short-lived JWTs obtained via
  ``/api/v2/token``, but it's the only scheme that doesn't require re-deriving
  a refresh flow, and it's still fully supported).
- Pagination: DRF LimitOffsetPagination -- ``limit``/``offset`` query params,
  response envelope ``{"count", "next", "previous", "results"}``.
- Workout logs: ``GET /api/v2/workoutlog/`` (``WorkoutLogViewSet``), scoped to
  the requesting user server-side. Supports ``date__gte``/``date__lte``
  filters (``WorkoutLogFilterSet``). Each entry references its exercise by a
  numeric ``exercise`` ID and its units by numeric ``weight_unit``/
  ``repetitions_unit`` IDs -- Wger's exercise database is normalized, so there
  is no raw exercise-name string on the log entry itself.
- Exercise names: ``GET /api/v2/exerciseinfo/<id>/`` returns per-language
  ``translations``; the human-readable name lives at
  ``translations[i]["name"]``.

NOT independently verified against a live instance (auth-gated / not
reachable from this environment; inferred from wger's source and its default
data fixtures, which every instance ships with and self-hosters are not
expected to edit): the numeric IDs of the weight-unit and repetition-unit
reference tables (``weightunit`` id 1 = kg, id 2 = lb; ``repetitionunit`` id 1
= "Repetitions"). If a self-hosted instance has actually re-numbered these
fixture rows, weight/rep unit resolution below would be wrong for that
instance.
"""

import logging
import urllib.error
import urllib.request

from core.http import build_url, read_error_body, send_request

logger = logging.getLogger(__name__)

# Wger's default fixture data (shipped with every instance, not user-editable
# in the normal course of using the app). See the module docstring's
# "NOT independently verified" note.
WEIGHT_UNIT_KG_ID = 1
WEIGHT_UNIT_LB_ID = 2
REPETITION_UNIT_REPS_ID = 1

# Wger's default translation language ID for English.
ENGLISH_LANGUAGE_ID = 2


class WgerAPIError(Exception):
    """Raised when the Wger API returns a non-2xx response."""

    def __init__(self, status_code: int, body: str) -> None:
        self.status_code = status_code
        self.body = body
        super().__init__(f"Wger API error {status_code}: {body}")


class WgerClient:
    """HTTP client for a self-hosted Wger instance's REST API.

    All methods are synchronous. No business logic lives here -- callers are
    responsible for interpreting results (unit conversion, alias resolution,
    etc).
    """

    def __init__(self, base_url: str, api_token: str, *, timeout: float = 10) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_token = api_token
        self._timeout = timeout

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _request(
        self, method: str, path: str, params: dict | None = None
    ) -> dict | list:
        """Make an authenticated HTTP request and return the parsed JSON body.

        Raises:
            WgerAPIError: for any non-2xx HTTP response.
            urllib.error.URLError: for network-level failures (propagated as-is).
        """
        url = build_url(f"{self._base_url}{path}", params)
        req = urllib.request.Request(
            url,
            method=method,
            headers={"Authorization": f"Token {self._api_token}"},
        )
        logger.info("Wger API %s %s", method, url)
        _status, data = send_request(req, timeout=self._timeout)
        return data

    def _request_raising(
        self, method: str, path: str, params: dict | None = None
    ) -> dict | list:
        try:
            return self._request(method, path, params)
        except urllib.error.HTTPError as exc:
            logger.warning("Wger API returned %s for %s %s", exc.code, method, path)
            raise WgerAPIError(exc.code, read_error_body(exc)) from exc

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_workout_logs(
        self,
        date_gte: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[dict], bool, int]:
        """Fetch a page of the user's workout log entries.

        Args:
            date_gte: Optional ISO date string (``YYYY-MM-DD``); only entries
                on or after this date are returned.
            limit: Page size.
            offset: Row offset for this page.

        Returns:
            (entries, has_more, next_offset) where each entry is the raw
            WorkoutLog dict (``exercise``, ``date``, ``weight``,
            ``repetitions``, ``weight_unit``, ``repetitions_unit``, ...).

        Raises:
            WgerAPIError: on non-2xx responses.
            urllib.error.URLError: on network failures.
        """
        params: dict = {"limit": limit, "offset": offset, "ordering": "date"}
        if date_gte is not None:
            params["date__gte"] = date_gte

        data = self._request_raising("GET", "/api/v2/workoutlog/", params)

        entries: list[dict] = []
        has_more = False
        if isinstance(data, dict):
            results = data.get("results")
            if isinstance(results, list):
                entries = results
            has_more = bool(data.get("next"))

        return entries, has_more, offset + limit

    def get_exercise_name(self, exercise_id: int) -> str | None:
        """Resolve a numeric exercise ID to its human-readable English name.

        Wger's exercise database is normalized (workout logs carry only a
        numeric exercise ID), so this is a second round-trip per unique
        exercise. Returns None if the exercise has no name in any language
        Wger returned, or the lookup itself fails.
        """
        try:
            data = self._request_raising(
                "GET",
                f"/api/v2/exerciseinfo/{exercise_id}/",
                {"language": ENGLISH_LANGUAGE_ID},
            )
        except WgerAPIError as exc:
            logger.warning(
                "Wger exercise name lookup failed for exercise %s: %s",
                exercise_id,
                exc,
            )
            return None

        if not isinstance(data, dict):
            return None
        translations = data.get("translations")
        if not isinstance(translations, list) or not translations:
            return None

        for translation in translations:
            if (
                isinstance(translation, dict)
                and translation.get("language") == ENGLISH_LANGUAGE_ID
                and translation.get("name")
            ):
                return translation["name"]

        first = translations[0]
        return first.get("name") if isinstance(first, dict) else None
