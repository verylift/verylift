"""Generic OIDC authentication backend for very lift.

Works with any spec-compliant OIDC provider (tested against Authentik, also
compatible with Keycloak, Auth0, Okta, etc.). Provider-specific details live
entirely in settings/env vars, not here.
"""

import logging
from urllib.parse import urlencode

import requests
from django.conf import settings
from django.shortcuts import redirect, resolve_url
from mozilla_django_oidc.auth import OIDCAuthenticationBackend
from mozilla_django_oidc.views import OIDCAuthenticationCallbackView

logger = logging.getLogger(__name__)


def generate_username(email):
    """Derive a username from the OIDC email claim."""
    return email.split("@")[0] if email else "user"


def build_oidc_logout_url(request):
    """Build the RP-initiated end-session redirect for OIDC_OP_LOGOUT_URL_METHOD.

    Called by OIDCLogoutView.post() before auth.logout(request), so
    request.session still has whatever store_tokens() stashed under
    "oidc_id_token" (only present when OIDC_STORE_ID_TOKEN=True). Falls back to
    the plain local logout redirect when no id_token is available, covering both
    local-only users and any OIDC session missing a usable token.

    Deliberately omits post_logout_redirect_uri: TASK-270 found live that
    sending it makes Authentik 2026.5.6 reject the whole end-session request
    as malformed (400), not merely fail to honor it as TASK-242 assumed --
    the provider's own invalidation flow already carries a static redirect
    target configured on the provider's side, so this app has no need to
    request one anyway.
    """
    local_logout_url = resolve_url(settings.LOGOUT_REDIRECT_URL)
    id_token = request.session.get("oidc_id_token")
    if not id_token:
        logger.debug(
            "OIDC logout: no stored id_token, falling back to local-only logout"
        )
        return local_logout_url

    params = {
        "id_token_hint": id_token,
        "client_id": settings.OIDC_RP_CLIENT_ID,
    }
    logger.info(
        "OIDC logout: redirecting user %s to provider end-session endpoint",
        request.user.pk,
    )
    return f"{settings.OIDC_OP_LOGOUT_ENDPOINT}?{urlencode(params)}"


