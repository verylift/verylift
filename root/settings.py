from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve().parent.parent

env = environ.Env(
    DEBUG=(bool, False),
    ALLOWED_HOSTS=(list, []),
)

environ.Env.read_env(BASE_DIR / ".env", overwrite=False)

SECRET_KEY = env("SECRET_KEY")

# Keys for core.fields.EncryptedCharField. The first key encrypts new values;
# the remaining ones are retired keys still accepted for decryption during a
# rotation. Deliberately independent of SECRET_KEY so either can be rotated
# without touching the other, and required with no default (like SECRET_KEY) --
# a guessable fallback would defeat the point of a dedicated key. A malformed
# key is reported by the core.E001 system check.
FIELD_ENCRYPTION_KEYS = env.list("FIELD_ENCRYPTION_KEYS")

DEBUG = env("DEBUG")
ALLOWED_HOSTS = env("ALLOWED_HOSTS")

# ALLOWED_HOSTS uses a leading-dot for wildcard subdomains (".domain.ca"), but
# CSRF_TRUSTED_ORIGINS requires the "*.domain.ca" form — translate between them.
# Applies in dev too: manage.py runserver may be reached through a reverse
# tunnel (e.g. Pangolin/Newt) over HTTPS, whose Origin header won't match a
# plain http://127.0.0.1 request unless the tunnel's hostname is listed here.
CSRF_TRUSTED_ORIGINS = [
    f"https://*{host}" if host.startswith(".") else f"https://{host}"
    for host in ALLOWED_HOSTS
]

# A tunnel/reverse proxy terminates TLS and forwards the original scheme via
# X-Forwarded-Proto; trust it so request.is_secure() (and anything built from
# request.build_absolute_uri(), e.g. mozilla-django-oidc's redirect_uri) sees
# https instead of defaulting to http. Applies in dev too, same as
# CSRF_TRUSTED_ORIGINS above -- manage.py runserver reached through a tunnel
# without this would build an http:// OIDC redirect_uri that a provider
# registered with an https:// one rejects. Base-level (not prod-only) since
# both need it and this is a single-operator deployment with no untrusted
# proxy in front of it to spoof the header.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.humanize",
    "mozilla_django_oidc",
    "core",
    "accounts",
    "challenges",
    "liftosaur",
    "fitnessvolt",
    "scoring",
    "notifications",
    "guide",
]

MIDDLEWARE = [
    # First: serves /healthz before SecurityMiddleware's SSL redirect and host
    # validation, so loopback health probes are not 301'd or rejected in prod.
    "core.middleware.HealthCheckMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.locale.LocaleMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "mozilla_django_oidc.middleware.SessionRefresh",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "accounts.middleware.RatelimitMiddleware",
    "accounts.middleware.UserLanguageMiddleware",
    "accounts.middleware.UserTimezoneMiddleware",
]

ROOT_URLCONF = "root.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "django.template.context_processors.i18n",
                "notifications.context_processors.unread_notification_count",
            ],
        },
    },
]

WSGI_APPLICATION = "root.wsgi.application"

DATABASES = {
    # Falls back to a local SQLite file when DATABASE_URL is unset, for hosts
    # that can't run Postgres. Set DATABASE_URL to use Postgres instead.
    "default": env.db("DATABASE_URL", default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}"),
}

# django-ratelimit stores request counters in a cache. Production runs multiple
# gunicorn workers, so a per-process LocMem cache would let each worker keep its
# own count and multiply the effective limit by the worker count. A Postgres
# DatabaseCache (table created by the accounts 0005 migration) is shared across
# all workers, so the limits hold globally. The default cache stays LocMem for
# any incidental framework use.
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
    },
    "ratelimit": {
        "BACKEND": "django.core.cache.backends.db.DatabaseCache",
        "LOCATION": "ratelimit_cache",
    },
}
RATELIMIT_USE_CACHE = "ratelimit"

