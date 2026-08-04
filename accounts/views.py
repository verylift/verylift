"""Views for the accounts app."""

import logging
from urllib.parse import urlencode

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model, login
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import SetPasswordForm
from django.contrib.auth.tokens import default_token_generator
from django.contrib.auth.views import LoginView
from django.core.exceptions import ValidationError
from django.db import OperationalError
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.utils.http import url_has_allowed_host_and_scheme, urlsafe_base64_decode
from django.utils.translation import gettext
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST
from django_ratelimit.decorators import ratelimit

from accounts.forms import (
    AvatarForm,
    EmailForm,
    LanguageForm,
    LiftosaurKeyForm,
    NicknameForm,
    PasswordResetRequestForm,
    RegistrationForm,
    TimezoneForm,
    UnitPreferenceForm,
)
from accounts.ratelimit import (
    client_ip,
    login_ip_rate,
    login_username_rate,
    password_reset_email_rate,
    password_reset_ip_rate,
    register_ip_rate,
    validate_key_user_rate,
)
from accounts.services import mask_api_key, send_password_reset_email
from accounts.timezones import (
    DETECT_COOKIE_MAX_AGE,
    DETECT_COOKIE_NAME,
    grouped_timezones,
    with_detect_param,
)
from challenges.models import Challenge, ChallengeParticipant
from challenges.services import resolve_invite_token
from core.http import is_htmx
from liftosaur.services import (
    last_synced_at,
    sync_user_lifts,
    trigger_lift_history_backfill,
    validate_liftosaur_key,
)
from scoring.services import score_pooled_history

logger = logging.getLogger(__name__)


@method_decorator(
    ratelimit(group="login_ip", key=client_ip, rate=login_ip_rate, method="POST"),
    name="post",
)
@method_decorator(
    ratelimit(
        group="login_username",
        key="post:username",
        rate=login_username_rate,
        method="POST",
    ),
    name="post",
)
class LocalLoginView(LoginView):
    template_name = "registration/login.html"

    def dispatch(self, request, *args, **kwargs):
        # Redirect before super() so POST is covered too, not just the rendered
        # GET form: hiding the form in the UI while still accepting posted
        # credentials would leave local login working for anyone who knows the
        # endpoint. The admin panel keeps its own password login as a
        # break-glass path (AUTHENTICATION_BACKENDS is untouched).
        if settings.OIDC_ONLY_LOGIN:
            target = reverse("oidc_authentication_init")
            # Carried through so mozilla-django-oidc stashes it in the session
            # and the post-login redirect still lands where the user intended.
            next_url = request.GET.get(self.redirect_field_name) or request.POST.get(
                self.redirect_field_name
            )
            if next_url:
                target = f"{target}?{urlencode({self.redirect_field_name: next_url})}"
            return redirect(target)
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["oidc_configured"] = bool(getattr(settings, "OIDC_RP_CLIENT_ID", ""))
        ctx["oidc_provider_name"] = getattr(settings, "OIDC_PROVIDER_NAME", "SSO")
        return ctx


def _invite_token_link(request):
    """The usable ChallengeInviteLink named by this session's invite token, if any.

    A challenge invite link doubles as a beta invite (TASK-249): a session
    carrying a still-usable token from challenges.invite_link_view is allowed
    to register even while self-serve registration is otherwise closed.
    Returns None when there is no token, or it no longer resolves to a usable
    link (unknown/expired/revoked) — a stale token is silently treated the
    same as no token at all, the resolve/gate distinction is not this
    function's job. Also None when ``request`` has no ``session`` at all (a
    bare ``RequestFactory`` request with no session middleware applied, as
    several OIDCBackend unit tests construct) rather than raising.
    """
    session = getattr(request, "session", None)
    token = session.get("invite_token") if session is not None else None
    if not token:
        return None
    link, reason = resolve_invite_token(token)
    return link if reason is None else None


