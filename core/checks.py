"""System checks for the core app.

Registered from CoreConfig.ready(). An *absent* FIELD_ENCRYPTION_KEYS already
fails at settings import, so this check exists for the *malformed* case: a
truncated or non-base64 key otherwise sits unnoticed until the first user saves
an encrypted value. System checks run before migrate, runserver and
collectstatic, so a bad key fails loudly at deploy time instead.
"""

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
