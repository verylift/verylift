"""System checks for the accounts app.

Registered from AccountsConfig.ready(). A settings-time assertion would break
any management command that doesn't need OIDC, and middleware would only fail on
a request -- letting a fully misconfigured deployment sit broken for every
request instead of failing at process start. System checks run before migrate,
runserver and collectstatic, so a bad config fails loudly at deploy time.
"""

from django.conf import settings
from django.core import checks

# Deliberately stricter than the login page's `oidc_configured` flag, which only
# checks OIDC_RP_CLIENT_ID: in the two-path world a missing endpoint just breaks
# an optional SSO button, but with OIDC_ONLY_LOGIN on it locks out every user
# with no fallback. There is intentionally no OIDC_OP_LOGOUT_ENDPOINT in this
# project (TASK-242 reverted RP-initiated logout); logout is local-only.
_REQUIRED_OIDC_SETTINGS = [
    "OIDC_RP_CLIENT_ID",
    "OIDC_RP_CLIENT_SECRET",
    "OIDC_OP_AUTHORIZATION_ENDPOINT",
    "OIDC_OP_TOKEN_ENDPOINT",
    "OIDC_OP_USER_ENDPOINT",
    "OIDC_OP_JWKS_ENDPOINT",
]


@checks.register(checks.Tags.security)
def check_oidc_only_login_requires_oidc(app_configs, **kwargs):
    if not getattr(settings, "OIDC_ONLY_LOGIN", False):
        return []

    missing = [
        name for name in _REQUIRED_OIDC_SETTINGS if not getattr(settings, name, "")
    ]
    if not missing:
        return []

    return [
        checks.Error(
            "OIDC_ONLY_LOGIN is enabled but OIDC is not fully configured: "
            f"{', '.join(missing)} must be set.",
            hint=(
                "Set every OIDC_* env var in the SSO section of example.env "
                "before enabling OIDC_ONLY_LOGIN, or leave it False to keep "
                "local login available."
            ),
            id="accounts.E001",
        )
    ]
