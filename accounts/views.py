"""Views for the accounts app."""

import logging
from urllib.parse import urlencode

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model, login, logout
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
    DeleteAccountConfirmationForm,
    EmailForm,
    HevyKeyForm,
    LanguageForm,
    LiftosaurKeyForm,
    NicknameForm,
    PasswordResetRequestForm,
    RegistrationForm,
    TimezoneForm,
    UnitPreferenceForm,
    WgerCredentialsForm,
)
from accounts.models import TrackerRequest
from accounts.ratelimit import (
    client_ip,
    login_ip_rate,
    login_username_rate,
    password_reset_email_rate,
    password_reset_ip_rate,
    register_ip_rate,
    validate_key_user_rate,
)
from accounts.services import anonymize_account, mask_api_key, send_password_reset_email
from accounts.timezones import (
    DETECT_COOKIE_MAX_AGE,
    DETECT_COOKIE_NAME,
    grouped_timezones,
    with_detect_param,
)
from challenges.models import Challenge, ChallengeParticipant
from challenges.services import (
    challenges_needing_new_owner,
    resolve_invite_token,
    transfer_ownership,
)
from core.http import is_htmx
from core.models import SiteSettings
from hevy_api.services import (
    HEVY_KEY_INVALID,
    HEVY_KEY_VALID,
    trigger_hevy_lift_history_backfill,
    validate_hevy_key,
    validate_hevy_key_status,
)
from hevy_api.services import last_synced_at as hevy_last_synced_at
from hevy_api.services import latest_sync_failure as hevy_latest_sync_failure
from hevy_api.services import sync_user_lifts as sync_hevy_lifts
from liftosaur.services import (
    last_synced_at,
    sync_user_lifts,
    trigger_lift_history_backfill,
    validate_liftosaur_key,
)
from policies.models import Policy, PolicyConsent, PolicyVersion
from policies.services import record_consent
from scoring.services import score_pooled_history
from wger.services import (
    last_synced_at as wger_last_synced_at,
)
from wger.services import (
    sync_wger_lifts,
    trigger_wger_lift_history_backfill,
    validate_wger_credentials,
)
from workout_imports.forms import WorkoutCsvImportForm
from workout_imports.services import import_workout_csv
from workout_imports.services import last_imported_at as workout_import_last_imported_at

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

    def form_valid(self, form):
        """Redirect a usable-invite-token session to the invite-link join flow.

        Mirrors OIDCCallbackView.login_success: a user who clicked a challenge
        invite link (TASK-249), then chose "sign in" instead of registering,
        would otherwise land on their normal next/LOGIN_REDIRECT_URL target
        and silently lose the pending invite. Calls super() first for its
        login-and-default-redirect side effects, then substitutes the
        invite-link redirect when a usable token is present -- the session
        token itself is left in place, cleared by challenges.views.invite_link_view
        once the join actually succeeds.
        """
        response = super().form_valid(form)
        invite_link = _invite_token_link(self.request)
        if invite_link is not None:
            return redirect("challenges:invite-link", token=invite_link.token)
        return response


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
    """Self-serve registration: validate credentials, create account, log in.

    Creates the account and records ToS/Privacy consent, then hands off to the
    onboarding flow (accounts:onboarding-tracking-method) for everything else
    -- choosing a tracking app, connecting it, and setting a unit
    preference. Registration itself asks for nothing beyond credentials and
    terms acceptance so a barrier-free signup (e.g. via a challenge invite)
    isn't blocked on anything else.

    A visitor arriving with a usable challenge invite-link token in their
    session (TASK-249) bypasses REGISTRATION_OPEN=False — the invite doubles
    as a beta invite. The invite-link join redirect itself now happens at the
    end of onboarding (accounts:onboarding-units), not here.
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
                tos_accepted_at=timezone.now(),
                acquisition_source=acquisition_source,
            )
            login(request, user, backend="django.contrib.auth.backends.ModelBackend")
            logger.info(
                "New account registered: user %s (acquisition_source=%s)",
                user.id,
                acquisition_source,
            )

            # The checkbox above only shows/links Terms of Service and Privacy
            # Policy, so only record consent for those two -- not any other
            # gated policy type that might exist later but wasn't shown here.
            record_consent(
                user,
                request,
                PolicyVersion.objects.active_gated().filter(
                    policy__policy_type__in=[
                        Policy.PolicyType.TOS,
                        Policy.PolicyType.PRIVACY,
                    ]
                ),
                PolicyConsent.Method.SIGNUP,
            )

            return redirect("accounts:onboarding-tracking-method")

    return render(
        request,
        "registration/register.html",
        {
            "errors": errors,
            "values": values,
            "invite_challenge": invite_link.challenge if invite_link else None,
        },
    )


ONBOARDING_TRACKER_APPS = ("liftosaur", "wger", "hevy", "strong")


@login_required
def onboarding_tracking_method_view(request):
    """Onboarding step 1: "Do you use a tracking app?"

    Routing-only -- the dropdown choice itself is never persisted anywhere,
    matching the pre-onboarding UI where this only decided which panel to
    show. Choosing a supported app goes on to whichever of API-key entry
    and/or CSV upload that app actually supports; "A different one" goes to a
    free-text feedback step (TrackerRequest) instead. The grey "No, I don't
    use one" button is a second, distinctly-named submit control -- per
    standard HTML form semantics only the clicked submit button's name/value
    pair is sent, so its presence in POST unambiguously means that button was
    clicked regardless of whatever the dropdown happens to be set to. Any of
    these "no app" paths (skip button, blank/unrecognized dropdown value) go
    on to onboarding_no_tracker_view, which suggests Liftosaur (with our
    affiliate coupon) before continuing to the units step, since manual
    self-report always lives in Settings either way.
    """
    if request.method == "POST":
        if request.POST.get("skip"):
            return redirect("accounts:onboarding-no-tracker")
        app = request.POST.get("tracking_app")
        if app in ONBOARDING_TRACKER_APPS:
            return redirect("accounts:onboarding-connect-tracker", app=app)
        if app == "other":
            return redirect("accounts:onboarding-other-tracker")
        return redirect("accounts:onboarding-no-tracker")

    return render(request, "registration/onboarding_tracking_method.html")


def _handle_onboarding_liftosaur_key(request, errors) -> None:
    api_key = request.POST.get("liftosaur_api_key", "").strip()
    if not api_key:
        return
    if not validate_liftosaur_key(api_key):
        errors["liftosaur_api_key"] = gettext(
            "Could not validate this Liftosaur API key."
        )
        return
    had_key_before = bool(request.user.liftosaur_api_key)
    request.user.liftosaur_api_key = api_key
    request.user.save(update_fields=["liftosaur_api_key"])
    if not had_key_before:
        # Seed the lifter's 12-month LiftHistory pool off the request cycle
        # so goal-setup and challenge joins later only need delta syncs.
        trigger_lift_history_backfill(request.user)


def _handle_onboarding_hevy_key(request, errors) -> None:
    api_key = request.POST.get("hevy_api_key", "").strip()
    if not api_key:
        return
    if not validate_hevy_key(api_key):
        errors["hevy_api_key"] = gettext("Could not validate this Hevy API key.")
        return
    had_key_before = bool(request.user.hevy_api_key)
    request.user.hevy_api_key = api_key
    request.user.save(update_fields=["hevy_api_key"])
    if not had_key_before:
        # Same one-time backfill contract as onboarding's Liftosaur path and
        # Settings' Hevy path -- seed history once, off the request cycle.
        trigger_hevy_lift_history_backfill(request.user)


def _handle_onboarding_wger_credentials(request, errors) -> None:
    instance_url = request.POST.get("wger_instance_url", "").strip()
    api_token = request.POST.get("wger_api_token", "").strip()
    if not (instance_url or api_token):
        return
    if not validate_wger_credentials(instance_url, api_token):
        errors["wger_credentials"] = gettext(
            "Could not validate this Wger instance URL or API token."
        )
        return
    if instance_url and api_token:
        had_credentials_before = bool(
            request.user.wger_instance_url and request.user.wger_api_token
        )
        request.user.wger_instance_url = instance_url
        request.user.wger_api_token = api_token
        request.user.save(update_fields=["wger_instance_url", "wger_api_token"])
        if not had_credentials_before:
            trigger_wger_lift_history_backfill(request.user)


def _handle_onboarding_csv_upload(request, errors) -> None:
    csv_file = request.FILES.get("csv_file")
    if not csv_file:
        return
    form = WorkoutCsvImportForm({}, {"csv_file": csv_file}, user=request.user)
    if not form.is_valid():
        errors["csv_file"] = form.errors["csv_file"][0]
        return
    try:
        # No challenge-rescore loop here (unlike Settings' identical CSV
        # handling) -- a brand-new account can't yet be an ACCEPTED
        # ChallengeParticipant of anything: the invite-link join this
        # wizard may be en route to only happens after the units step.
        import_workout_csv(request.user, form.cleaned_data["csv_file"])
    except OperationalError:
        logger.exception(
            "Onboarding workout CSV import failed for user %s", request.user.id
        )
        errors["csv_file"] = gettext(
            "Couldn't import right now. Please try again in a moment."
        )


@login_required
def onboarding_connect_tracker_view(request, app):
    """Onboarding step 2 (only reached via a tracking-app choice): connect it.

    Generalized over whichever app was picked in step 1, showing only what
    that app actually supports: Liftosaur and Hevy both offer an API key
    (Hevy's requires an active Hevy Pro subscription) plus a CSV upload;
    Wger (self-hostable, API-only, no CSV importer exists for it) is
    instance URL + API token; Strong (no live-sync integration merged) is
    CSV upload only. Every field is independently optional -- a blank
    submission just moves on, and submitting some but not all fields
    processes whichever were filled in without blocking on the others.
    Credentials are validated against the live API before saving, same as
    registration used to do for Liftosaur alone. The 12-month LiftHistory
    backfill only fires when the account didn't already have credentials for
    this app, so revisiting/resubmitting this step (e.g. after bookmarking
    it) doesn't re-seed history that's already been pulled.
    """
    if app not in ONBOARDING_TRACKER_APPS:
        return redirect("accounts:onboarding-tracking-method")

    errors = {}

    if request.method == "POST":
        if app == "liftosaur":
            _handle_onboarding_liftosaur_key(request, errors)
            _handle_onboarding_csv_upload(request, errors)
        elif app == "wger":
            _handle_onboarding_wger_credentials(request, errors)
        elif app == "hevy":
            _handle_onboarding_hevy_key(request, errors)
            _handle_onboarding_csv_upload(request, errors)
        else:
            _handle_onboarding_csv_upload(request, errors)

        if not errors:
            return redirect("accounts:onboarding-units")

    return render(
        request,
        "registration/onboarding_connect_tracker.html",
        {"app": app, "errors": errors},
    )


@login_required
def onboarding_other_tracker_view(request):
    """Onboarding step 2 (only reached via "A different one"): name it.

    Purely product-feedback signal for triaging the tracker-support backlog
    (e.g. GitHub issue #26) -- nothing here connects any account to anything.
    A blank/skipped name creates nothing and still continues, same
    optional-field-skips-forward pattern as the credential steps.
    """
    if request.method == "POST":
        app_name = request.POST.get("app_name", "").strip()
        if app_name:
            TrackerRequest.objects.create(user=request.user, app_name=app_name)
        return redirect("accounts:onboarding-units")

    return render(request, "registration/onboarding_other_tracker.html")


@login_required
def onboarding_no_tracker_view(request):
    """Onboarding step 2 (reached when no tracking app is in use): suggest Liftosaur.

    Reached via the "No, I don't use one" skip button and via any
    blank/unrecognized tracking_app value from step 1. Purely a suggestion --
    no credentials are collected here, matching onboarding_very_open_view's
    shape (external link out, secondary POST-and-continue button). GET
    renders the page; POST just continues to the units step.
    """
    if request.method == "POST":
        return redirect("accounts:onboarding-units")

    return render(request, "registration/onboarding_no_tracker.html")


@login_required
def onboarding_units_view(request):
    """Onboarding step 3: kg/lb display preference.

    Reuses the same UnitPreferenceForm/save() as Settings. GET pre-checks
    request.user.unit_preference -- the model default is lb, matching this
    step's own default, so a fresh account already shows lb here without
    needing to hardcode it.

    Always hands off to onboarding_very_open_view next, which itself decides
    whether to show anything or fall straight through to the invite-link/
    dashboard redirect register_view used to end with.
    """
    if request.method == "POST":
        form = UnitPreferenceForm(request.POST)
        form.is_valid()
        form.save(request.user)

        return redirect("accounts:onboarding-very-open")

    return render(
        request,
        "registration/onboarding_units.html",
        {"unit_preference": request.user.unit_preference},
    )


def _finish_onboarding(request):
    invite_link = _invite_token_link(request)
    if invite_link:
        # Leave the session token in place — invite_link_view clears
        # it once the join itself succeeds.
        return redirect("challenges:invite-link", token=invite_link.token)
    return redirect("challenges:dashboard")


@login_required
def onboarding_very_open_view(request):
    """Onboarding step 4 (final): optional invite to join the current Very Open.

    Only rendered when an operator has configured
    SiteSettings.very_open_invite_url for the season -- when it's blank (not
    yet set, or cleared once the invite window closed) this step is skipped
    entirely rather than showing a dead link, and both GET and POST fall
    straight through to the same invite-link/dashboard redirect that used to
    end onboarding_units_view. On POST (the user clicking through, whether
    they used the join link or not) it also falls through -- there's nothing
    to persist here, the join link is just an external link.
    """
    site_settings = SiteSettings.load()

    if request.method == "POST" or not site_settings.very_open_invite_url:
        return _finish_onboarding(request)

    return render(
        request,
        "registration/onboarding_very_open.html",
        {
            "very_open_invite_url": site_settings.very_open_invite_url,
            "very_open_label": site_settings.very_open_label,
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
    "wger_credentials": "accounts/_wger_section.html",
    "remove_wger_credentials": "accounts/_wger_section.html",
    "hevy_key": "accounts/_hevy_section.html",
    "remove_hevy_key": "accounts/_hevy_section.html",
    "workout_csv_import": "accounts/_workout_import_section.html",
    "unit_preference": "accounts/_unit_preference_section.html",
    "timezone": "accounts/_timezone_section.html",
}


@login_required
def settings_view(request):
    user = request.user
    avatar_error = None
    email_error = None
    workout_import_error = None
    hevy_key_error = None
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

        elif posted_form_name == "wger_credentials":
            form = WgerCredentialsForm(request.POST)
            form.is_valid()
            if form.save(user):
                trigger_wger_lift_history_backfill(user)
                messages.success(request, gettext("Wger connected."))

        elif posted_form_name == "remove_wger_credentials":
            user.wger_instance_url = None
            user.wger_api_token = None
            user.save(update_fields=["wger_instance_url", "wger_api_token"])
            messages.success(request, gettext("Wger disconnected."))

        elif posted_form_name == "hevy_key":
            form = HevyKeyForm(request.POST)
            form.is_valid()
            api_key = form.cleaned_data["hevy_api_key"]
            if api_key:
                # Validate before saving, mirroring
                # _handle_onboarding_hevy_key. Unlike that strict bool check,
                # an inconclusive probe (Hevy briefly unreachable) still gets
                # saved rather than rejected -- see validate_hevy_key_status's
                # docstring. A key confirmed bad is never saved.
                validation = validate_hevy_key_status(api_key)
                if validation == HEVY_KEY_INVALID:
                    hevy_key_error = gettext("Could not validate this Hevy API key.")
                else:
                    if form.save(user):
                        trigger_hevy_lift_history_backfill(user)
                    if validation == HEVY_KEY_VALID:
                        messages.success(request, gettext("Hevy API key saved."))
                    else:
                        messages.success(
                            request,
                            gettext(
                                "Hevy API key saved, but we couldn't confirm "
                                "it works right now. We'll let you know here "
                                "if syncing fails."
                            ),
                        )

        elif posted_form_name == "remove_hevy_key":
            user.hevy_api_key = None
            user.save(update_fields=["hevy_api_key"])
            messages.success(request, gettext("Hevy API key removed."))

        elif posted_form_name == "workout_csv_import":
            form = WorkoutCsvImportForm(request.POST, request.FILES, user=user)
            if form.is_valid():
                participations = ChallengeParticipant.objects.filter(
                    user=user,
                    invite_status=ChallengeParticipant.InviteStatus.ACCEPTED,
                    challenge__status=Challenge.Status.ACTIVE,
                ).select_related("challenge")
                try:
                    result = import_workout_csv(user, form.cleaned_data["csv_file"])
                    scored = 0
                    for participation in participations:
                        score_pooled_history(
                            user=user, challenge=participation.challenge
                        )
                        scored += 1
                except OperationalError:
                    # Same degrade-to-message contract as sync_now_view: a
                    # user-triggered action must report back instead of 500ing
                    # on a lost write-lock race.
                    logger.exception("Workout CSV import failed for user %s", user.id)
                    messages.error(
                        request,
                        gettext(
                            "Couldn't import right now. Please try again in a moment."
                        ),
                    )
                else:
                    messages.success(
                        request,
                        gettext(
                            "Imported %(count)s set(s) from %(source)s. "
                            "%(scored)s challenge(s) rescored."
                        )
                        % {
                            "count": result.pooled_count,
                            "source": result.source.label,
                            "scored": scored,
                        },
                    )
            else:
                workout_import_errors = form.errors.get("csv_file")
                workout_import_error = (
                    workout_import_errors[0] if workout_import_errors else None
                )

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
        if (
            not is_htmx(request)
            and avatar_error is None
            and email_error is None
            and workout_import_error is None
            and hevy_key_error is None
        ):
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
        "masked_hevy_key": mask_api_key(user.hevy_api_key),
        "hevy_key_error": hevy_key_error,
        "avatar_error": avatar_error,
        "last_synced_at": last_synced_at(user),
        "hevy_last_synced_at": hevy_last_synced_at(user),
        "hevy_sync_error": hevy_latest_sync_failure(user),
        "workout_import_error": workout_import_error,
        "last_workout_imported_at": workout_import_last_imported_at(user),
        "has_wger_credentials": bool(user.wger_instance_url and user.wger_api_token),
        "wger_instance_url": user.wger_instance_url,
        "masked_wger_token": mask_api_key(user.wger_api_token),
        "last_wger_synced_at": wger_last_synced_at(user),
    }

    if request.method == "POST" and is_htmx(request):
        context["oob_messages"] = True
        return render(request, _SETTINGS_SECTION_PARTIALS[posted_form_name], context)

    return render(request, "accounts/settings.html", context)


@login_required
def delete_account_view(request):
    """Danger Zone "Delete account" flow (#46, TASK-308).

    GET renders a typed-confirmation page (matching the cancel/leave/transfer
    confirm-page pattern in challenges.views); POST anonymizes the account in
    place (accounts.services.anonymize_account -- no hard delete, no data
    export) and logs the session out. There is no HTMX partial swap here on
    purpose, unlike every other settings section: the outcome ends the
    session, so a full PRG to the login page is the only response that makes
    sense.

    Any non-terminal challenge the user still owns is handed off first (via
    challenges.services.transfer_ownership, which also notifies the new
    owner) -- otherwise deletion would silently strand it behind a creator
    who can never log back in. The confirmation page's optional picker drawer
    lets the user override who each challenge goes to; an unopened/untouched
    picker still submits each challenge's preselected default (the
    longest-tenured eligible participant), so "don't interact with it" and
    "explicitly accept the defaults" are the same submission -- no separate
    tracking of whether the drawer was opened. A submitted choice is only
    trusted if it's still one of that challenge's actual eligible candidates
    (recomputed server-side, not read back from a hidden field) -- otherwise
    it silently falls back to the same default, rather than erroring out of
    account deletion over a stale/tampered picker value.
    """
    errors = {}
    user = request.user
    ownership_rows = challenges_needing_new_owner(user)
    if request.method == "POST":
        form = DeleteAccountConfirmationForm(request.POST)
        if form.is_valid():
            for row in ownership_rows:
                candidates_by_id = {str(c.id): c for c in row["candidates"]}
                submitted = request.POST.get(row["field_name"])
                new_owner = candidates_by_id.get(submitted, row["candidates"][0])
                transfer_ownership(row["challenge"], new_owner)
            anonymize_account(user)
            logger.info("Account %s deleted (self-serve) by its own owner", user.id)
            logout(request)
            messages.success(request, gettext("Your account has been deleted."))
            return redirect("accounts:login")
        errors["confirmation"] = form.errors["confirmation"][0]

    return render(
        request,
        "accounts/delete_account.html",
        {"errors": errors, "ownership_rows": ownership_rows},
    )


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
def wger_sync_now_view(request):
    """Force an immediate Wger sync + rescore, mirroring sync_now_view."""
    user = request.user
    if not user.wger_instance_url or not user.wger_api_token:
        messages.error(request, gettext("Connect your Wger account first."))
        return _wger_sync_now_response(request, user)

    participations = ChallengeParticipant.objects.filter(
        user=user,
        invite_status=ChallengeParticipant.InviteStatus.ACCEPTED,
        challenge__status=Challenge.Status.ACTIVE,
    ).select_related("challenge")

    try:
        sync_wger_lifts(user, force=True)

        count = 0
        for participation in participations:
            score_pooled_history(user=user, challenge=participation.challenge)
            count += 1
    except OperationalError:
        logger.exception("Forced Wger sync failed for user %s", user.id)
        messages.error(
            request,
            gettext("Couldn't sync right now. Please try again in a moment."),
        )
        return _wger_sync_now_response(request, user)

    messages.success(
        request,
        gettext("Sync triggered for %(count)s challenge(s).") % {"count": count},
    )
    return _wger_sync_now_response(request, user)


def _wger_sync_now_response(request, user):
    if is_htmx(request):
        return render(
            request,
            "accounts/_wger_sync_status.html",
            {"last_wger_synced_at": wger_last_synced_at(user), "oob_messages": True},
        )
    return redirect("accounts:settings")


@login_required
@require_POST
def hevy_sync_now_view(request):
    """Force an immediate Hevy pull, then re-score every active challenge.

    Mirrors sync_now_view for the Hevy source -- see that docstring.
    """
    user = request.user
    if not user.hevy_api_key:
        messages.error(request, gettext("Connect a Hevy API key first."))
        return _hevy_sync_now_response(request, user)

    participations = ChallengeParticipant.objects.filter(
        user=user,
        invite_status=ChallengeParticipant.InviteStatus.ACCEPTED,
        challenge__status=Challenge.Status.ACTIVE,
    ).select_related("challenge")

    try:
        sync_hevy_lifts(user, force=True)

        count = 0
        for participation in participations:
            score_pooled_history(user=user, challenge=participation.challenge)
            count += 1
    except OperationalError:
        logger.exception("Forced Hevy sync failed for user %s", user.id)
        messages.error(
            request,
            gettext("Couldn't sync right now. Please try again in a moment."),
        )
        return _hevy_sync_now_response(request, user)

    # force=True always logs an attempt (bypasses the cooldown short-circuit
    # that would otherwise skip logging entirely), so the log this call just
    # wrote is what latest_sync_failure reads back here. sync_hevy_lifts
    # swallows HevyAPIError/network/DB-contention failures and returns 0 --
    # the same value it returns for "nothing new" -- so the log, not the
    # return value, is what tells "Sync triggered" from an actual failure
    # apart.
    sync_failure = hevy_latest_sync_failure(user)
    if sync_failure is not None:
        logger.warning(
            "Hevy sync for user %s reported failure: %s",
            user.id,
            sync_failure.error_detail,
        )
        messages.error(
            request,
            gettext("Couldn't sync right now. Please try again in a moment."),
        )
    else:
        messages.success(
            request,
            gettext("Sync triggered for %(count)s challenge(s).") % {"count": count},
        )
    return _hevy_sync_now_response(request, user)


def _hevy_sync_now_response(request, user):
    if is_htmx(request):
        return render(
            request,
            "accounts/_hevy_sync_status.html",
            {
                "hevy_last_synced_at": hevy_last_synced_at(user),
                "hevy_sync_error": hevy_latest_sync_failure(user),
                "oob_messages": True,
            },
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


@login_required
@require_POST
@ratelimit(
    group="validate_key_user",
    key="user",
    rate=validate_key_user_rate,
    method="POST",
)
def validate_wger_credentials_view(request):
    """AJAX endpoint: validate a Wger instance URL + API token without saving.

    If either field is omitted, falls back to the user's saved credentials so
    the connected-card 'Test Connection' button can validate without
    re-transmitting the token to the browser.
    """
    instance_url = request.POST.get("wger_instance_url", "").strip()
    api_token = request.POST.get("wger_api_token", "").strip()
    if not instance_url:
        instance_url = (request.user.wger_instance_url or "").strip()
    if not api_token:
        api_token = (request.user.wger_api_token or "").strip()
    if not instance_url or not api_token:
        return JsonResponse(
            {"valid": False, "message": gettext("No Wger credentials provided.")}
        )

    if not validate_wger_credentials(instance_url, api_token):
        logger.warning("Wger credential validation failed for user %s", request.user.id)
        return JsonResponse(
            {
                "valid": False,
                "message": gettext("Invalid credentials or connection error."),
            }
        )

    return JsonResponse({"valid": True, "message": gettext("Connection successful.")})


@login_required
@require_POST
@ratelimit(
    group="validate_key_user",
    key="user",
    rate=validate_key_user_rate,
    method="POST",
)
def validate_hevy_key_view(request):
    """AJAX endpoint: validate a Hevy API key without saving it.

    Mirrors validate_liftosaur_key_view -- see that docstring.
    """
    api_key = request.POST.get("api_key", "").strip()
    if not api_key:
        api_key = (request.user.hevy_api_key or "").strip()
    if not api_key:
        return JsonResponse(
            {"valid": False, "message": gettext("No API key provided.")}
        )

    if not validate_hevy_key(api_key):
        logger.warning("Hevy key validation failed for user %s", request.user.id)
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
