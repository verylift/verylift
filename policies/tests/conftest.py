import pytest

from policies.models import Policy


@pytest.fixture(autouse=True)
def _consent_to_active_policies_on_login():
    """Disable the root conftest's auto-consent shortcut within this package.

    These tests are the ones actually exercising consent/gating behavior, so
    they need to control PolicyConsent state themselves rather than have
    every force_login() silently pre-consent the user.
    """
    yield


@pytest.fixture(autouse=True)
def _clear_seeded_policies(db):
    """Start every policies test from a clean slate.

    The 0002 grandfather-consent migration seeds a real, active, gated TOS and
    Privacy Policy version once against the shared test database (see
    test_migrations.py's module docstring). Without this, every test in this
    package that logs a fresh user in and hits any non-exempt URL would get
    redirected to /policies/consent/ for those two pre-existing versions,
    regardless of what the test itself set up.
    """
    Policy.objects.all().delete()