# Auth-endpoint throttling (TASK-153). Rates use django-ratelimit's
# "<count>/<period>" syntax (s/m/h/d). Behind a reverse proxy the real client
# IP is read from X-Forwarded-For (see accounts.ratelimit.client_ip). Tune these
# per environment via the matching env vars.
RATELIMIT_LOGIN_IP = env("RATELIMIT_LOGIN_IP", default="10/m")
RATELIMIT_LOGIN_USERNAME = env("RATELIMIT_LOGIN_USERNAME", default="5/m")
RATELIMIT_REGISTER_IP = env("RATELIMIT_REGISTER_IP", default="10/h")
RATELIMIT_VALIDATE_KEY_USER = env("RATELIMIT_VALIDATE_KEY_USER", default="10/m")
# Password-reset requests (TASK-283). These are load-bearing for the
# enumeration-safe contract, not just brute-force defence: a real match does
# synchronous SMTP work and so answers measurably slower than a miss, and
# bounding an attacker to a trickle is what makes that residual timing
# difference uninteresting. See accounts.views.password_reset_view.
RATELIMIT_PASSWORD_RESET_IP = env("RATELIMIT_PASSWORD_RESET_IP", default="5/h")
RATELIMIT_PASSWORD_RESET_EMAIL = env("RATELIMIT_PASSWORD_RESET_EMAIL", default="3/h")

_VALIDATORS = "django.contrib.auth.password_validation"
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": f"{_VALIDATORS}.UserAttributeSimilarityValidator"},
    {"NAME": f"{_VALIDATORS}.MinimumLengthValidator"},
    {"NAME": f"{_VALIDATORS}.CommonPasswordValidator"},
    {"NAME": f"{_VALIDATORS}.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en"
LANGUAGES = [
    ("en", "English"),
    ("es", "Español"),
]
LOCALE_PATHS = [BASE_DIR / "locale"]
# Anonymous-page language cookie set by the language switcher (see
# accounts.views.settings_view); keep it long-lived so a signed-out visit on
# the same browser still matches the last chosen language.
LANGUAGE_COOKIE_AGE = 60 * 60 * 24 * 365
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]

# User uploads (currently: profile avatars). Small self-hosted app at low
# scale, so these are served directly by Django (see root/urls.py)
# rather than through a CDN/object-store — unlike STATIC_ROOT, WhiteNoise
# does not serve this since it isn't build-time static content. "Directly by
# Django" does not mean "to anyone": requests go through
# core.views.protected_media_view, which rejects unauthenticated clients
# (TASK-277).
MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

AUTH_USER_MODEL = "accounts.User"

AUTHENTICATION_BACKENDS = [
    "accounts.auth.OIDCBackend",
    "django.contrib.auth.backends.ModelBackend",
]

LOGIN_URL = "/accounts/login/"
LOGIN_REDIRECT_URL = "/dashboard/"
LOGOUT_REDIRECT_URL = "/accounts/login/"

# Serving the admin panel at Django's default admin path is what every
# scanner/bot probes first (TASK-284). The path is env-configurable so it can
# differ per environment; the default below is a vanity name rather than the
# literal default. Trailing slash included so root/urls.py can pass
# this straight to path().
ADMIN_URL_PATH = env("ADMIN_URL_PATH", default="the-rack/")

# Render the styled 403 page on CSRF validation failure instead of Django's default.
CSRF_FAILURE_VIEW = "accounts.views.csrf_failure"

# Base URL for the Liftosaur REST API (override per environment, e.g. a sandbox host).
LIFTOSAUR_API_BASE = env("LIFTOSAUR_API_BASE", default="https://www.liftosaur.com")
# Per-request timeout (seconds) for outbound Liftosaur API calls.
LIFTOSAUR_API_TIMEOUT = env.int("LIFTOSAUR_API_TIMEOUT", default=10)
# Cooldown (minutes) between automatic Liftosaur syncs for a given user+challenge.
LIFTOSAUR_SYNC_COOLDOWN_MINUTES = env.int("LIFTOSAUR_SYNC_COOLDOWN_MINUTES", default=10)

# FitnessVolt strength standards (TASK-104). Enabled by default; the
# FitnessVolt options still never appear in the challenge-creation picker
# until `manage.py refresh_fitnessvolt_cache` has been run once to warm the
# first snapshot -- set FITNESSVOLT_ENABLED=False to keep the picker from
# offering it at all instead.
FITNESSVOLT_ENABLED = env.bool("FITNESSVOLT_ENABLED", default=True)
FITNESSVOLT_API_BASE = env("FITNESSVOLT_API_BASE", default="https://fitnessvolt.com")
# Per-request timeout (seconds) for outbound FitnessVolt API calls.
FITNESSVOLT_API_TIMEOUT = env.int("FITNESSVOLT_API_TIMEOUT", default=10)
# Grace window before an old, challenge-unreferenced FitnessVolt snapshot is
# swept during a new-snapshot refresh.
FITNESSVOLT_SNAPSHOT_RETENTION_MONTHS = env.int(
    "FITNESSVOLT_SNAPSHOT_RETENTION_MONTHS", default=3
)