@ratelimit(group="register_ip", key=client_ip, rate=register_ip_rate, method="POST")
def register_view(request):
    """Self-serve registration: validate, create account, log in.

    The Liftosaur API key is optional (TASK-250): it's a barrier-free signup so
    someone arriving with zero context, e.g. via a challenge invite, isn't
    blocked. When a key is submitted it's validated against the live API before
    any account is created. On success the user is logged in; if a key was
    given, their 12-month LiftHistory pool is seeded off the request cycle. The
    key becomes mandatory later, at the moment the user joins a challenge.

    A visitor arriving with a usable challenge invite-link token in their
    session (TASK-249) bypasses REGISTRATION_OPEN=False — the invite doubles
    as a beta invite — and lands back in the invite-link join flow on success
    instead of the dashboard.
    """
    User = get_user_model()
    if request.user.is_authenticated:
        return redirect("challenges:dashboard")

    invite_link = _invite_token_link(request)

    # OIDC-only mode force-closes local signup unconditionally, regardless of
    # REGISTRATION_OPEN or any invite token: a local account created while
    # local login is hidden could never be used to sign in through the app UI.
    if settings.OIDC_ONLY_LOGIN or (
        not settings.REGISTRATION_OPEN and invite_link is None
    ):
        return render(
            request, "registration/register.html", {"registration_closed": True}
        )

    errors = {}
    values = {"username": "", "email": "", "accept_terms": False}

    if request.method == "POST":
        username = request.POST.get("username", "").strip()
        email = request.POST.get("email", "").strip()
        password = request.POST.get("password", "")
        password_confirm = request.POST.get("password_confirm", "")
        api_key = request.POST.get("liftosaur_api_key", "").strip()
        accept_terms = bool(request.POST.get("accept_terms"))
        values = {"username": username, "email": email, "accept_terms": accept_terms}

        # UserCreationForm runs AUTH_PASSWORD_VALIDATORS (length, common, numeric,
        # user-attribute similarity) and the confirm-match check; the template
        # posts `password`/`password_confirm`, mapped here to the form's fields.
        form = RegistrationForm(
            data={
                "username": username,
                "email": email,
                "password1": password,
                "password2": password_confirm,
            }
        )
        if not form.is_valid():
            if "username" in form.errors:
                errors["username"] = form.errors["username"][0]
            if "email" in form.errors:
                errors["email"] = form.errors["email"][0]
            password_errors = form.errors.get("password1", []) + form.errors.get(
                "password2", []
            )
            if password_errors:
                errors["password"] = " ".join(password_errors)

        if not accept_terms:
            errors["accept_terms"] = gettext(
                "You must accept the Terms of Service and Privacy Policy."
            )

        # The key is optional at signup (TASK-250): it becomes mandatory only when
        # the user joins a challenge, gated inline at that point instead of here.
        if api_key and not errors and not validate_liftosaur_key(api_key):
            errors["liftosaur_api_key"] = gettext(
                "Could not validate this Liftosaur API key."
            )

        if not errors:
            acquisition_source = (
                User.AcquisitionSource.INVITE_LINK
                if invite_link
                else User.AcquisitionSource.DIRECT
            )
            user = User.objects.create_user(
                username=username,
                email=email,
                password=password,
                liftosaur_api_key=api_key or None,
                tos_accepted_at=timezone.now(),
                acquisition_source=acquisition_source,
            )
            login(request, user, backend="django.contrib.auth.backends.ModelBackend")
            logger.info(
                "New account registered: user %s (acquisition_source=%s)",
                user.id,
                acquisition_source,
            )

            if api_key:
                # Seed the lifter's 12-month LiftHistory pool off the request
                # cycle so goal-setup and challenge joins later only need
                # delta syncs.
                trigger_lift_history_backfill(user)

            if invite_link:
                # Leave the session token in place — invite_link_view clears
                # it once the join itself succeeds.
                return redirect("challenges:invite-link", token=invite_link.token)
            return redirect("challenges:dashboard")

    return render(
        request,
        "registration/register.html",
        {
            "errors": errors,
            "values": values,
            "invite_challenge": invite_link.challenge if invite_link else None,
        },
    )


def _password_reset_unavailable(request):
    """The OIDC-only gate shared by all three password-reset views.

    Returns a redirect into the provider's authorization flow when local login
    is closed, else None. Checked before any POST handling for the same reason
    LocalLoginView.dispatch redirects before super(): gating only the rendered
    form would leave the endpoint live for anyone who knows the URL. In that
    mode credentials belong to the provider, so this app has no business
    resetting them. No ``next`` threading -- unlike login there is no post-auth
    destination to preserve.
    """
    if settings.OIDC_ONLY_LOGIN:
        return redirect("oidc_authentication_init")
    return None


