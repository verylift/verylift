"""Container health-check middleware.

Serves ``GET /healthz`` for orchestration liveness/readiness probes. Implemented
as middleware rather than a URL-routed view so it runs *before* SecurityMiddleware:
in production ``SECURE_SSL_REDIRECT`` would otherwise 301 a plain-HTTP loopback
probe, and the loopback host is intentionally absent from ``ALLOWED_HOSTS``. Short
-circuiting here sidesteps both without weakening either setting for real traffic.
"""

import logging

from django.db import connection
from django.http import JsonResponse

logger = logging.getLogger(__name__)

_HEALTH_PATH = "/healthz"

# Loopback only. gunicorn is not behind a reverse proxy in production, so any
# non-loopback REMOTE_ADDR is an external probe and gets a 404 (endpoint hidden).
# REVISIT if a reverse proxy is added: REMOTE_ADDR would then be the proxy's
# address and this check would need X-Forwarded-For handling instead.
_LOOPBACK_ADDRS = frozenset({"127.0.0.1", "::1"})


class HealthCheckMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path == _HEALTH_PATH:
            return self._healthz(request)
        return self.get_response(request)

    def _healthz(self, request):
        if request.META.get("REMOTE_ADDR") not in _LOOPBACK_ADDRS:
            return JsonResponse({"status": "not_found"}, status=404)

        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()
        except Exception:
            logger.exception("Health check failed: database unreachable")
            return JsonResponse({"status": "unhealthy", "database": "down"}, status=503)

        return JsonResponse({"status": "ok", "database": "up"})
