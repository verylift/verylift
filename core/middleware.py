"""Container health-check middleware.

Serves ``GET /healthz`` for orchestration liveness/readiness probes. Implemented
as middleware rather than a URL-routed view so it runs *before* SecurityMiddleware:
in production ``SECURE_SSL_REDIRECT`` would otherwise 301 a plain-HTTP probe, and
the probing host is intentionally absent from ``ALLOWED_HOSTS``. Short-circuiting
here sidesteps both without weakening either setting for real traffic.

Unauthenticated and reachable from any address: under Kamal, kamal-proxy is a
separate container reaching this endpoint over Kamal's private Docker network,
so ``REMOTE_ADDR`` is never loopback there. The app container has no
host-published port at all (only kamal-proxy does), so exposure is already
bounded by network topology, not by a check here. The response body carries
nothing sensitive.
"""

import logging

from django.conf import settings
from django.db import connection
from django.http import JsonResponse

logger = logging.getLogger(__name__)

_HEALTH_PATH = "/healthz"


class HealthCheckMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path == _HEALTH_PATH:
            return self._healthz(request)
        return self.get_response(request)

    def _healthz(self, request):
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()
        except Exception:
            logger.exception("Health check failed: database unreachable")
            return JsonResponse(
                {
                    "status": "unhealthy",
                    "database": "down",
                    "version": settings.APP_VERSION,
                },
                status=503,
            )

        return JsonResponse(
            {"status": "ok", "database": "up", "version": settings.APP_VERSION}
        )
