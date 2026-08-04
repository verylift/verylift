"""HTTP client module for the Liftosaur API.

This module owns all HTTP concerns (auth headers, pagination, error handling,
response parsing) and nothing else — no Django ORM, no business logic.
"""

import logging
import urllib.error
import urllib.request

from django.conf import settings

from core.http import build_url, read_error_body, send_request

logger = logging.getLogger(__name__)


class LiftosaurAPIError(Exception):
    """Raised when the Liftosaur API returns a non-2xx response."""

    def __init__(self, status_code: int, body: str) -> None:
        self.status_code = status_code
        self.body = body
        super().__init__(f"Liftosaur API error {status_code}: {body}")


def _record_text(record: object) -> str:
    """Extract the Liftohistory text from a history record.

    The API returns records as ``{"id": ..., "text": "..."}`` dicts; fall back
    to ``str`` for plain-string records.
    """
    if isinstance(record, dict) and "text" in record:
        return record["text"]
    return str(record)


class LiftosaurClient:
    """HTTP client for the Liftosaur API.

    All methods are synchronous.  No business logic lives here — callers are
    responsible for interpreting results.
    """

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key
        self._base_url = settings.LIFTOSAUR_API_BASE

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _request(
        self, method: str, path: str, params: dict | None = None
    ) -> dict | list:
        """Make an authenticated HTTP request and return the parsed JSON body.

        Raises:
            LiftosaurAPIError: for any non-2xx HTTP response.
            urllib.error.URLError: for network-level failures (propagated as-is).
        """
        url = build_url(f"{self._base_url}{path}", params)
        req = urllib.request.Request(
            url,
            method=method,
            headers={"Authorization": f"Bearer {self._api_key}"},
        )
        logger.info("Liftosaur API %s %s", method, url)
        _status, data = send_request(req, timeout=settings.LIFTOSAUR_API_TIMEOUT)
        return data

    def _request_raising(
        self, method: str, path: str, params: dict | None = None
    ) -> dict | list:
        """Like _request but converts HTTP errors to LiftosaurAPIError.

        The Liftosaur REST API wraps every payload in a top-level ``data``
        envelope (e.g. ``{"data": {"records": [...]}}``). When present, the
        envelope is unwrapped so callers see the inner payload directly; an
        un-enveloped body is returned as-is.
        """
        try:
            result = self._request(method, path, params)
        except urllib.error.HTTPError as exc:
            logger.warning(
                "Liftosaur API returned %s for %s %s", exc.code, method, path
            )
            raise LiftosaurAPIError(exc.code, read_error_body(exc)) from exc

        if isinstance(result, dict) and set(result.keys()) == {"data"}:
            return result["data"]
        return result

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_weight_measurements(
        self,
        limit: int = 50,
        cursor: str | None = None,
    ) -> tuple[list[dict], bool, str | None]:
        """Fetch weight measurements from the Liftosaur API.

        Args:
            limit: Maximum number of measurements to return.
            cursor: Pagination cursor from a previous call.

        Returns:
            (values, has_more, next_cursor) where each value dict contains
            at least ``value`` (raw string like ``'80kg'``) and a timestamp.

        Raises:
            LiftosaurAPIError: on non-2xx responses.
            urllib.error.URLError: on network failures.
        """
        params: dict = {"limit": limit}
        if cursor is not None:
            params["cursor"] = cursor

        data = self._request_raising("GET", "/api/v1/measurements/weight", params)

        values: list[dict] = []
        has_more = False
        next_cursor: str | None = None

        if isinstance(data, list):
            values = data
        elif isinstance(data, dict):
            for key in ("measurements", "values", "data"):
                if key in data and isinstance(data[key], list):
                    values = data[key]
                    break
            else:
                if "value" in data:
                    values = [data]
            has_more = bool(data.get("hasMore") or data.get("has_more"))
            next_cursor = (
                data.get("cursor") or data.get("next_cursor") or data.get("nextCursor")
            )

        return values, has_more, next_cursor

    def get_history(
        self,
        start_date: str | None = None,
        end_date: str | None = None,
        cursor: str | None = None,
        limit: int | None = None,
    ) -> tuple[list[str], bool, str | None]:
        """Fetch workout history records in Liftohistory format.

        Args:
            start_date: Optional ISO date string (``YYYY-MM-DD``) to filter from.
            end_date: Optional ISO date string (``YYYY-MM-DD``) to filter to.
                Liftosaur normalizes endDate to midnight UTC (00:00:00.000Z) and
                filters records with a string date-range comparison — passing
                today's date as endDate silently excludes any workout completed
                earlier that same UTC day. Omit end_date for routine/delta syncs;
                only pass it deliberately for a bounded historical window.
            cursor: Pagination cursor from a previous call. Note: Liftosaur
                ignores the cursor whenever startDate is set and returns the
                first page repeatedly.
            limit: Optional page size (server default 50, max 200).

        Returns:
            (record_texts, has_more, next_cursor) where each record_text is the
            raw Liftohistory-format string for one workout entry.

        Raises:
            LiftosaurAPIError: on non-2xx responses.
            urllib.error.URLError: on network failures.
        """
        params: dict = {}
        if start_date is not None:
            params["startDate"] = start_date
        if end_date is not None:
            params["endDate"] = end_date
        if cursor is not None:
            params["cursor"] = cursor
        if limit is not None:
            params["limit"] = limit

        data = self._request_raising("GET", "/api/v1/history", params or None)

        record_texts: list[str] = []
        has_more = False
        next_cursor: str | None = None

        if isinstance(data, list):
            record_texts = [_record_text(r) for r in data]
        elif isinstance(data, dict):
            for key in ("history", "records", "data"):
                if key in data and isinstance(data[key], list):
                    record_texts = [_record_text(r) for r in data[key]]
                    break
            has_more = bool(data.get("hasMore") or data.get("has_more"))
            next_cursor = (
                data.get("cursor") or data.get("next_cursor") or data.get("nextCursor")
            )

        return record_texts, has_more, next_cursor
