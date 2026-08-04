"""Tests for core.encryption (TASK-285).

Every test pins FIELD_ENCRYPTION_KEYS through the ``settings`` fixture rather
than relying on the ambient .env value -- the keys are env-configured, and these
assertions are about behaviour under a known key list, not about whichever key
the local environment happens to carry.
"""

import pytest
from cryptography.fernet import Fernet, InvalidToken
from django.core.exceptions import ImproperlyConfigured

from core.encryption import decrypt, encrypt, is_encrypted, rotate

KEY_A = Fernet.generate_key().decode()
KEY_B = Fernet.generate_key().decode()


@pytest.fixture
def key_a(settings):
    settings.FIELD_ENCRYPTION_KEYS = [KEY_A]
    return settings


class TestRoundTrip:
    def test_decrypt_reverses_encrypt(self, key_a):
        assert decrypt(encrypt("liftosaur-secret")) == "liftosaur-secret"

    def test_ciphertext_does_not_contain_plaintext(self, key_a):
        token = encrypt("liftosaur-secret")

        assert "liftosaur-secret" not in token

    def test_encryption_is_non_deterministic(self, key_a):
        # Random IV plus a timestamp, which is why the field refuses value
        # lookups: equal plaintexts do not produce equal ciphertexts.
        assert encrypt("same") != encrypt("same")

    def test_unicode_round_trips(self, key_a):
        assert decrypt(encrypt("clé-ünicode-🔑")) == "clé-ünicode-🔑"


class TestKeyRotation:
    def test_value_encrypted_under_retired_key_still_decrypts(self, settings):
        settings.FIELD_ENCRYPTION_KEYS = [KEY_A]
        token = encrypt("rotate-me")

        # New key prepended, old key retained: this is the rotation contract.
        settings.FIELD_ENCRYPTION_KEYS = [KEY_B, KEY_A]

        assert decrypt(token) == "rotate-me"

    def test_new_writes_use_the_first_key(self, settings):
        settings.FIELD_ENCRYPTION_KEYS = [KEY_B, KEY_A]
        token = encrypt("new-write")

        settings.FIELD_ENCRYPTION_KEYS = [KEY_B]

        assert decrypt(token) == "new-write"

    def test_dropping_the_key_that_encrypted_a_value_breaks_it(self, settings):
        settings.FIELD_ENCRYPTION_KEYS = [KEY_A]
        token = encrypt("orphaned")

        settings.FIELD_ENCRYPTION_KEYS = [KEY_B]

        with pytest.raises(InvalidToken):
            decrypt(token)


class TestRotate:
    def test_rotates_a_retired_key_token_onto_the_current_key(self, settings):
        settings.FIELD_ENCRYPTION_KEYS = [KEY_A]
        token = encrypt("rotate-me")

        settings.FIELD_ENCRYPTION_KEYS = [KEY_B, KEY_A]
        rotated = rotate(token)

        # Rotated ciphertext must now stand alone under the new current key.
        settings.FIELD_ENCRYPTION_KEYS = [KEY_B]
        assert decrypt(rotated) == "rotate-me"

    def test_rotating_a_current_key_token_is_a_safe_no_op(self, key_a):
        token = encrypt("already-current")

        rotated = rotate(token)

        assert decrypt(rotated) == "already-current"

    def test_rotate_is_re_runnable(self, settings):
        settings.FIELD_ENCRYPTION_KEYS = [KEY_A]
        token = encrypt("re-run-me")

        settings.FIELD_ENCRYPTION_KEYS = [KEY_B, KEY_A]
        once = rotate(token)
        twice = rotate(once)

        settings.FIELD_ENCRYPTION_KEYS = [KEY_B]
        assert decrypt(twice) == "re-run-me"

    def test_rotate_raises_on_a_token_under_no_configured_key(self, settings):
        settings.FIELD_ENCRYPTION_KEYS = [KEY_A]
        token = encrypt("orphaned")

        settings.FIELD_ENCRYPTION_KEYS = [KEY_B]

        with pytest.raises(InvalidToken):
            rotate(token)


class TestInvalidInput:
    def test_decrypt_raises_on_non_token(self, key_a):
        with pytest.raises(InvalidToken):
            decrypt("not-a-fernet-token")

    def test_is_encrypted_true_for_own_ciphertext(self, key_a):
        assert is_encrypted(encrypt("value")) is True

    def test_is_encrypted_false_for_plaintext(self, key_a):
        assert is_encrypted("plain-liftosaur-key") is False


class TestMisconfiguration:
    def test_empty_key_list_is_improperly_configured(self, settings):
        settings.FIELD_ENCRYPTION_KEYS = []

        with pytest.raises(ImproperlyConfigured, match="FIELD_ENCRYPTION_KEYS"):
            encrypt("value")

    def test_malformed_key_names_its_position_but_not_its_value(self, settings):
        settings.FIELD_ENCRYPTION_KEYS = [KEY_A, "too-short"]

        with pytest.raises(ImproperlyConfigured, match="entry #2") as exc_info:
            encrypt("value")

        assert "too-short" not in str(exc_info.value)