class OIDCBackend(OIDCAuthenticationBackend):
    """OIDC backend that creates/updates local User records from OIDC claims."""

    # mozilla_django_oidc's get_userinfo calls response.raise_for_status(),
    # which discards the response body -- so a provider-side error (e.g. a
    # WWW-Authenticate header naming the real cause) is otherwise invisible.
    def get_userinfo(self, access_token, id_token, payload):
        try:
            return super().get_userinfo(access_token, id_token, payload)
        except requests.exceptions.HTTPError as exc:
            resp = exc.response
            logger.debug(
                "OIDC userinfo request failed: status=%s headers=%s body=%s",
                resp.status_code if resp is not None else None,
                dict(resp.headers) if resp is not None else None,
                resp.text if resp is not None else None,
            )
            raise

    def create_user(self, claims):
        # Local import: accounts.views imports from challenges.services, and
        # AUTHENTICATION_BACKENDS/OIDC_CALLBACK_CLASS reference this module by
        # string path, so importing at call time (rather than module import
        # time) sidesteps any question of import ordering at Django startup.
        from accounts.views import _invite_token_link

        request = getattr(self, "request", None)
        invite_link = _invite_token_link(request)

        # A usable challenge invite-link token doubles as a beta invite
        # (TASK-249): it bypasses the closed-registration refusal below the
        # same way it bypasses REGISTRATION_OPEN for local signup
        # (accounts.views.register_view). Deliberately no OIDC_ONLY_LOGIN term
        # here -- that mode's unconditional force-close is handled entirely
        # by LocalLoginView/register_view, not this backend.
        if (
            not settings.REGISTRATION_OPEN
            and not self._has_auto_enroll_group(claims)
            and invite_link is None
        ):
            logger.warning(
                "Rejected first-time OIDC account creation for %s: "
                "registration is closed and no qualifying group was present",
                claims.get("email", "unknown"),
            )
            if request is not None:
                request.oidc_registration_closed = True
            return None

        email = claims.get("email", "")
        username = self.get_username(claims)
        is_admin = self._is_admin_group_member(claims)
        acquisition_source = (
            self.UserModel.AcquisitionSource.INVITE_LINK
            if invite_link
            else self.UserModel.AcquisitionSource.OIDC
        )
        user = self.UserModel.objects.create_user(
            username=username,
            email=email,
            is_staff=is_admin,
            is_superuser=is_admin,
            acquisition_source=acquisition_source,
        )
        user.oidc_sub = claims.get("sub", "")
        user.display_name = claims.get("preferred_username", "") or claims.get(
            "name", ""
        )
        user.save(update_fields=["oidc_sub", "display_name"])
        logger.info(
            "New OIDC account registered: user %s (acquisition_source=%s)",
            user.id,
            acquisition_source,
        )
        if request is not None:
            request.oidc_user_just_created = True
        return user

    def _has_auto_enroll_group(self, claims):
        """Grant-based exception to a closed registration flag for OIDC signups.

        Membership is read from a "groups" claim. On a stock Authentik
        deployment this rides along with the "profile" scope (see
        OIDC_RP_SCOPES in settings.py) -- no separate claim/scope config is
        needed there, just "profile" selected on the Provider. Other
        providers may need the "groups" claim exposed explicitly. Empty/unset
        OIDC_AUTO_ENROLL_GROUP means no exception is configured at all.
        """
        required_group = settings.OIDC_AUTO_ENROLL_GROUP
        if not required_group:
            return False
        return required_group in (claims.get("groups") or [])

    def _is_admin_group_member(self, claims):
        """Whether this login's "groups" claim contains OIDC_ADMIN_GROUP.

        Empty/unset OIDC_ADMIN_GROUP means the feature is off -- is_staff/
        is_superuser are never touched by OIDC login at all in that case (see
        update_user, which skips both fields entirely rather than calling this
        and saving False over manually-granted local admin flags).
        """
        admin_group = settings.OIDC_ADMIN_GROUP
        if not admin_group:
            return False
        return admin_group in (claims.get("groups") or [])

    def update_user(self, user, claims):
        user.email = claims.get("email", user.email)
        user.display_name = claims.get("preferred_username", "") or claims.get(
            "name", user.display_name
        )
        update_fields = ["email", "display_name"]

        if settings.OIDC_ADMIN_GROUP:
            groups = claims.get("groups")
            if groups is None:
                # The claim key is entirely absent, not just empty -- almost
                # certainly the provider-side claim/scope mapping prerequisite
                # isn't set up yet (e.g. Authentik's Property Mapping, or the
                # equivalent for your provider). Treating this the same as
                # "present but empty" would silently revoke admin access from
                # an existing admin due to a config mistake, not a real
                # group-membership change, so skip the sync entirely rather
                # than fail closed on ambiguous data.
                logger.warning(
                    "OIDC_ADMIN_GROUP is set but this login's claims have no "
                    "'groups' key at all -- skipping admin sync for user %s "
                    "(check your OIDC provider's claim/scope mapping "
                    "configuration)",
                    user.pk,
                )
            else:
                is_admin = settings.OIDC_ADMIN_GROUP in groups
                if user.is_staff != is_admin or user.is_superuser != is_admin:
                    logger.info(
                        "OIDC admin-group sync: user %s admin status %s -> %s",
                        user.pk,
                        user.is_superuser,
                        is_admin,
                    )
                    user.is_staff = is_admin
                    user.is_superuser = is_admin
                    update_fields.extend(["is_staff", "is_superuser"])

        user.save(update_fields=update_fields)
        return user

    def filter_users_by_claims(self, claims):
        sub = claims.get("sub")
        if not sub:
            return self.UserModel.objects.none()
        return self.UserModel.objects.filter(oidc_sub=sub)

    def get_username(self, claims):
        return generate_username(claims.get("email", ""))


class OIDCCallbackView(OIDCAuthenticationCallbackView):
    """Sends a rejected first-time OIDC signup to the closed-registration page,
    and an invite-link-driven OIDC signup/login back into its join flow.

    OIDCBackend.create_user flags the request when it declines to auto-create an
    account because registration is closed and the user lacks the configured
    auto-enroll group. Any other authentication failure (bad state, provider
    error, etc.) keeps the default failure_url behavior.
    """

    def login_failure(self):
        if getattr(self.request, "oidc_registration_closed", False):
            return redirect("accounts:register")
        return super().login_failure()

    def login_success(self):
        """Redirect a usable-invite-token session to the invite-link join flow.

        mozilla_django_oidc honours ``next``/``LOGIN_REDIRECT_URL``, not this
        app's session key, so an SSO signup/login started from a challenge
        invite link (TASK-249) needs an explicit override here to land back in
        the join flow instead of wherever OIDC_CALLBACK normally sends it.
        Calls super() first for its login-and-default-redirect side effects,
        then substitutes the invite-link redirect when a usable token is
        present -- the session token itself is left in place, cleared by
        challenges.views.invite_link_view once the join actually succeeds.

        A brand-new OIDC account (OIDCBackend.create_user flagged this
        request via oidc_user_just_created) is sent into onboarding instead,
        before the invite-link override -- invite-link continuity is still
        honoured, just at the end of onboarding (onboarding_units_view)
        rather than here. A returning user never sets that flag and keeps
        this method's existing behavior.
        """
        from accounts.views import _invite_token_link

        response = super().login_success()
        if getattr(self.request, "oidc_user_just_created", False):
            return redirect("accounts:onboarding-tracking-method")
        invite_link = _invite_token_link(self.request)
        if invite_link is not None:
            return redirect("challenges:invite-link", token=invite_link.token)
        return response
