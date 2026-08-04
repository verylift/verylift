"""Symmetric encryption primitives for encrypted model fields (TASK-285).

Key material comes from ``settings.FIELD_ENCRYPTION_KEYS``: an ordered list of
urlsafe-base64 32-byte Fernet keys. The first key encrypts new values; the rest
are retired keys still accepted for decryption, which is what makes key rotation
possible without a flag-day re-encryption of every row.

The ``MultiFernet`` is built per call and deliberately not cached: construction
is a base64 decode, negligible next to the AES/HMAC work of a single operation,
and a module-level cache would make ``override_settings`` in tests a no-op.
"""

import logging

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

logger = logging.getLogger(__name__)

_GENERATE_HINT = (
    'uv run python -c "from cryptography.fernet import Fernet; '
    'print(Fernet.generate_key().decode())"'
)


def _multifernet() -> MultiFernet:
    keys = getattr(settings, "FIELD_ENCRYPTION_KEYS", None) or []
    if not keys:
        raise ImproperlyConfigured(
            "FIELD_ENCRYPTION_KEYS is empty. Set it to a comma-separated list of "
            "urlsafe-base64 32-byte Fernet keys (see the field-encryption block "
            f"in example.env). Generate one with: {_GENERATE_HINT}"
        )

    fernets = []
    for position, key in enumerate(keys, start=1):
        try:
            fernets.append(Fernet(key))
        except ValueError as exc:  # binascii.Error subclasses ValueError
            # Name the position, never the value -- key material must not reach
            # logs or tracebacks.
            raise ImproperlyConfigured(
                f"FIELD_ENCRYPTION_KEYS entry #{position} is not a valid Fernet "
                "key; expected 32 url-safe base64-encoded bytes (44 characters). "
                f"Generate one with: {_GENERATE_HINT}"
            ) from exc

    return MultiFernet(fernets)


def encrypt(plaintext: str) -> str:
    """Encrypt with the first configured key. Output is non-deterministic."""
    return _multifernet().encrypt(plaintext.encode()).decode()


def decrypt(token: str) -> str:
    """Decrypt with the first configured key that accepts the token."""
    try:
        return _multifernet().decrypt(token.encode()).decode()
    except InvalidToken:
        logger.error(
            "Failed to decrypt an encrypted field value: no FIELD_ENCRYPTION_KEYS "
            "entry matches this ciphertext. A retired key may have been dropped "
            "from the list, or the row predates encryption."
        )
        raise


def rotate(token: str) -> str:
    """Re-encrypt under the first configured key. Used by rotate_encrypted_fields.

    Decrypts under whichever configured key accepts the token (current or
    retired) and re-encrypts under the current one. Unlike decrypt(), which
    input key a token was already under is irrelevant to the output: rotating
    a value already on the current key just produces a fresh, equally valid
    ciphertext, which is what makes re-running the rotation command safe.
    """
    try:
        return _multifernet().rotate(token.encode()).decode()
    except InvalidToken:
        logger.error(
            "Failed to rotate an encrypted field value: no FIELD_ENCRYPTION_KEYS "
            "entry matches this ciphertext. A retired key may have been dropped "
            "from the list before every row was rotated onto the current one."
        )
        raise


def is_encrypted(value: str) -> bool:
    """Whether a stored value decrypts under any configured key.

    Used by the data migration to stay idempotent; it decrypts without logging
    because a legacy plaintext row is the expected, non-exceptional case there.
    """
    try:
        _multifernet().decrypt(value.encode())
    except InvalidToken:
        return False
    return True