@ratelimit(
    group="password_reset_ip",
    key=client_ip,
    rate=password_reset_ip_rate,
    method="POST",
)
@ratelimit(
    group="password_reset_email",
    key="post:email",
    rate=password_reset_email_rate,
    method="POST",
)
def password_reset_view(request):
    """Ask for the account's email address, then mail a reset link.

    Enumeration-safe by construction: nothing downstream of a syntactically
    valid address branches on whether an account matched. A live local account,
    an OIDC-only account, and an address belonging to nobody all take the same
    path and produce the same 302 to the same done page, which renders no
    request-derived data at all (it deliberately doesn't echo the address back
    -- doing so invites a future "we sent it to X" regression).

    The residual side-channel is timing: a real match does synchronous SMTP
    work and so answers measurably slower. That is accepted rather than padded
    with a sleep or a decoy send, both of which are fragile and unverifiable.
    The mitigation is the two rate limits above -- 5/h per IP and 3/h per
    submitted address bound an attacker to a trickle instead of a sweep, which
    is what makes the timing difference uninteresting. Please don't "fix" this
    with a sleep.
    """
    unavailable = _password_reset_unavailable(request)
    if unavailable:
        return unavailable
    if request.user.is_authenticated:
        return redirect("challenges:dashboard")

    errors = {}
    values = {"email": ""}

    if request.method == "POST":
        values = {"email": request.POST.get("email", "").strip()}
        form = PasswordResetRequestForm(data={"email": values["email"]})
        if form.is_valid():
            send_password_reset_email(
                form.cleaned_data["email"],
                base_url=request.build_absolute_uri("/").rstrip("/"),
            )
            # PRG so a refresh doesn't re-send.
            return redirect("accounts:password-reset-done")
        errors["email"] = form.errors["email"][0]

    return render(
        request,
        "registration/password_reset.html",
        {"errors": errors, "values": values},
    )


def password_reset_done_view(request):
    unavailable = _password_reset_unavailable(request)
    if unavailable:
        return unavailable
    return render(request, "registration/password_reset_done.html")


def _reset_user(uidb64):
    """The active user named by a reset link's uidb64, or None.

    ``ValidationError`` is mandatory in the except clause, not defensive
    padding: User.pk is a UUIDField, so a tampered uidb64 that decodes to a
    non-UUID string makes the queryset raise ValidationError rather than the
    ValueError an integer pk would give. Without it a garbage link 500s.
    """
    User = get_user_model()
    try:
        uid = urlsafe_base64_decode(uidb64).decode()
        return User.objects.get(pk=uid, is_active=True)
    except (TypeError, ValueError, OverflowError, ValidationError, User.DoesNotExist):
        return None


def password_reset_confirm_view(request, uidb64, token):
    """Set a new password from an emailed link.

    Single-use falls out of the token design rather than any bookkeeping here:
    PasswordResetTokenGenerator hashes the stored password, so saving a new one
    invalidates the token and a replayed link lands in the invalid branch.

    The token stays in the URL for both GET and POST rather than being stashed
    in the session behind a second redirect the way Django's own view does.
    Django's stated motivation there is keeping the token out of the Referer
    header, and SECURE_REFERRER_POLICY defaults to "same-origin" with
    SecurityMiddleware enabled and no override in this project -- so the header
    is never sent cross-origin, and this page loads no third-party assets.

    No auto-login on success: that would mean naming an auth backend for a
    session the user never asked for, and bouncing to the login page confirms
    the new password actually works while keeping LocalLoginView the single
    local-login entry point the OIDC-only gate depends on.
    """
    unavailable = _password_reset_unavailable(request)
    if unavailable:
        return unavailable

    user = _reset_user(uidb64)
    valid = (
        user is not None
        and user.has_usable_password()
        and default_token_generator.check_token(user, token)
    )
    if not valid:
        # No uid, no token in the log line -- the token is a bearer credential.
        logger.warning("Invalid or expired password reset link presented")
        return render(
            request, "registration/password_reset_confirm.html", {"validlink": False}
        )

    errors = {}
    if request.method == "POST":
        # Mapped onto the template's `password`/`password_confirm` names so the
        # markup matches register.html, while still getting
        # AUTH_PASSWORD_VALIDATORS and the mismatch check from SetPasswordForm.
        form = SetPasswordForm(
            user,
            data={
                "new_password1": request.POST.get("password", ""),
                "new_password2": request.POST.get("password_confirm", ""),
            },
        )
        if form.is_valid():
            form.save()
            logger.info("Password reset completed for user %s", user.id)
            messages.success(
                request,
                gettext(
                    "Your password has been updated. Sign in with your new password."
                ),
            )
            return redirect("accounts:login")
        password_errors = form.errors.get("new_password1", []) + form.errors.get(
            "new_password2", []
        )
        if password_errors:
            errors["password"] = " ".join(password_errors)

    return render(
        request,
        "registration/password_reset_confirm.html",
        {"validlink": True, "errors": errors},
    )


