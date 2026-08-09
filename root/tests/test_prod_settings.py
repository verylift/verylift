"""Production settings checks (TASK-36).

Verifies that root.settings_prod hardens the deployment and that
`manage.py check --deploy` reports no security issues against it.
"""

import importlib

import pytest
from django.core.management import call_command
from django.test.utils import override_settings


@pytest.fixture
def prod_settings(monkeypatch):
    """Import settings_prod with the env vars a real deployment would provide."""
    monkeypatch.setenv(
        "SECRET_KEY",
        "superduperultrasecretkeysuperduperultrasecretkeyok",
    )
    monkeypatch.setenv("ALLOWED_HOSTS", "verylift.example.com")
    monkeypatch.setenv("DATABASE_URL", "postgres://verylift:verylift@db:5432/verylift")
    import root.settings_prod as prod

    return importlib.reload(prod)


class TestProdSettingsValues:
    def test_debug_is_false(self, prod_settings):
        assert prod_settings.DEBUG is False

    def test_allowed_hosts_from_env(self, prod_settings):
        assert prod_settings.ALLOWED_HOSTS == ["verylift.example.com"]

    def test_secure_proxy_ssl_header_set(self, prod_settings):
        assert prod_settings.SECURE_PROXY_SSL_HEADER == (
            "HTTP_X_FORWARDED_PROTO",
            "https",
        )

    def test_secure_cookies(self, prod_settings):
        assert prod_settings.SESSION_COOKIE_SECURE is True
        assert prod_settings.CSRF_COOKIE_SECURE is True

    def test_hsts_configured(self, prod_settings):
        assert prod_settings.SECURE_HSTS_SECONDS > 0

    def test_csrf_trusted_origins_https(self, prod_settings):
        assert prod_settings.CSRF_TRUSTED_ORIGINS == ["https://verylift.example.com"]

    def test_csrf_trusted_origins_wildcard_subdomain(self, monkeypatch):
        """A leading-dot ALLOWED_HOSTS entry must become a *.-prefixed CSRF origin."""
        monkeypatch.setenv(
            "SECRET_KEY",
            "superduperultrasecretkeysuperduperultrasecretkeyok",
        )
        monkeypatch.setenv("ALLOWED_HOSTS", ".domain.ca")
        monkeypatch.setenv(
            "DATABASE_URL", "postgres://verylift:verylift@db:5432/verylift"
        )
        import root.settings_prod as prod

        reloaded = importlib.reload(prod)
        assert reloaded.CSRF_TRUSTED_ORIGINS == ["https://*.domain.ca"]

    def test_https_enabled_by_default(self, prod_settings):
        """Omitting HTTPS_ENABLED must keep the hardened behaviour."""
        assert prod_settings.HTTPS_ENABLED is True

    def test_whitenoise_middleware_after_security(self, prod_settings):
        mw = prod_settings.MIDDLEWARE
        # HealthCheckMiddleware runs first (before SSL redirect / host checks),
        # then WhiteNoise must sit immediately after SecurityMiddleware.
        assert mw[0] == "core.middleware.HealthCheckMiddleware"
        security = mw.index("django.middleware.security.SecurityMiddleware")
        assert mw[security + 1] == "whitenoise.middleware.WhiteNoiseMiddleware"

    def test_whitenoise_middleware_listed_exactly_once(self, prod_settings):
        """Prod must not re-insert WhiteNoise; the base list already has it."""
        whitenoise = "whitenoise.middleware.WhiteNoiseMiddleware"
        assert prod_settings.MIDDLEWARE.count(whitenoise) == 1

    def test_whitenoise_static_storage(self, prod_settings):
        assert (
            prod_settings.STORAGES["staticfiles"]["BACKEND"]
            == "whitenoise.storage.CompressedManifestStaticFilesStorage"
        )


