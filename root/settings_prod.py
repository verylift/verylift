"""Production settings for very lift.

Imports the base settings and overrides only what differs in production. The app
runs behind a reverse proxy or tunnel that terminates TLS and forwards the
X-Forwarded-Proto header, so Django must trust that header to know the original
request was HTTPS.

Activate with: DJANGO_SETTINGS_MODULE=root.settings_prod
"""

import sentry_sdk
from sentry_sdk.integrations.django import DjangoIntegration

from .settings import *  # noqa: F403
from .settings import env

# DEBUG is force-disabled in production regardless of the .env value.
DEBUG = False

# Error tracking and log forwarding via a Sentry-compatible DSN. GLITCHTIP_DSN
# is blank by default -- sentry_sdk.init with an empty dsn is a documented
# no-op, so any deployment that never sets it simply doesn't send anything,
# no separate on/off flag needed.
#
# send_default_pii=False: don't attach request bodies, cookies, or other
# ambient PII sentry_sdk would otherwise infer -- events should carry only
# what our own logger calls explicitly say.
#
# enable_logs=True forwards logger.* calls (see LOGGING below and CLAUDE.md's
# logging conventions) to GlitchTip's Logs feature over the same DSN, so
# existing logger.info/warning/exception call sites get log aggregation for
# free without a separate shipping pipeline.
sentry_sdk.init(
    dsn=env("GLITCHTIP_DSN", default=""),
    integrations=[DjangoIntegration()],
    send_default_pii=False,
    enable_logs=True,
)

# Required in production — no localhost default. Re-derived here (not just
# inherited via the wildcard import above) because
# root/tests/test_prod_settings.py reloads only this module to test
# different ALLOWED_HOSTS scenarios — importlib.reload(settings_prod) does not
# re-execute root.settings, so these must be recomputed here to reflect
# a freshly monkeypatched env var.
ALLOWED_HOSTS = env("ALLOWED_HOSTS")

# Whether this deployment is reachable over HTTPS. True by default: anything
# on the public internet should be, and the maintainers' own deployment sits
# behind a TLS-terminating tunnel.
#
# Self-hosters running on a trusted LAN with no TLS in front should set
# HTTPS_ENABLED=False. Leaving it on there makes the app unusable rather than
# merely strict -- every request is redirected to an https:// URL nothing is
# serving, and the session cookie is withheld over plain HTTP, so login
# cannot complete. Turning it off disables the redirect, the Secure cookie
# flag, and HSTS, and matches CSRF origins on http:// instead.
#
# This is a real reduction in protection: traffic (including passwords) is
# then readable by anything on the same network. Only set it False when you
# trust that network, never on an internet-facing install.
HTTPS_ENABLED = env.bool("HTTPS_ENABLED", default=True)

_origin_scheme = "https" if HTTPS_ENABLED else "http"
CSRF_TRUSTED_ORIGINS = [
    f"{_origin_scheme}://*{host}"
    if host.startswith(".")
    else f"{_origin_scheme}://{host}"
    for host in ALLOWED_HOSTS
]

# SECURE_PROXY_SSL_HEADER is inherited from the base settings (wildcard import
# above) -- both dev and prod need it, see the comment there.

# Cookies only travel over HTTPS.
SESSION_COOKIE_SECURE = HTTPS_ENABLED
CSRF_COOKIE_SECURE = HTTPS_ENABLED

# Redirect any plain-HTTP request that still reaches the app to HTTPS.
SECURE_SSL_REDIRECT = HTTPS_ENABLED

# HSTS: tell browsers to only ever use HTTPS for this host. Must be 0 when
# HTTPS is off -- a browser that saw a non-zero max-age once will refuse
# plain HTTP to that host for that long afterwards, which would outlive the
# setting change and look like the app breaking for no reason.
SECURE_HSTS_SECONDS = 31536000 if HTTPS_ENABLED else 0
SECURE_HSTS_INCLUDE_SUBDOMAINS = HTTPS_ENABLED
SECURE_HSTS_PRELOAD = HTTPS_ENABLED

# Defense-in-depth headers.
SECURE_CONTENT_TYPE_NOSNIFF = True

# WhiteNoise serves collected static files directly from Gunicorn — no separate
# static file server needed behind the main proxy. The base MIDDLEWARE already includes
# WhiteNoiseMiddleware immediately after SecurityMiddleware, so nothing to add here.

# Compressed, hashed static files with a manifest for long-term caching.
STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
    },
}
