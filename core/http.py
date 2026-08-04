"""Shared HTTP plumbing for the project.

Owns the urllib request/response mechanics for the synchronous API clients (URL
building, urlopen, body decoding, and safe error-body extraction) that were
previously duplicated line-for-line across the Liftosaur and FitnessVolt
clients — each client keeps its own exception classes and error-handling policy;
this module only removes the shared boilerplate. It also holds small
request-inspection helpers (``is_htmx``) shared across the app's views.
"""

import contextlib
import json
import urllib.error
import urllib.parse
import urllib.request


def is_htmx(request) -> bool:
    """True when the request was issued by htmx (HX-Request header present)."""
    return bool(request.headers.get("HX-Request"))


def build_url(base_url: str, params: dict | None = None) -> str:
    """Append a query string to ``base_url``, dropping ``None`` values.

    Returns ``base_url`` unchanged when there are no non-``None`` params.
    """
    if params:
        filtered = {k: v for k, v in params.items() if v is not None}
        if filtered:
            return f"{base_url}?{urllib.parse.urlencode(filtered)}"
    return base_url


def read_error_body(exc: urllib.error.HTTPError) -> str:
    """Best-effort decode of an HTTPError response body; ``""`` on failure."""
    body = ""
    with contextlib.suppress(Exception):
        body = exc.read().decode("utf-8")
    return body


def send_request(
    req: urllib.request.Request, *, timeout: float
) -> tuple[int, dict | list]:
    """Perform ``req`` and return ``(status_code, parsed_json)``.

    An empty body parses to ``{}``. ``urllib.error.HTTPError`` (non-2xx) and
    ``urllib.error.URLError`` (network failures) propagate to the caller, which
    owns how they map onto client-specific exceptions.
    """
    with urllib.request.urlopen(req, timeout=timeout) as response:
        status = response.status
        raw = response.read().decode("utf-8")
    return status, (json.loads(raw) if raw else {})