# "Close to goal" highlight on the athlete's personal-performance view (TASK-202).
# An unscored lift is flagged when its raw-kg gap to the first point is within
# CHALLENGES_CLOSE_TO_GOAL_GAP_FRACTION of the rep-adjusted threshold, OR the
# athlete is within CHALLENGES_CLOSE_TO_GOAL_REPS_GAP additional reps at the
# current weight. Both are tunable per environment without a code change.
CHALLENGES_CLOSE_TO_GOAL_GAP_FRACTION = env.float(
    "CHALLENGES_CLOSE_TO_GOAL_GAP_FRACTION", default=0.05
)
CHALLENGES_CLOSE_TO_GOAL_REPS_GAP = env.int(
    "CHALLENGES_CLOSE_TO_GOAL_REPS_GAP", default=2
)

# "Endgame" point-gap suggestion in the final stretch of a challenge (TASK-212).
# CHALLENGES_ENDGAME_WINDOW_DAYS sets how many days before Challenge.end_date
# the single motivational point-gap suggestion may appear. The two achievability
# thresholds are kept deliberately separate from the close-to-goal pair above so
# the two features can diverge without one silently retuning the other: a lift
# qualifies when its raw-kg gap is within CHALLENGES_ENDGAME_GAP_FRACTION of the
# rep-adjusted threshold, OR (unscored lifts only) it is within
# CHALLENGES_ENDGAME_REPS_GAP additional reps at the current weight.
CHALLENGES_ENDGAME_WINDOW_DAYS = env.int("CHALLENGES_ENDGAME_WINDOW_DAYS", default=14)
CHALLENGES_ENDGAME_GAP_FRACTION = env.float(
    "CHALLENGES_ENDGAME_GAP_FRACTION", default=0.05
)
CHALLENGES_ENDGAME_REPS_GAP = env.int("CHALLENGES_ENDGAME_REPS_GAP", default=2)

# Goal-setup "suggested from history" method (TASK-248): a lift's suggested
# rep-max ladder is the lifter's best recent e1RM, uplifted by this fraction,
# then expanded via threshold_for_reps. CHALLENGES_GOAL_SUGGESTION_LOOKBACK_DAYS
# matches the existing "Recent Results" window (challenges.services).
CHALLENGES_GOAL_SUGGESTION_UPLIFT = env.float(
    "CHALLENGES_GOAL_SUGGESTION_UPLIFT", default=0.10
)
CHALLENGES_GOAL_SUGGESTION_LOOKBACK_DAYS = env.int(
    "CHALLENGES_GOAL_SUGGESTION_LOOKBACK_DAYS", default=182
)

# Shareable challenge invite link lifetime (TASK-249, AC#1). A challenge's
# single live link expires this many days after it was (re)generated; the
# owner can regenerate at any time, which revokes the incumbent.
CHALLENGES_INVITE_LINK_TTL_DAYS = env.int("CHALLENGES_INVITE_LINK_TTL_DAYS", default=7)

# Generic OIDC relying-party config. Any spec-compliant provider (Authentik,
# Keycloak, Auth0, Okta, ...) is configured the same way: set the client
# credentials and all five endpoint URLs explicitly. mozilla-django-oidc has no
# .well-known discovery support, so there is no base URL to derive endpoints
# from -- every endpoint is its own setting, full stop. Leaving OIDC_CLIENT_ID
# empty hides the SSO login button and local login stays available.
OIDC_RP_CLIENT_ID = env("OIDC_CLIENT_ID", default="")
OIDC_RP_CLIENT_SECRET = env("OIDC_CLIENT_SECRET", default="")
OIDC_RP_SIGN_ALGO = "RS256"
OIDC_OP_JWKS_ENDPOINT = env("OIDC_OP_JWKS_ENDPOINT", default="")
OIDC_OP_AUTHORIZATION_ENDPOINT = env("OIDC_OP_AUTHORIZATION_ENDPOINT", default="")
OIDC_OP_TOKEN_ENDPOINT = env("OIDC_OP_TOKEN_ENDPOINT", default="")
OIDC_OP_USER_ENDPOINT = env("OIDC_OP_USER_ENDPOINT", default="")
OIDC_OP_LOGOUT_ENDPOINT = env("OIDC_OP_LOGOUT_ENDPOINT", default="")

