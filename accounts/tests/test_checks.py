"""Tests for accounts system checks (TASK-233).

django.core.checks reads live settings via django.conf.settings, so
pytest-django's `settings` fixture is enough -- no settings-module reload.
"""

import pytest
from django.core.management import call_command
from django.core.management.base import SystemCheckError

from accounts.checks import (
    _REQUIRED_OIDC_SETTINGS,
    check_oidc_only_login_requires_oidc,
)

_FULLY_CONFIGURED = {
    "OIDC_RP_CLIENT_ID": "client-id",
    "OIDC_RP_CLIENT_SECRET": "client-secret",
    "OIDC_OP_AUTHORIZATION_ENDPOINT": "https://idp.example/authorize",
    "OIDC_OP_TOKEN_ENDPOINT": "https://idp.example/token",
    "OIDC_OP_USER_ENDPOINT": "https://idp.example/userinfo",
    "OIDC_OP_JWKS_ENDPOINT": "https://idp.example/jwks",
}


@pytest.fixture
def oidc_configured(settings):
    for name, value in _FULLY_CONFIGURED.items():
        setattr(settings, name, value)
    return settings


class TestOIDCOnlyLoginCheck:
    def test_required_settings_all_exist_on_settings(self, settings):
        # A required name that no longer exists in settings.py would make the
        # check fire unconditionally and the flag impossible to enable --
        # OIDC_OP_LOGOUT_ENDPOINT was removed in TASK-242 for exactly this
        # reason and must stay out of the list.
        for name in _REQUIRED_OIDC_SETTINGS:
            assert hasattr(settings, name)

    def test_passes_when_fully_configured(self, oidc_configured):
        oidc_configured.OIDC_ONLY_LOGIN = True
        assert check_oidc_only_login_requires_oidc(None) == []

    def test_errors_when_one_setting_is_blank(self, oidc_configured):
        oidc_configured.OIDC_ONLY_LOGIN = True
        oidc_configured.OIDC_OP_JWKS_ENDPOINT = ""

        errors = check_oidc_only_login_requires_oidc(None)

        assert len(errors) == 1
        assert errors[0].id == "accounts.E001"
        assert "OIDC_OP_JWKS_ENDPOINT" in errors[0].msg
        assert "example.env" in errors[0].hint

    def test_error_lists_every_missing_setting(self, settings):
        settings.OIDC_ONLY_LOGIN = True
        for name in _REQUIRED_OIDC_SETTINGS:
            setattr(settings, name, "")

        errors = check_oidc_only_login_requires_oidc(None)

        assert len(errors) == 1
        for name in _REQUIRED_OIDC_SETTINGS:
            assert name in errors[0].msg

    def test_no_op_when_flag_is_off(self, settings):
        settings.OIDC_ONLY_LOGIN = False
        for name in _REQUIRED_OIDC_SETTINGS:
            setattr(settings, name, "")

        assert check_oidc_only_login_requires_oidc(None) == []

    def test_manage_check_blocks_on_bad_config(self, settings):
        """End-to-end: the check is registered via ready() and actually blocks."""
        settings.OIDC_ONLY_LOGIN = True
        for name in _REQUIRED_OIDC_SETTINGS:
            setattr(settings, name, "")

        with pytest.raises(SystemCheckError, match="accounts.E001"):
            call_command("check")

    def test_manage_check_passes_when_fully_configured(self, oidc_configured):
        oidc_configured.OIDC_ONLY_LOGIN = True
        call_command("check")
