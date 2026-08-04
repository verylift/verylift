"""HTTP client module for the FitnessVolt Strength Standards (FVSS) v1 API.

This module owns all HTTP concerns (URL building, error handling, response
parsing) and nothing else — no Django ORM, no business logic. It mirrors
liftosaur.client.LiftosaurClient: raw urllib.request, a custom exception
carrying the HTTP status and body, single attempt per call, no retries.

The FVSS API needs no authentication. Every response carries a
``data_version`` field (the snapshot identifier, stored verbatim by the
service layer) and an ``api_version`` field (the API contract version,
ignored here).
"""

import logging
import urllib.error
import urllib.parse
import urllib.request

from django.conf import settings

from core.http import build_url, read_error_body, send_request

logger = logging.getLogger(__name__)

API_PREFIX = "/wp-json/fvss/v1"


class FitnessVoltAPIError(Exception):
    """Raised when the FitnessVolt API returns a non-2xx response.

    On a 429 the parsed ``Retry-After`` header (seconds) is attached as
    ``retry_after`` so the out-of-band refresh job can log it; there is no
    inline retry loop, matching the single-attempt Liftosaur precedent.
    """

    def __init__(
        self, status_code: int, body: str, retry_after: int | None = None
    ) -> None:
        self.status_code = status_code
        self.body = body
        self.retry_after = retry_after
        super().__init__(f"FitnessVolt API error {status_code}: {body}")


def _parse_retry_after(value: str | None) -> int | None:
    """Parse a Retry-After header value into whole seconds, if possible."""
    if value is None:
        return None
    try:
        return int(value.strip())
    except (ValueError, AttributeError):
        return None


class FitnessVoltClient:
    """HTTP client for the FitnessVolt Strength Standards v1 API.

    All methods are synchronous. The only caller is the out-of-band cache
    refresh (fitnessvolt.services.refresh_cache) — never a request handler.
    """

    def __init__(self) -> None:
        self._base_url = settings.FITNESSVOLT_API_BASE

    def _request_raising(self, path: str, params: dict | None = None) -> dict:
        """Make a GET request and return the parsed JSON body.

        Raises:
            FitnessVoltAPIError: for any non-2xx HTTP response (with
                retry_after populated on a 429).
            urllib.error.URLError: for network-level failures (propagated
                as-is).
        """
        url = build_url(f"{self._base_url}{API_PREFIX}{path}", params)
        req = urllib.request.Request(
            url,
            method="GET",
            headers={
                "User-Agent": "verylift/1.0 (+https://github.com/verylift/verylift)"
            },
        )
        logger.info("FitnessVolt API GET %s", url)
        try:
            _status, data = send_request(req, timeout=settings.FITNESSVOLT_API_TIMEOUT)
        except urllib.error.HTTPError as exc:
            body = read_error_body(exc)
            retry_after = None
            if exc.code == 429:
                retry_after = _parse_retry_after(exc.headers.get("Retry-After"))
                logger.warning(
                    "FitnessVolt API rate-limited GET %s (Retry-After: %s)",
                    path,
                    retry_after,
                )
            else:
                logger.warning("FitnessVolt API returned %s for GET %s", exc.code, path)
            raise FitnessVoltAPIError(exc.code, body, retry_after=retry_after) from exc

        return data

    def get_capabilities(self) -> dict:
        """Fetch the /standards capability doc.

        Returns the parsed document. Lifts live under
        ``sources.<population>.lifts`` as ``{"lift": slug, "label": name}``
        objects (verified uses hyphenated multi-word slugs like
        ``bench-press``; gym uses underscored slugs like ``back_squat``)::

            {
                "success": true,
                "api_version": "1.0.0",
                "data_version": "2026-06-09",
                "sources": {
                    "verified": {
                        "population": "verified_challenge",
                        "lifts": [
                            {"lift": "squat", "label": "Back Squat"},
                            {"lift": "bench-press", "label": "Bench Press"},
                            ...
                        ],
                        "sexes": ["male", "female"],
                        ...
                    },
                    "gym": {...},
                },
            }

        Raises:
            FitnessVoltAPIError: on non-2xx responses.
            urllib.error.URLError: on network failures.
        """
        return self._request_raising("/standards")

    def get_lift_standards(self, lift_slug: str, population: str, sex: str) -> dict:
        """Fetch /standards/{lift} for one lift, population, and sex.

        ``sex`` is the API-side value (``male``/``female``) — both ``sex``
        and ``source`` are required query params; each call returns only one
        sex's weight-class table. ``format=table`` and ``unit=kg`` are always
        requested so cached weight classes never need unit conversion at read
        time (doc-1 §2). Returns the parsed document, shaped like::

            {
                "success": true,
                "data_version": "2026-06-09",
                "lift": "squat",
                "sex": "male",
                "source": "verified_challenge",
                "unit": "kg",
                "format": "table",
                "weight_classes": [
                    {
                        "weight_class": 83,
                        "weight_class_label": "83 kg",
                        "sample_size": 78720,
                        "percentiles": {"p10": 140, ..., "p99": 270.5},
                    },
                    ...
                ],
            }

        Cohorts with fewer than 30 samples are omitted by FitnessVolt itself.

        Raises:
            FitnessVoltAPIError: on non-2xx responses.
            urllib.error.URLError: on network failures.
        """
        quoted = urllib.parse.quote(lift_slug, safe="")
        return self._request_raising(
            f"/standards/{quoted}",
            params={
                "source": population,
                "sex": sex,
                "format": "table",
                "unit": "kg",
            },
        )
