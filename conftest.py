"""Project-wide pytest configuration."""

import time

import pytest
from django.core.management import call_command
from django.test import Client

from accounts.timezones import DETECT_COOKIE_NAME

_OIDC_TOKEN_LIFETIME_SECONDS = 3600


@pytest.fixture(autouse=True)
def _disable_ratelimit(settings):
    """Keep auth-endpoint throttling off for the general suite.

    The existing login/registration/key-validation tests deliberately POST the
    same endpoint many times; leaving django-ratelimit enabled would start
    tripping the limits and fail them. Tests that specifically exercise the
    throttling re-enable it by setting ``settings.RATELIMIT_ENABLE = True``.
    """
    settings.RATELIMIT_ENABLE = False


@pytest.fixture(autouse=True)
def _registration_open_by_default(settings):
    """Default the test suite to registration-open behavior.

    REGISTRATION_OPEN (TASK-232) is read from the environment/.env like any
    other setting -- an operator's local .env (e.g. closing registration to
    test the closed-registration gate manually) shouldn't change what the rest
    of the suite exercises. Tests that specifically test the closed-registration
    gate (accounts/tests/test_auth.py::TestOIDCBackendRegistrationGate,
    accounts/tests/test_registration_view.py's closed-state tests) override
    this explicitly, same pattern as _disable_ratelimit above.
    """
    settings.REGISTRATION_OPEN = True


@pytest.fixture(autouse=True)
def _oidc_only_login_off_by_default(settings):
    """Default the test suite to local-login-available behavior.

    Same reasoning as _registration_open_by_default above: OIDC_ONLY_LOGIN
    (TASK-233) is read from the environment/.env, and an operator who has
    enabled it locally would otherwise see every local-login and registration
    test redirect to the OIDC provider. The tests that exercise OIDC-only mode
    (accounts/tests/test_login_view.py::TestOIDCOnlyLoginMode and the
    OIDC-only registration test) set it to True explicitly.
    """
    settings.OIDC_ONLY_LOGIN = False


@pytest.fixture(autouse=True)
def _valid_oidc_session(monkeypatch):
    """Stamp force_login() sessions with a valid OIDC id-token expiration.

    SessionRefresh (mozilla-django-oidc) does not make any HTTP calls: on every
    authenticated GET it reads ``oidc_id_token_expiration`` from the session and
    redirects to the provider when the token has expired. force_login() never
    sets that value, so without this fixture the full middleware stack would 302
    every authenticated request to the OIDC provider. Stamping a future
    expiration mirrors a freshly-authenticated user and lets the real middleware
    run instead of being stripped from MIDDLEWARE.
    """
    original_force_login = Client.force_login

    def force_login(self, user, backend=None):
        original_force_login(self, user, backend=backend)
        session = self.session
        session["oidc_id_token_expiration"] = time.time() + _OIDC_TOKEN_LIFETIME_SECONDS
        session.save()

    monkeypatch.setattr(Client, "force_login", force_login)


@pytest.fixture(autouse=True)
def _consent_to_active_policies_on_login(monkeypatch):
    """Auto-grant consent to every active gated PolicyVersion on force_login().

    PolicyConsentMiddleware (policies app) redirects any authenticated request
    to /policies/consent/ until the user has consented to every active, gated
    PolicyVersion. Real deployments grandfather already-existing users in via
    a one-off data migration (policies/migrations/0002_...), but that only
    covers whoever existed when it ran -- every ``UserFactory``-made test user
    is "new" from the middleware's perspective, so without this fixture the
    entire suite's view tests would 302 to the consent page instead of
    exercising the page they meant to test. Mirrors ``_valid_oidc_session``
    above: patch force_login() rather than push this orthogonal concern onto
    every other app's tests. policies/tests overrides this fixture with a
    no-op (see policies/tests/conftest.py) since those tests deliberately
    control consent state themselves.
    """
    from policies.models import PolicyConsent, PolicyVersion

    original_force_login = Client.force_login

    def force_login(self, user, backend=None):
        original_force_login(self, user, backend=backend)
        PolicyConsent.objects.bulk_create(
            [
                PolicyConsent(
                    user=user,
                    policy_version=version,
                    method=PolicyConsent.Method.ADMIN_OVERRIDE,
                )
                for version in PolicyVersion.objects.active_gated()
            ],
            ignore_conflicts=True,
        )

    monkeypatch.setattr(Client, "force_login", force_login)


@pytest.fixture(autouse=True)
def _skip_timezone_detection(monkeypatch):
    """Seed the tzdetect cookie on every test Client (TASK-273).

    UserTimezoneMiddleware detours a cookie-less HTML GET through
    accounts:timezone-detect when it can't resolve a timezone. Most existing
    view tests are a bare ``client.get(...)`` expecting a normal 200/302 for
    that view, not this detour, so every Client is seeded with the
    "detection already offered" marker cookie by default. Seeding
    ``pp_tzdetect`` rather than ``pp_timezone`` only disables the detour --
    it doesn't pin a timezone, so requests still resolve to UTC and existing
    timestamp assertions elsewhere in the suite stay valid. Tests that
    specifically exercise the detection redirect (test_middleware.py's
    TestTimezoneDetectionRedirect) remove this cookie explicitly.
    """
    original_init = Client.__init__

    def init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        self.cookies[DETECT_COOKIE_NAME] = "1"

    monkeypatch.setattr(Client, "__init__", init)


@pytest.fixture(scope="session")
def django_db_setup(django_db_setup, django_db_blocker):
    """Seed the Lift/LiftAlias reference tables once per test session.

    Lift qualities (Liftosaur built-in, bodyweight-added) and name aliases live
    in seeded DB rows rather than code constants; production seeds them with
    the seed_liftosaur_lifts management command. Seeding here mirrors that so
    tests exercise the same reference data a deployed instance has.
    """
    with django_db_blocker.unblock():
        call_command("seed_liftosaur_lifts")
        call_command("seed_fitnessvolt_lifts")
