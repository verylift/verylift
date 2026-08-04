"""Production settings for very lift.

Imports the base settings and overrides only what differs in production. The app
runs behind Pangolin (external tunnel / reverse proxy on a VPS) which terminates
TLS and forwards the X-Forwarded-Proto header, so Django must trust that header
to know the original request was HTTPS.

Activate with: DJANGO_SETTINGS_MODULE=root.settings_prod
"""

from .settings import *  # noqa: F403
from .settings import env

# DEBUG is force-disabled in production regardless of the .env value.
DEBUG = False

# Required in production — no localhost default. Re-derived here (not just
# inherited via the wildcard import above) because
# root/tests/test_prod_settings.py reloads only this module to test
# different ALLOWED_HOSTS scenarios — importlib.reload(settings_prod) does not
# re-execute root.settings, so these must be recomputed here to reflect
# a freshly monkeypatched env var.
ALLOWED_HOSTS = env("ALLOWED_HOSTS")
CSRF_TRUSTED_ORIGINS = [
    f"https://*{host}" if host.startswith(".") else f"https://{host}"
    for host in ALLOWED_HOSTS
]

# SECURE_PROXY_SSL_HEADER is inherited from the base settings (wildcard import
# above) -- both dev and prod need it, see the comment there.

# Cookies only travel over HTTPS.
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True

# Redirect any plain-HTTP request that still reaches the app to HTTPS.
SECURE_SSL_REDIRECT = True

# HSTS: tell browsers to only ever use HTTPS for this host.
SECURE_HSTS_SECONDS = 31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True

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