# Which section partial to swap back for each HTMX-driven settings form.
_SETTINGS_SECTION_PARTIALS = {
    "avatar": "accounts/_avatar_section.html",
    "nickname": "accounts/_nickname_section.html",
    "email": "accounts/_email_section.html",
    "liftosaur_key": "accounts/_liftosaur_section.html",
    "remove_liftosaur_key": "accounts/_liftosaur_section.html",
    "unit_preference": "accounts/_unit_preference_section.html",
    "timezone": "accounts/_timezone_section.html",
}


@login_required
def settings_view(request):
    user = request.user
    avatar_error = None
    email_error = None
    # None means "show the stored address"; a rejected submission replaces it
    # with what was typed so the error has something to point at.
    email_value = None
    posted_form_name = None

    if request.method == "POST":
        posted_form_name = request.POST.get("form_name")

        if posted_form_name == "avatar":
            form = AvatarForm(request.POST, request.FILES)
            if form.is_valid():
                form.save(user)
                messages.success(request, gettext("Profile photo updated."))
            else:
                avatar_errors = form.errors.get("avatar")
                avatar_error = avatar_errors[0] if avatar_errors else None

        elif posted_form_name == "nickname":
            form = NicknameForm(request.POST)
            form.is_valid()
            form.save(user)
            messages.success(request, gettext("Display name updated."))

        elif posted_form_name == "email":
            # Unlike the other single-field sections this one can genuinely fail
            # validation (a malformed address), so it can't use the
            # is_valid()-then-save shortcut and instead short-circuits the
            # redirect the way avatar_error does.
            form = EmailForm(request.POST)
            if form.is_valid():
                form.save(user)
                messages.success(request, gettext("Email address saved."))
            else:
                email_error = form.errors["email"][0]
                # Keep what they typed on screen next to the error rather than
                # snapping back to the stored address.
                email_value = request.POST.get("email", "")

        elif posted_form_name == "liftosaur_key":
            form = LiftosaurKeyForm(request.POST)
            form.is_valid()
            if form.save(user):
                messages.success(request, gettext("Liftosaur API key saved."))

        elif posted_form_name == "remove_liftosaur_key":
            user.liftosaur_api_key = None
            user.save(update_fields=["liftosaur_api_key"])
            messages.success(request, gettext("Liftosaur API key removed."))

        elif posted_form_name == "unit_preference":
            form = UnitPreferenceForm(request.POST)
            form.is_valid()
            form.save(user)
            messages.success(request, gettext("Unit preference saved."))

        elif posted_form_name == "timezone":
            form = TimezoneForm(request.POST)
            form.is_valid()
            form.save(user)
            messages.success(request, gettext("Timezone saved."))

        elif posted_form_name == "language":
            form = LanguageForm(request.POST)
            form.is_valid()
            form.save(user)
            messages.success(request, gettext("Language saved."))
            # Language changes re-render the whole page chrome, so this stays a
            # plain PRG redirect rather than an HTMX section swap. Set/clear the
            # anonymous-page cookie here so a pre-login visit on this browser
            # matches the choice too.
            response = redirect("accounts:settings")
            if user.language:
                response.set_cookie(settings.LANGUAGE_COOKIE_NAME, user.language)
            else:
                response.delete_cookie(settings.LANGUAGE_COOKIE_NAME)
            return response

        # HTMX requests swap only the posted section in place; plain requests keep
        # the PRG redirect on success. A validation error falls through to a
        # full-page (or partial) re-render at 200 so the error stays visible.
        if not is_htmx(request) and avatar_error is None and email_error is None:
            return redirect("accounts:settings")

    context = {
        "masked_liftosaur_key": mask_api_key(user.liftosaur_api_key),
        "email": user.email if email_value is None else email_value,
        "email_error": email_error,
        "unit_preference": user.unit_preference,
        "language": user.language,
        "languages": settings.LANGUAGES,
        "user_timezone": user.timezone,
        "timezone_groups": grouped_timezones(),
        "has_liftosaur_key": bool(user.liftosaur_api_key),
        "avatar_error": avatar_error,
        "last_synced_at": last_synced_at(user),
    }

    if request.method == "POST" and is_htmx(request):
        context["oob_messages"] = True
        return render(request, _SETTINGS_SECTION_PARTIALS[posted_form_name], context)

    return render(request, "accounts/settings.html", context)


