"""Tests for core system checks (TASK-285)."""

import pytest
from cryptography.fernet import Fernet
from django.core.management import call_command
from django.core.management.base import SystemCheckError

from core.checks import check_field_encryption_keys


class TestFieldEncryptionKeysCheck:
    def test_passes_with_a_valid_key(self, settings):
        settings.FIELD_ENCRYPTION_KEYS = [Fernet.generate_key().decode()]

        assert check_field_encryption_keys(None) == []

    def test_passes_with_multiple_valid_keys(self, settings):
        settings.FIELD_ENCRYPTION_KEYS = [
            Fernet.generate_key().decode(),
            Fernet.generate_key().decode(),
        ]

        assert check_field_encryption_keys(None) == []

    def test_errors_on_empty_list(self, settings):
        settings.FIELD_ENCRYPTION_KEYS = []

        errors = check_field_encryption_keys(None)

        assert len(errors) == 1
        assert errors[0].id == "core.E001"
        assert "example.env" in errors[0].hint

    def test_errors_on_malformed_key(self, settings):
        settings.FIELD_ENCRYPTION_KEYS = ["not-a-valid-fernet-key"]

        errors = check_field_encryption_keys(None)

        assert len(errors) == 1
        assert errors[0].id == "core.E001"
        assert "entry #1" in errors[0].msg
        # A check message ends up in logs and CI output; it must never carry key
        # material, even a bad key's.
        assert "not-a-valid-fernet-key" not in errors[0].msg

    def test_manage_check_blocks_on_malformed_key(self, settings):
        """End-to-end: the check is registered via ready() and actually blocks."""
        settings.FIELD_ENCRYPTION_KEYS = ["not-a-valid-fernet-key"]

        with pytest.raises(SystemCheckError, match="core.E001"):
            call_command("check")

    def test_manage_check_passes_with_a_valid_key(self, settings):
        settings.FIELD_ENCRYPTION_KEYS = [Fernet.generate_key().decode()]

        call_command("check")
