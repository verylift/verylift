"""OIDC settings-resolution tests.

Every OIDC_OP_*_ENDPOINT is an independent, explicitly-set env var -- there is
no base URL these derive from (mozilla-django-oidc has no .well-known
discovery support, so a derived default can never be verified against the
provider's real path scheme, e.g. a non-default Authentik app slug).

Reloading the settings module is inherently brittle; these tests snapshot and
restore os.environ around an importlib.reload and are deliberately kept minimal.
"""

import importlib
import os
from unittest import mock

import pytest

import root.settings as app_settings


@pytest.fixture
def reload_settings():
    """Reload root.settings with a temporary, isolated environment.

    Clears every OIDC_* env var before reloading, then patches
    environ.Env.read_env to a no-op for the duration of the reload --
    otherwise settings.py's read_env(BASE_DIR / ".env", overwrite=False) call
    would refill any cleared key straight back from a real local .env file, or
    leak an operator's real value for a var the test never touches at all
    (e.g. a customized OIDC_PROVIDER_NAME), defeating the point of a
    controlled environment.
    """
    saved_environ = dict(os.environ)

    def _reload(env_overrides):
        for key in list(os.environ):
            if key.startswith("OIDC_"):
                del os.environ[key]
        os.environ.update(env_overrides)
        with mock.patch("environ.Env.read_env"):
            return importlib.reload(app_settings)

    yield _reload

    os.environ.clear()
    os.environ.update(saved_environ)
    importlib.reload(app_settings)


def test_endpoints_default_to_empty_when_unset(reload_settings):
    settings = reload_settings({})
    assert settings.OIDC_OP_TOKEN_ENDPOINT == ""
    assert settings.OIDC_OP_JWKS_ENDPOINT == ""


def test_explicit_endpoint_env_vars_are_read_independently(reload_settings):
    settings = reload_settings(
        {
            "OIDC_OP_TOKEN_ENDPOINT": "https://keycloak.example.com/realms/x/token",
        }
    )
    assert (
        settings.OIDC_OP_TOKEN_ENDPOINT == "https://keycloak.example.com/realms/x/token"
    )
    # Setting one endpoint has no effect on another -- there is no shared base
    # URL to derive from, so an unset endpoint stays unset.
    assert settings.OIDC_OP_JWKS_ENDPOINT == ""


def test_provider_name_defaults_to_generic_sso(reload_settings):
    settings = reload_settings({})
    assert settings.OIDC_PROVIDER_NAME == "SSO"