@never_cache
@require_GET
def timezone_detect_view(request):
    """One-shot detour page for browser timezone detection (TASK-273 R1).

    Reached only via accounts.middleware.UserTimezoneMiddleware's
    _detection_redirect, when a request has no resolvable timezone. Renders a
    chrome-less page that sets the pp_timezone cookie from the browser's
    Intl.DateTimeFormat and immediately navigates back to ``next`` -- so the
    originally requested page is never rendered in UTC just because detection
    hasn't run yet. ``@never_cache`` so no proxy or bfcache ever serves a
    stale detection page carrying someone else's ``next``.
    """
    next_url = request.GET.get("next") or "/"
    if not url_has_allowed_host_and_scheme(
        next_url,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        logger.warning("Rejected unsafe next= for timezone detection: %r", next_url)
        next_url = "/"
    response = render(
        request,
        "accounts/timezone_detect.html",
        {"next_url": with_detect_param(next_url)},
    )
    response.set_cookie(
        DETECT_COOKIE_NAME,
        "1",
        max_age=DETECT_COOKIE_MAX_AGE,
        path="/",
        samesite="Lax",
    )
    return response


@login_required
@require_POST
def sync_now_view(request):
    """Force an immediate Liftosaur pull, then re-score every active challenge.

    Bypasses the per-user cooldown with force=True on a single shared-pool pull
    (the pool serves every challenge), then explicitly scores each active,
    accepted challenge. HTMX requests get the sync-status partial (with an
    out-of-band message) at HTTP 200 so only the last-synced line updates in
    place; plain requests redirect back to settings with a count of challenges
    scored.
    """
    user = request.user
    if not user.liftosaur_api_key:
        messages.error(request, gettext("Connect a Liftosaur API key first."))
        return _sync_now_response(request, user)

    participations = ChallengeParticipant.objects.filter(
        user=user,
        invite_status=ChallengeParticipant.InviteStatus.ACCEPTED,
        challenge__status=Challenge.Status.ACTIVE,
    ).select_related("challenge")

    try:
        sync_user_lifts(user, force=True)

        count = 0
        for participation in participations:
            score_pooled_history(user=user, challenge=participation.challenge)
            count += 1
    except OperationalError:
        # "Sync now" is the button a user mashes when things feel stuck, so a
        # lost write-lock race here is exactly the case that must report back
        # instead of 500ing. _sync_now_response already carries the message
        # out-of-band for HTMX and via redirect otherwise.
        logger.exception("Forced Liftosaur sync failed for user %s", user.id)
        messages.error(
            request,
            gettext("Couldn't sync right now. Please try again in a moment."),
        )
        return _sync_now_response(request, user)

    messages.success(
        request,
        gettext("Sync triggered for %(count)s challenge(s).") % {"count": count},
    )
    return _sync_now_response(request, user)


def _sync_now_response(request, user):
    if is_htmx(request):
        return render(
            request,
            "accounts/_liftosaur_sync_status.html",
            {"last_synced_at": last_synced_at(user), "oob_messages": True},
        )
    return redirect("accounts:settings")


@login_required
@require_POST
@ratelimit(
    group="validate_key_user",
    key="user",
    rate=validate_key_user_rate,
    method="POST",
)
def validate_liftosaur_key_view(request):
    """AJAX endpoint: validate a Liftosaur API key without saving it.

    If api_key is omitted or empty, falls back to the user's saved key so the
    key-card 'Test Connection' button can validate without re-transmitting the
    full key to the browser.
    """
    api_key = request.POST.get("api_key", "").strip()
    if not api_key:
        # Fall back to user's saved key (key-card test)
        api_key = (request.user.liftosaur_api_key or "").strip()
    if not api_key:
        return JsonResponse(
            {"valid": False, "message": gettext("No API key provided.")}
        )

    if not validate_liftosaur_key(api_key):
        logger.warning("Liftosaur key validation failed for user %s", request.user.id)
        return JsonResponse(
            {
                "valid": False,
                "message": gettext("Invalid API key or connection error."),
            }
        )

    return JsonResponse({"valid": True, "message": gettext("Connection successful.")})


def csrf_failure(request, reason=""):
    """Render the styled 403 page for CSRF validation failures.

    Wired via ``CSRF_FAILURE_VIEW`` and mirrors ``accounts.ratelimit.ratelimited_429``:
    Django's CSRF middleware calls this directly rather than through view dispatch,
    so it builds the response itself instead of raising for another handler to catch.
    """
    logger.warning("CSRF failure on %s: %s", request.path, reason)
    return render(request, "403.html", status=403)
