"""System checks for the core app.

Registered from CoreConfig.ready(). An *absent* FIELD_ENCRYPTION_KEYS already
fails at settings import, so this check exists for the *malformed* case: a
truncated or non-base64 key otherwise sits unnoticed until the first user saves
an encrypted value. System checks run before migrate, runserver and
collectstatic, so a bad key fails loudly at deploy time instead.
"""

from django.conf import settings
from django.core import checks
from django.core.exceptions import ImproperlyConfigured

from core.encryption import _multifernet


@checks.register(checks.Tags.security)
def check_field_encryption_keys(app_configs, **kwargs):
    try:
        _multifernet()
    except ImproperlyConfigured as exc:
        return [
            checks.Error(
                str(exc),
                hint=(
                    "See the field-encryption block in example.env. Each key "
                    "must be 32 url-safe base64-encoded bytes (44 characters); "
                    "the first key encrypts new values and the rest are retired "
                    "keys kept for decryption."
                ),
                id="core.E001",
            )
        ]
    return []


@checks.register(checks.Tags.security)
def check_admin_url_path(app_configs, **kwargs):
    # root/urls.py does path(settings.ADMIN_URL_PATH, admin.site.urls) -- an
    # empty string registers the admin urlconf at the site root, shadowing
    # every other URL. env.bool-style "unset falls back to a safe default"
    # doesn't apply here: ADMIN_URL_PATH already has a Python-level default
    # ("the-rack/"), so reaching this check with a falsy value means something
    # upstream (e.g. a secrets store) explicitly supplied an empty string,
    # which is exactly what needs to fail loudly rather than deploy.
    if not settings.ADMIN_URL_PATH:
        return [
            checks.Error(
                "ADMIN_URL_PATH is empty, which serves the Django admin at the "
                "site root instead of a vanity path.",
                hint=(
                    "Set ADMIN_URL_PATH to a non-guessable path ending in '/' "
                    "(e.g. 'the-rack/')."
                ),
                id="core.E002",
            )
        ]
    return []