# Label shown on the SSO login button ("Sign in with <name>"). Defaults to a
# provider-agnostic "SSO" -- set this to your actual provider's name
# (Authentik, Keycloak, Okta, ...) for a nicer button label.
OIDC_PROVIDER_NAME = env("OIDC_PROVIDER_NAME", default="SSO")

OIDC_STORE_ID_TOKEN = True
OIDC_USERNAME_ALGO = "accounts.auth.generate_username"

# Build the RP-initiated end-session redirect (id_token_hint + client_id) so
# logout terminates the provider's SSO session, not just the local Django
# session. See accounts.auth.build_oidc_logout_url.
#
# TASK-270: re-enabled after TASK-242's revert. The provider's own
# invalidation flow redirects back to a STATIC target configured on the
# Authentik side -- Authentik 2026.5.6 has no stage that can honor a dynamic
# post_logout_redirect_uri. This app deliberately does NOT send
# post_logout_redirect_uri: live testing found that including it makes
# Authentik reject the whole end-session request as malformed (400), not
# merely ignore it.
OIDC_OP_LOGOUT_URL_METHOD = "accounts.auth.build_oidc_logout_url"

# Swaps in accounts.auth.OIDCCallbackView so a first-time OIDC login rejected by
# OIDCBackend.create_user (registration closed, no qualifying group) lands on a
# clear message instead of mozilla-django-oidc's default silent redirect.
OIDC_CALLBACK_CLASS = "accounts.auth.OIDCCallbackView"

# Closing self-serve registration (REGISTRATION_OPEN=False) also gates first-time
# OIDC account creation in accounts.auth.OIDCBackend.create_user, EXCEPT for a
# first-time OIDC login whose "groups" claim contains OIDC_AUTO_ENROLL_GROUP.
# Existing accounts (matched by oidc_sub, or local username/password) can always
# log in regardless of either setting -- only brand-new account creation is
# gated. Requires the "profile" scope to actually be requested (see
# OIDC_RP_SCOPES below) and configured to expose a "groups" claim on your
# provider's side (e.g. Authentik's Provider scope config).
REGISTRATION_OPEN = env.bool("REGISTRATION_OPEN", default=True)
OIDC_AUTO_ENROLL_GROUP = env("OIDC_AUTO_ENROLL_GROUP", default="")

# Independent of OIDC_AUTO_ENROLL_GROUP above: on every OIDC login (not just
# first-time account creation), accounts.auth.OIDCBackend grants full Django
# admin access (is_staff AND is_superuser -- bypasses all permission checks)
# when the "groups" claim contains this group, and revokes both when
# membership is absent -- your OIDC provider becomes the source of truth for
# admin status among OIDC-authenticated users once this is set. Empty/unset means
# the feature is off and OIDC login never touches either flag. Only affects
# accounts that log in via OIDC; local password-only accounts are never
# touched by this.
OIDC_ADMIN_GROUP = env("OIDC_ADMIN_GROUP", default="")

# mozilla-django-oidc defaults OIDC_RP_SCOPES to "openid email" -- neither the
# "groups" claim the two settings above need, nor "preferred_username"/"name"
# (used for display_name in accounts.auth.OIDCBackend), are ever requested
# without "profile". Authentik's *default* OpenID 'profile' Scope Mapping
# bundles "groups" in alongside the standard profile claims (its own platform
# convention, not part of the OIDC spec -- see
# https://github.com/goauthentik/authentik/blob/main/blueprints/system/providers-oauth2.yaml,
# the "profile" mapping's expression includes
# "groups": [group.name for group in request.user.groups.all()]), so
# requesting "profile" is sufficient for a stock Authentik deployment -- no
# separate custom Property Mapping is needed for OIDC_AUTO_ENROLL_GROUP/
# OIDC_ADMIN_GROUP to work, only the Provider having 'profile' in its selected
# scopes (Authentik's default). A deployment that has replaced Authentik's
# default profile mapping, or a non-Authentik provider that scopes "groups"
# differently, can override this env var directly.
OIDC_RP_SCOPES = env("OIDC_RP_SCOPES", default="openid email profile")

