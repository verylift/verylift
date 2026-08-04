"""Tests for the rotate_encrypted_fields management command (TASK-289).

Uses accounts.User.liftosaur_api_key as the only current EncryptedCharField
consumer, same as core/tests/test_fields.py.
"""

import io

import pytest
from cryptography.fernet import Fernet, InvalidToken
from django.core.management import call_command
from django.db import connection

from accounts.tests.factories import UserFactory
from core.encryption import decrypt

KEY_OLD = Fernet.generate_key().decode()
KEY_NEW = Fernet.generate_key().decode()


def _raw_column_value(user):
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT liftosaur_api_key FROM accounts_user WHERE id = %s", [user.id]
        )
        return cursor.fetchone()[0]


@pytest.mark.django_db
class TestRotateEncryptedFields:
    def test_rotates_a_row_encrypted_under_a_retired_key(self, settings):
        settings.FIELD_ENCRYPTION_KEYS = [KEY_OLD]
        user = UserFactory(liftosaur_api_key="legacy-key")
        before = _raw_column_value(user)

        settings.FIELD_ENCRYPTION_KEYS = [KEY_NEW, KEY_OLD]
        call_command("rotate_encrypted_fields")

        after = _raw_column_value(user)
        assert after != before
        assert decrypt(after) == "legacy-key"

        # Now stands alone under the new key -- the old key is no longer needed.
        settings.FIELD_ENCRYPTION_KEYS = [KEY_NEW]
        assert decrypt(after) == "legacy-key"

    def test_safe_to_re_run(self, settings):
        settings.FIELD_ENCRYPTION_KEYS = [KEY_OLD]
        user = UserFactory(liftosaur_api_key="re-run-me")

        settings.FIELD_ENCRYPTION_KEYS = [KEY_NEW, KEY_OLD]
        call_command("rotate_encrypted_fields")
        call_command("rotate_encrypted_fields")

        settings.FIELD_ENCRYPTION_KEYS = [KEY_NEW]
        assert decrypt(_raw_column_value(user)) == "re-run-me"

    def test_row_already_on_the_current_key_is_safely_rewritten(self, settings):
        settings.FIELD_ENCRYPTION_KEYS = [KEY_NEW, KEY_OLD]
        user = UserFactory(liftosaur_api_key="already-current")
        before = _raw_column_value(user)

        call_command("rotate_encrypted_fields")

        after = _raw_column_value(user)
        assert after != before
        assert decrypt(after) == "already-current"

    def test_null_and_blank_rows_are_skipped(self, settings):
        settings.FIELD_ENCRYPTION_KEYS = [KEY_NEW, KEY_OLD]
        null_user = UserFactory(liftosaur_api_key=None)
        blank_user = UserFactory(liftosaur_api_key="")

        call_command("rotate_encrypted_fields")

        assert _raw_column_value(null_user) is None
        assert _raw_column_value(blank_user) == ""

    def test_reports_rotated_row_count(self, settings):
        settings.FIELD_ENCRYPTION_KEYS = [KEY_OLD]
        UserFactory(liftosaur_api_key="one")
        UserFactory(liftosaur_api_key="two")
        UserFactory(liftosaur_api_key=None)

        settings.FIELD_ENCRYPTION_KEYS = [KEY_NEW, KEY_OLD]
        out = io.StringIO()
        call_command("rotate_encrypted_fields", stdout=out)

        assert "rotated 2 row(s)" in out.getvalue()
        assert "Rotated 2 row(s) total" in out.getvalue()

    def test_row_under_no_configured_key_raises(self, settings):
        settings.FIELD_ENCRYPTION_KEYS = [KEY_OLD]
        UserFactory(liftosaur_api_key="orphaned")

        # KEY_OLD dropped entirely -- simulates retiring a key before every row
        # was rotated onto the new one.
        settings.FIELD_ENCRYPTION_KEYS = [KEY_NEW]

        with pytest.raises(InvalidToken):
            call_command("rotate_encrypted_fields")
