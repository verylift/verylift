"""Gate authenticated access behind consent to any active, gated policy version.

Deliberately does not cache the required-version-pk list (unlike the
django-poc source this was ported from): verylift's default cache is an
in-process LocMemCache (see root/settings.py's CACHES comment — the shared
DatabaseCache alias is reserved for django-ratelimit), and gunicorn runs
several worker processes in production. A per-worker cache would let one
worker invalidate its own copy on a version activation while the others kept
serving a stale (or empty) required-list for up to an hour, silently letting
some requests through ungated. PolicyVersion rows are few and the lookup is a
single indexed query, so querying fresh on every request is cheap enough to
skip the correctness risk entirely.
"""

import logging

from django.shortcuts import redirect
from django.urls import reverse

logger = logging.getLogger(__name__)


def _exempt_prefixes():
    # Imported lazily so this always reflects the current settings (tests
    # override settings.ADMIN_URL_PATH per-test via the settings fixture).
    from django.conf import settings

    return (
        f"/{settings.ADMIN_URL_PATH}",
        "/oidc/",
        "/accounts/logout/",
        "/accounts/login/",
        "/accounts/register/",
        "/accounts/password-reset/",
        "/accounts/reset/",
        "/media/",
        settings.STATIC_URL,
        "/healthz",
        "/terms/",
        "/privacy/",
        "/policies/",
    )


class PolicyConsentMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.user.is_authenticated and not request.path.startswith(
            _exempt_prefixes()
        ):
            required_pks = self._required_pks()
            if required_pks:
                consented_pks = set(
                    request.user.policy_consents.filter(
                        policy_version_id__in=required_pks
                    ).values_list("policy_version_id", flat=True)
                )
                if not set(required_pks).issubset(consented_pks):
                    logger.info(
                        "Redirecting user %s to policy consent (path=%s)",
                        request.user.id,
                        request.path,
                    )
                    consent_url = reverse("policies:consent")
                    return redirect(f"{consent_url}?next={request.path}")
        return self.get_response(request)

    def _required_pks(self):
        from policies.models import PolicyVersion

        return list(PolicyVersion.objects.active_gated().values_list("pk", flat=True))