# OIDC-only login mode: when True, accounts.views.LocalLoginView skips rendering
# the local username/password form entirely and redirects visitors straight into
# the OIDC authorization flow -- on POST as well as GET, so hiding the form
# isn't bypassable by posting credentials directly. Also force-closes self-serve
# local registration (accounts:register) regardless of REGISTRATION_OPEN, since
# a local account created while local login is hidden could never be used to
# sign in. AUTHENTICATION_BACKENDS is deliberately untouched, which is what
# keeps the admin panel's own password login working as a break-glass path
# when the provider is unreachable. Existing local-password accounts are not
# modified, but can no longer sign in through the app UI once this is enabled.
# Guarded by a system check (accounts/checks.py, accounts.E001) that fails
# startup if this is enabled without OIDC fully configured.
OIDC_ONLY_LOGIN = env.bool("OIDC_ONLY_LOGIN", default=False)

# Outbound email (TASK-283). Only used for password-reset links today; the app
# speaks generic SMTP, so any relay that accepts DEFAULT_FROM_EMAIL as a sender
# works.
EMAIL_HOST = env("EMAIL_HOST", default="")
EMAIL_PORT = env.int("EMAIL_PORT", default=587)
EMAIL_HOST_USER = env("EMAIL_HOST_USER", default="")
EMAIL_HOST_PASSWORD = env("EMAIL_HOST_PASSWORD", default="")
# EMAIL_USE_TLS is STARTTLS on 587; EMAIL_USE_SSL is implicit TLS on 465. Set
# one or the other -- Django refuses to start with both enabled. SSL is beyond
# the vars the task named, but without it a 465-only relay is simply unusable.
EMAIL_USE_TLS = env.bool("EMAIL_USE_TLS", default=True)
EMAIL_USE_SSL = env.bool("EMAIL_USE_SSL", default=False)
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", default="very lift <noreply@localhost>")
# Django's own default is None, i.e. an SMTP connect can hang a gunicorn worker
# indefinitely on a synchronous request path. Bounded like the outbound API
# timeouts above.
EMAIL_TIMEOUT = env.int("EMAIL_TIMEOUT", default=10)
# Unconfigured must not mean broken: Django's stock default is the SMTP backend
# pointed at localhost:25, so a dev box or an unrelayed deployment would raise
# ConnectionRefusedError mid-request. Falling back to the console backend puts
# reset links in the container log instead, which keeps the flow usable (and
# means the views never need an "email is unavailable" branch).
EMAIL_BACKEND = env(
    "EMAIL_BACKEND",
    default=(
        "django.core.mail.backends.smtp.EmailBackend"
        if EMAIL_HOST
        else "django.core.mail.backends.console.EmailBackend"
    ),
)
# How long an emailed password-reset link stays valid (seconds). Deliberately
# one hour rather than Django's three-day default: this is a self-hosted app
# with no recovery support desk, so a link sitting live in an inbox for days is
# the bigger risk. Env-tunable if an operator wants it looser.
PASSWORD_RESET_TIMEOUT = env.int("PASSWORD_RESET_TIMEOUT", default=3600)

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "simple": {
            "format": "%(levelname)s %(name)s %(message)s",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "stream": "ext://sys.stdout",
            "formatter": "simple",
        },
    },
    "root": {
        "handlers": ["console"],
        "level": "WARNING",
    },
    "loggers": {
        "core": {"level": "DEBUG" if DEBUG else "INFO", "propagate": True},
        "accounts": {"level": "DEBUG" if DEBUG else "WARNING", "propagate": True},
        "challenges": {"level": "DEBUG" if DEBUG else "WARNING", "propagate": True},
        "liftosaur": {"level": "DEBUG" if DEBUG else "WARNING", "propagate": True},
        "scoring": {"level": "DEBUG" if DEBUG else "WARNING", "propagate": True},
        "notifications": {"level": "DEBUG" if DEBUG else "WARNING", "propagate": True},
    },
}