class TestHttpsDisabled:
    """HTTPS_ENABLED=False is the LAN self-hosting path (docs/self-hosting.md).

    Every setting that would make plain HTTP unusable has to relax together --
    leaving any one of them on still breaks the deployment, which is exactly
    the failure this option exists to avoid.
    """

    @pytest.fixture
    def lan_settings(self, monkeypatch):
        monkeypatch.setenv(
            "SECRET_KEY",
            "superduperultrasecretkeysuperduperultrasecretkeyok",
        )
        monkeypatch.setenv("ALLOWED_HOSTS", "verylift.local")
        monkeypatch.setenv(
            "DATABASE_URL", "postgres://verylift:verylift@db:5432/verylift"
        )
        monkeypatch.setenv("HTTPS_ENABLED", "False")
        import root.settings_prod as prod

        return importlib.reload(prod)

    def test_no_ssl_redirect(self, lan_settings):
        """The redirect is what makes the app unreachable over plain HTTP."""
        assert lan_settings.SECURE_SSL_REDIRECT is False

    def test_cookies_not_secure_only(self, lan_settings):
        """Secure cookies are withheld over HTTP, so login could not complete."""
        assert lan_settings.SESSION_COOKIE_SECURE is False
        assert lan_settings.CSRF_COOKIE_SECURE is False

    def test_hsts_fully_disabled(self, lan_settings):
        """A non-zero max-age would outlive this setting in every browser."""
        assert lan_settings.SECURE_HSTS_SECONDS == 0
        assert lan_settings.SECURE_HSTS_INCLUDE_SUBDOMAINS is False
        assert lan_settings.SECURE_HSTS_PRELOAD is False

    def test_csrf_origins_use_http(self, lan_settings):
        assert lan_settings.CSRF_TRUSTED_ORIGINS == ["http://verylift.local"]

    def test_csrf_origins_use_http_for_wildcard_hosts(self, monkeypatch):
        monkeypatch.setenv(
            "SECRET_KEY",
            "superduperultrasecretkeysuperduperultrasecretkeyok",
        )
        monkeypatch.setenv("ALLOWED_HOSTS", ".home.lan")
        monkeypatch.setenv(
            "DATABASE_URL", "postgres://verylift:verylift@db:5432/verylift"
        )
        monkeypatch.setenv("HTTPS_ENABLED", "False")
        import root.settings_prod as prod

        assert importlib.reload(prod).CSRF_TRUSTED_ORIGINS == ["http://*.home.lan"]

    def test_debug_still_forced_off(self, lan_settings):
        """Relaxing transport security must not relax anything else."""
        assert lan_settings.DEBUG is False
        assert lan_settings.SECURE_CONTENT_TYPE_NOSNIFF is True


class TestCollectstaticManifest:
    def test_collectstatic_builds_manifest(self, prod_settings, tmp_path):
        """collectstatic must succeed under the prod manifest storage.

        CompressedManifestStaticFilesStorage post-processes every asset and
        raises on dangling references inside them (e.g. a sourceMappingURL
        comment pointing at a map file that was never vendored) — a failure
        mode dev's plain static storage never exercises, so without this test
        it only surfaces during a production deploy.
        """
        with override_settings(
            STORAGES=prod_settings.STORAGES,
            STATIC_ROOT=tmp_path,
        ):
            call_command("collectstatic", "--noinput", verbosity=0)
        assert (tmp_path / "staticfiles.json").exists()


class TestCheckDeploy:
    def test_check_deploy_passes(self, prod_settings):
        """`manage.py check --deploy` must report no issues with prod settings."""
        overrides = {
            "SECRET_KEY": ("superduperultrasecretkeysuperduperultrasecretkeyok"),
            "DEBUG": prod_settings.DEBUG,
            "ALLOWED_HOSTS": prod_settings.ALLOWED_HOSTS,
            "SECURE_PROXY_SSL_HEADER": prod_settings.SECURE_PROXY_SSL_HEADER,
            "SESSION_COOKIE_SECURE": prod_settings.SESSION_COOKIE_SECURE,
            "CSRF_COOKIE_SECURE": prod_settings.CSRF_COOKIE_SECURE,
            "SECURE_SSL_REDIRECT": prod_settings.SECURE_SSL_REDIRECT,
            "SECURE_HSTS_SECONDS": prod_settings.SECURE_HSTS_SECONDS,
            "SECURE_HSTS_INCLUDE_SUBDOMAINS": (
                prod_settings.SECURE_HSTS_INCLUDE_SUBDOMAINS
            ),
            "SECURE_HSTS_PRELOAD": prod_settings.SECURE_HSTS_PRELOAD,
            "SECURE_CONTENT_TYPE_NOSNIFF": prod_settings.SECURE_CONTENT_TYPE_NOSNIFF,
            "MIDDLEWARE": prod_settings.MIDDLEWARE,
        }
        with override_settings(**overrides):
            # Raises CommandError if any deploy check fails.
            call_command("check", "--deploy", "--fail-level", "WARNING")
