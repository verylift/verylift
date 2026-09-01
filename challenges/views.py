"""Views for the challenges app."""

import json
import logging
import time
from datetime import UTC, date, datetime
from decimal import Decimal

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db import OperationalError
from django.http import Http404, HttpResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext, ngettext
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST
from django_ratelimit.decorators import ratelimit

from accounts.forms import HevyKeyForm, LiftosaurKeyForm, WgerCredentialsForm
from accounts.ratelimit import client_ip
from accounts.units import to_display_weight
from challenges.custom_goals import detach_active_goal, save_custom_goal
from challenges.forms import (
    CreateChallengeDatesForm,
    CreateChallengeLiftsForm,
    CreateChallengeModeForm,
    CreateChallengeNameForm,
    CustomGoalForm,
    GoalInputsForm,
    GoalMethodForm,
    HistoryWindowForm,
    InviteLinkOptionsForm,
    RenameChallengeForm,
)
from challenges.goal_builders import (
    default_goal_name,
    history_source_detail,
    standards_source_detail,
    suggest_from_history,
    suggest_from_standards,
    suggest_rep_targets_from_history,
)
from challenges.models import (
    Challenge,
    ChallengeParticipant,
    CustomGoal,
    RepTargetGoal,
)
from challenges.rep_target_goals import (
    MAX_TARGET_REPS,
    detach_active_rep_target_goal,
    merge_suggested_fields,
    parse_rep_target_grid,
    parse_suggested_fields,
    rep_target_goal_is_complete,
    save_rep_target_goal,
)
from challenges.services import (
    activate_draft_for_creator,
    build_custom_goal_context,
    build_invite_link_qr_png,
    build_participant_chart,
    build_personal_data,
    build_rep_target_goal_context,
    build_rep_target_personal_data,
    challenge_end_instant,
    close_challenge,
    create_challenge,
    current_invite_link,
    delete_draft_challenge,
    get_co_participants,
    record_invite_link_use,
    regenerate_invite_link,
    remove_participant,
    resolve_invite_token,
    submit_manual_lift,
    submit_manual_rep_target_set,
    sync_and_score,
    transfer_ownership,
    update_invite_link,
)
from challenges.standards import covered_lift_names
from core.http import is_htmx
from core.models import SiteSettings
from fitnessvolt.services import standards_method_available
from hevy_api.services import sync_user_lifts as sync_hevy_lifts
from hevy_api.services import validate_hevy_key
from liftosaur.models import LiftHistory
from liftosaur.services import (
    last_synced_at,
    sync_user_lifts,
    validate_liftosaur_key,
)
from notifications.models import Notification
from notifications.views import dashboard_section_context
from scoring.domain.calculator import is_bodyweight_added_lift
from scoring.services import (
    build_career_stats,
    build_points_by_lift,
    build_points_over_time,
    build_recent_scoring_activity,
    get_leader,
    get_user_standing,
    rank_participants,
)
from wger.services import sync_wger_lifts, validate_wger_credentials
from workout_imports.forms import WorkoutCsvImportForm
from workout_imports.services import import_workout_csv

logger = logging.getLogger(__name__)

# Wall-clock budget for the participant sync loop in challenge_detail_view,
# independent of how many participants the challenge has. HEVY_SYNC_COOLDOWN_MINUTES
# / LIFTOSAUR_SYNC_COOLDOWN_MINUTES only stop a single already-recently-synced
# user from being re-pulled; they do nothing to bound how many *different*,
# not-recently-synced participants get pulled inside one render of this view --
# a challenge whose members mostly opened it for the first time in a while (or
# just joined and connected a tracker) can have every one of them outside
# cooldown at once, and the loop below calls sync_and_score for each in
# sequence. Each per-source pull is itself bounded (see
# hevy_api.services.MAX_EVENT_PAGES_PER_INLINE_RUN), but that bound is
# per-call, not per-request, so it does not by itself stop total latency from
# scaling with participant count.
#
# Once cumulative time spent on fresh pulls in this loop reaches this budget,
# remaining participants are still scored (local-DB-only, cheap) but skip the
# API pull and use whatever is already pooled -- the same "stale but not
# broken" fallback already used for a locked challenge (sync=False) and for
# any pull that errors out. 30s leaves the other half of gunicorn.conf.py's
# 60s worker timeout for the sync call already in flight when the budget is
# hit to finish, plus scoring, leaderboard assembly, and template rendering.
PARTICIPANT_SYNC_BUDGET_SECONDS = 30


def _invite_link_ip_rate(group, request):
    return settings.RATELIMIT_INVITE_LINK_IP


def _hx_redirect(request, url):
    """Navigate to ``url``: HX-Redirect header for htmx, PRG 302 otherwise.

    htmx follows an ordinary 302 and swaps the redirected page into the target,
    which would render a full goal-setup page inside a dashboard card. Returning
    an HX-Redirect header instead makes htmx do a client-side navigation, matching
    the plain PRG behaviour for the interactions that must move to another page.
    """
    if is_htmx(request):
        response = HttpResponse(status=204)
        response["HX-Redirect"] = url
        return response
    return redirect(url)


def _ensure_history_source(request):
    """Try to get the requesting user some pooled LiftHistory to suggest
    from, for the goal-setup wizard's history-recovery screen (issue #88).

    Tries, in order: a Liftosaur/Hevy/Wger credential (any one supplied),
    or a workout-CSV upload. Returns ``None`` when the attempt got far
    enough to be worth re-checking -- a credential validated and its
    initial pull ran to completion (see below for why that pull is
    synchronous here), or a CSV import completed -- so the caller redirects
    to a fresh GET. Note that this is deliberately NOT a promise that
    history now exists: a pull or import that legitimately yields zero rows
    also returns ``None``, and the caller's own history check then re-fires
    the recovery screen in its "connected but empty" form, which is the
    right outcome. Returns ``""`` when nothing was submitted at all (first
    time showing the screen). Otherwise returns a validation-failure
    message to redisplay inline.

    The credential branches deliberately call this connector's synchronous
    ``sync_user_lifts``/``sync_wger_lifts`` pull directly instead of the
    fire-and-forget ``trigger_*_backfill`` daemon-thread pattern used
    everywhere else a key is connected (Settings, onboarding). Those
    threads are fine when nothing downstream depends on the pull finishing,
    but a goal chart LOCKS PERMANENTLY once saved -- advancing into the
    suggestion step while a backfill thread is still mid-pull risks a
    silently-wrong, uncorrectable chart. Each sync function already caps
    itself to a request-cycle-safe amount of work per call (see
    MAX_*_PAGES_PER_INLINE_RUN / the Liftosaur pull's own page-cap
    behaviour), so calling it inline here is within the same budget the
    rest of the app already spends synchronously (e.g. sync_and_score at
    wizard entry).
    """
    user = request.user

    liftosaur_form = LiftosaurKeyForm(request.POST)
    if liftosaur_form.is_valid() and liftosaur_form.cleaned_data["liftosaur_api_key"]:
        api_key = liftosaur_form.cleaned_data["liftosaur_api_key"]
        if not validate_liftosaur_key(api_key):
            logger.warning(
                "Liftosaur key validation failed for user %s during history recovery",
                user.id,
            )
            return gettext("Could not validate this Liftosaur API key.")
        liftosaur_form.save(user)
        sync_user_lifts(user)
        logger.info(
            "User %s connected a Liftosaur key during history recovery for goal-setup",
            user.id,
        )
        return None

    hevy_form = HevyKeyForm(request.POST)
    if hevy_form.is_valid() and hevy_form.cleaned_data["hevy_api_key"]:
        api_key = hevy_form.cleaned_data["hevy_api_key"]
        if not validate_hevy_key(api_key):
            logger.warning(
                "Hevy key validation failed for user %s during history recovery",
                user.id,
            )
            return gettext("Could not validate this Hevy API key.")
        hevy_form.save(user)
        sync_hevy_lifts(user)
        logger.info(
            "User %s connected a Hevy key during history recovery for goal-setup",
            user.id,
        )
        return None

    wger_form = WgerCredentialsForm(request.POST)
    if wger_form.is_valid() and (
        wger_form.cleaned_data["wger_instance_url"]
        or wger_form.cleaned_data["wger_api_token"]
    ):
        instance_url = wger_form.cleaned_data["wger_instance_url"]
        api_token = wger_form.cleaned_data["wger_api_token"]
        if not (instance_url and api_token) or not validate_wger_credentials(
            instance_url, api_token
        ):
            logger.warning(
                "Wger credential validation failed for user %s during history recovery",
                user.id,
            )
            return gettext("Could not validate this Wger instance URL or API token.")
        wger_form.save(user)
        sync_wger_lifts(user)
        logger.info(
            "User %s connected Wger during history recovery for goal-setup",
            user.id,
        )
        return None

    if request.FILES.get("csv_file"):
        csv_form = WorkoutCsvImportForm(request.POST, request.FILES, user=user)
        if not csv_form.is_valid():
            return csv_form.errors["csv_file"][0]
        try:
            result = import_workout_csv(user, csv_form.cleaned_data["csv_file"])
        except OperationalError:
            logger.exception(
                "History-recovery workout CSV import failed for user %s", user.id
            )
            return gettext("Couldn't import right now. Please try again in a moment.")
        logger.info(
            "User %s imported %s set(s) from %s during history recovery for goal-setup",
            user.id,
            result.pooled_count,
            result.source,
        )
        messages.success(
            request,
            gettext("Imported %(count)s set(s) from %(source)s.")
            % {"count": result.pooled_count, "source": result.source.label},
        )
        return None

    # Nothing usable came back. If the lifter picked a tracker in the source
    # dropdown, they filled in none of the fields it revealed -- say so,
    # rather than re-rendering an apparently-unchanged screen with no
    # explanation. An untouched dropdown is the genuine "first time showing
    # this" case and still returns "".
    if request.POST.get("history_source"):
        return gettext(
            "Add an API key or choose a CSV export for the tracker you picked."
        )

    return ""


def _history_needed_response(
    request, challenge, *, tracker_connected, action_url, error=None, cancel_url=None
):
    """Render the source-agnostic "let's get some history in here" screen for
    the goal-setup wizard's history method (issue #88), reached whenever the
    lifter has no pooled LiftHistory to suggest from yet.

    Replaces the old Liftosaur-only "connect your key" interstitial: offers
    any of Liftosaur/Hevy/Wger connect, a workout-CSV upload, or bailing out
    to manual entry. ``tracker_connected`` branches the copy -- a lifter with
    a live-sync tracker already connected (a fresh Wger instance, a sync
    that silently failed, an empty lookback window) reads very differently
    from one who has connected nothing at all.
    """
    context = {
        "challenge": challenge,
        "tracker_connected": tracker_connected,
        "action_url": action_url,
        "cancel_url": cancel_url or reverse("challenges:dashboard"),
        "error": error,
        # Re-selects the tracker picker after a failed submit, so a bad key
        # doesn't also silently collapse the fields the lifter had open.
        "history_source": request.POST.get("history_source", ""),
    }
    template = (
        "challenges/_history_needed.html"
        if is_htmx(request)
        else "challenges/history_needed.html"
    )
    return render(request, template, context)


def _notify_user_joined(challenge, joining_user):
    """Notify every other accepted, non-bailed participant that a user joined.

    The joining user does not notify themselves.
    """
    others = (
        ChallengeParticipant.objects.filter(
            challenge=challenge,
            invite_status=ChallengeParticipant.InviteStatus.ACCEPTED,
            is_bailed=False,
        )
        .exclude(user=joining_user)
        .select_related("user")
    )
    Notification.objects.bulk_create(
        [
            Notification(
                user=other.user,
                event_type=Notification.EventType.USER_JOINED,
                challenge=challenge,
                metadata={"joined_user_name": str(joining_user)},
            )
            for other in others
        ]
    )
    logger.info(
        "User %s joined challenge %s; notified %s participant(s)",
        joining_user.id,
        challenge.pk,
        others.count(),
    )


@login_required
def dashboard_view(request):
    """Personal home page at ``/dashboard/``.

    Top to bottom: an overview row of three cards (the user's photo/avatar
    hero, cross-challenge career stats, and the people they've played with), a
    scrollable notifications section, a unified Challenges section
    (create-challenge CTA beside the title; active challenges are represented
    by their own Points Over Time chart panel rather than a static card),
    completed challenges demoted to a collapsed list, and finally the
    first-scored-point-vs-latest comparison at the very bottom. The participant
    rows are partitioned into Active and Completed.

    The notifications section defaults to unread-only; ``?show_read=1``
    reveals read ones too, matching the query-param convention
    ``find_challenges_view`` uses for ``hide_completed`` — the same state the
    HTMX toggle in the section itself carries when JS is unavailable.
    """
    show_read = request.GET.get("show_read") == "1"
    participations = (
        ChallengeParticipant.objects.filter(user=request.user)
        .select_related("challenge")
        .order_by("-created_at")
    )

    active = []
    completed = []
    for participation in participations:
        if participation.invite_status == ChallengeParticipant.InviteStatus.ACCEPTED:
            challenge = participation.challenge
            # A departed participant always loses their dashboard card. Open
            # challenges used to keep it (they stayed publicly readable); with
            # open visibility retired every challenge is invite-only, so the
            # carve-out went with it — deliberate.
            if participation.is_bailed:
                continue
            if challenge.status == Challenge.Status.COMPLETED:
                completed.append(participation)
            elif challenge.status == Challenge.Status.ACTIVE:
                active.append(participation)

    def build_cards(participants, *, with_chart=False):
        cards = []
        for participation in participants:
            challenge = participation.challenge
            standing = get_user_standing(challenge, request.user)
            card = {
                "challenge": challenge,
                "total_points": standing["total_points"],
                "rank": standing["rank"],
                # Every row reaching here is already ACCEPTED and not bailed
                # (the partition loop above drops everything else), so only the
                # goal and lock conditions are left to test.
                "needs_goal": (
                    not participation.has_goal_configured and not challenge.is_terminal
                ),
            }
            if with_chart:
                card["chart_data"] = build_points_over_time(challenge)
                card["script_id"] = f"potc-data-{challenge.pk}"
            cards.append(card)
        return cards

    context = {
        "active_cards": build_cards(active, with_chart=True),
        "completed_cards": build_cards(completed),
        "career": build_career_stats(request.user),
        "co_participants": get_co_participants(request.user),
        **dashboard_section_context(request.user, show_read=show_read),
    }
    return render(request, "dashboard.html", context)


_CREATE_DATA_SESSION_KEY = "create_challenge_data"
_CREATE_STEP_SESSION_KEY = "create_challenge_step_index"


def _wizard_steps(data):
    """Effective step list for the create wizard.

    Name -> Dates -> Mode -> Lifts (issue #85 inserted "mode" right after
    dates). There is no invitee step (TASK-272: challenges are invite-only and
    everyone joins by shareable link), and goal setup happens per-participant
    at join. Kept as a function (rather than a bare constant) so the
    session-based "go back" / re-render flow keeps its existing shape.
    """
    return ["name", "dates", "mode", "lifts"]


def _create_wizard_step_form(step, data, *, post=None):
    """Build the form for ``step``, bound to ``post`` or prefilled from ``data``.

    ``data`` is the session-accumulated wizard state from earlier steps, used
    to prefill a step's form on GET (e.g. after ``?back=1``) so returning to an
    earlier step doesn't lose what was already entered.
    """
    if step == "name":
        if post is not None:
            return CreateChallengeNameForm(post)
        return CreateChallengeNameForm(initial={"name": data.get("name", "")})
    if step == "dates":
        if post is not None:
            return CreateChallengeDatesForm(post)
        return CreateChallengeDatesForm(
            initial={
                "start_date": data.get("start_date", ""),
                "end_date": data.get("end_date", ""),
            }
        )
    if step == "mode":
        if post is not None:
            return CreateChallengeModeForm(post)
        return CreateChallengeModeForm(
            initial={"mode": data.get("mode", Challenge.Mode.CLASSIC)}
        )
    # step == "lifts", the final step. No prefill-from-session branch here (the
    # other steps have one): submitting this step creates the challenge and
    # clears the wizard session, so no GET can ever arrive with a stashed lift
    # selection to restore.
    if post is not None:
        return CreateChallengeLiftsForm(post)
    return CreateChallengeLiftsForm()


@login_required
def create_challenge_view(request):
    """Guided wizard to create a challenge: name, dates, then lifts.

    Session-tracked, one step per request: each step's validated data is
    stashed in the session and the step index advances; the Challenge itself
    is only created once the final ("lifts") step is submitted, which then
    hands off to the share screen carrying its brand-new invite link.
    ``?back=1`` returns to the previous step without losing progress;
    ``?cancel=1`` abandons the wizard entirely.

    Every challenge created here has a fixed history_window (FROM_START) and
    plate_unit/smallest_plate — the old units/rounding/visibility drawer is
    gone from creation, and every challenge is invite-only (TASK-272), so only
    the name, dates, and lift list are still creator choices (TASK-248: the
    owner no longer picks a chart-generation standard at all — each
    participant builds their own goal chart at join, via goal_setup_view).
    """
    if request.GET.get("cancel") == "1":
        request.session.pop(_CREATE_DATA_SESSION_KEY, None)
        request.session.pop(_CREATE_STEP_SESSION_KEY, None)
        return redirect("challenges:dashboard")

    data = request.session.get(_CREATE_DATA_SESSION_KEY, {})
    index = request.session.get(_CREATE_STEP_SESSION_KEY, 0)
    if request.GET.get("back") == "1" and index > 0:
        index -= 1
        request.session[_CREATE_STEP_SESSION_KEY] = index

    steps = _wizard_steps(data)
    step = steps[index]

    if request.method == "POST":
        form = _create_wizard_step_form(step, data, post=request.POST)
        if form.is_valid():
            if step == "name":
                data["name"] = form.cleaned_data["name"]
            elif step == "dates":
                data["start_date"] = form.cleaned_data["start_date"].isoformat()
                data["end_date"] = form.cleaned_data["end_date"].isoformat()
            elif step == "mode":
                data["mode"] = form.cleaned_data["mode"]
            elif step == "lifts":
                data["lift_names"] = [lift.name for lift in form.cleaned_data["lifts"]]
                creation_data = {
                    "name": data["name"],
                    "start_date": date.fromisoformat(data["start_date"]),
                    "end_date": date.fromisoformat(data["end_date"]),
                    "mode": data.get("mode", Challenge.Mode.CLASSIC),
                    "history_window": Challenge.HistoryWindow.FROM_START,
                    "plate_unit": Challenge.PlateUnit.LB,
                    "smallest_plate_kg": Decimal("1.25"),
                    "custom_lift_names": data["lift_names"],
                }
                challenge = create_challenge(request.user, creation_data)
                request.session.pop(_CREATE_DATA_SESSION_KEY, None)
                request.session.pop(_CREATE_STEP_SESSION_KEY, None)
                return redirect(reverse("challenges:share", args=[challenge.pk]))

            request.session[_CREATE_DATA_SESSION_KEY] = data
            request.session[_CREATE_STEP_SESSION_KEY] = index + 1
            return redirect("challenges:create")
        # invalid: fall through and re-render this step with the bound form
    else:
        form = _create_wizard_step_form(step, data)

    total_steps = len(steps)
    context = {
        "step": step,
        "step_number": index + 1,
        "total_steps": total_steps,
        "progress_percent": int((index + 1) / total_steps * 100),
        "form": form,
    }
    return render(request, "challenges/create.html", context)


@login_required
def find_challenges_view(request):
    """Paginated list of the challenges the requesting user is a member of.

    Nothing here is public any more (TASK-272 retired open challenges): the
    queryset is scoped to challenges the viewer participates in, so it leaks
    nothing. Each row shows the current leader from the leaderboard; joining is
    not offered at all — the only way in is a challenge's shareable invite link.

    DRAFT rows are included, unlike the dashboard's buckets. A link-bearer can
    join a DRAFT challenge (invite_link_view gates on ``is_terminal``, not
    ``status``) but only its creator can activate it, so without DRAFT here an
    invitee who joins before the owner finishes their own goal setup would have
    no in-app route back to the challenge at all.
    """
    hide_completed = request.GET.get("hide_completed") == "1"
    statuses = (
        (Challenge.Status.ACTIVE,)
        if hide_completed
        else (
            Challenge.Status.DRAFT,
            Challenge.Status.ACTIVE,
            Challenge.Status.COMPLETED,
        )
    )
    challenges = (
        Challenge.objects.filter(
            participants__user=request.user,
            participants__invite_status=(ChallengeParticipant.InviteStatus.ACCEPTED),
            participants__is_bailed=False,
            status__in=statuses,
        )
        .distinct()
        .order_by("-end_date")
    )

    paginator = Paginator(challenges, 20)
    page = paginator.get_page(request.GET.get("page"))

    rows = []
    for challenge in page.object_list:
        leader = get_leader(challenge)
        rows.append(
            {
                "challenge": challenge,
                "leader_name": (
                    leader["user"].display_name or leader["user"].username
                    if leader
                    else None
                ),
                "leader_points": leader["total_points"] if leader else None,
            }
        )

    return render(
        request,
        "challenges/find.html",
        {"rows": rows, "page_obj": page, "hide_completed": hide_completed},
    )


@ratelimit(group="invite_link_ip", key=client_ip, rate=_invite_link_ip_rate)
def invite_link_view(request, token):
    """Landing page for a challenge's shareable invite link (TASK-249, AC#1/#2).

    Deliberately NOT @login_required: this is the public landing surface for a
    bearer link, so anonymous visitors must be able to reach it. An unknown
    token 404s with no information leaked; an expired/revoked one renders an
    explanatory page naming the challenge (the bearer already held a
    valid-format link for it, so that's not a leak).

    An anonymous visitor gets a real, OG-tagged welcome page naming the
    challenge and inviter (with links on to register/login, and back out to
    the landing page) rather than an immediate redirect. This used to redirect
    straight through, but link-preview fetchers need an actual rendered
    response to read OG tags from, and some of them (notably iMessage's) don't
    self-identify via User-Agent, so detecting "is this a bot" server-side
    isn't reliable enough to special-case -- every anonymous visitor, human or
    fetcher, sees the same page (invite_link_preview.html).

    Per-IP rate limited (TASK-300): tokens are now Discord-length (8 chars,
    48 bits of entropy) rather than the original 43-char/256-bit ones, which
    is enough entropy against a naive brute force but not against a fast,
    unthrottled scan. Matches the client_ip/rate-callable/settings pattern
    already used for auth (accounts/ratelimit.py, TASK-153) and the
    newsletter form (core/views.py) rather than inventing a new one.

    This is now the only way to join a challenge (TASK-272 deleted the direct
    join and accept/decline views). Its guard ladder was ported from the old
    join view rather than rewritten, since TASK-58/152/250 fixed that ordering
    piecemeal and it is load-bearing. Two deliberate differences from what it
    replaced: it gates on ``is_terminal`` rather than ``status != ACTIVE`` (a
    DRAFT challenge is joinable via link, since the owner gets their share link
    before finishing their own goal setup), and every fresh/rejoined participant
    row records ``joined_via_link`` (AC#3's per-join provenance). No membership
    pre-check is needed either: possessing a valid, unexpired token is itself
    the authorization.

    A terminal challenge is checked uniformly, before the anonymous/
    authenticated split -- a never-expiring link (issue #33) can otherwise
    stay live and shareable long after its challenge ends, so anyone still
    hitting it gets a dedicated "this one's over, start your own"
    page (invite_link_ended.html) instead of either an anonymous join-preview
    that dead-ends at signup, or the raw 400 an authenticated visitor used to
    get at the actual join attempt.
    """
    link, reason = resolve_invite_token(token)

    if reason == "unknown":
        raise Http404

    challenge = link.challenge

    if reason in ("expired", "revoked", "exhausted"):
        logger.warning(
            "Visitor hit a %s invite link for challenge %s", reason, challenge.pk
        )
        return render(
            request,
            "challenges/invite_link_invalid.html",
            {"challenge": challenge, "reason": reason},
        )

    if challenge.is_terminal:
        # Checked uniformly for anonymous and authenticated visitors, and
        # before the invite_token session write below -- a never-expiring
        # link (issue #33) could otherwise walk an anonymous visitor through
        # the entire registration flow only to dead-end on the raw
        # HttpResponseBadRequest this used to return once they finally tried
        # to join. Deliberately does NOT stash invite_token in the session:
        # there is nothing left to join here, and leaving an old, unrelated
        # pending invite (from a still-active challenge visited earlier)
        # untouched is safer than overwriting it with one that can never be
        # redeemed.
        logger.info("Visitor hit an invite link for ended challenge %s", challenge.pk)
        return render(
            request,
            "challenges/invite_link_ended.html",
            {
                "challenge": challenge,
                "participant_count": challenge.participants.filter(
                    invite_status=ChallengeParticipant.InviteStatus.ACCEPTED,
                    is_bailed=False,
                ).count(),
            },
        )

    if not request.user.is_authenticated:
        request.session["invite_token"] = token
        participant_count = challenge.participants.filter(
            invite_status=ChallengeParticipant.InviteStatus.ACCEPTED,
            is_bailed=False,
        ).count()
        return render(
            request,
            "challenges/invite_link_preview.html",
            {
                "challenge": challenge,
                "link": link,
                "participant_count": participant_count,
                "discord_invite_url": SiteSettings.load().discord_invite_url,
            },
        )

    existing = ChallengeParticipant.objects.filter(
        challenge=challenge, user=request.user
    ).first()

    if existing is not None and not existing.is_bailed:
        logger.info(
            "User %s re-visited their invite link for challenge %s they "
            "already belong to",
            request.user.id,
            challenge.pk,
        )
        return _hx_redirect(request, reverse("challenges:detail", args=[challenge.pk]))

    if existing is not None and existing.is_bailed and existing.removed_by_creator:
        logger.warning(
            "User %s tried to use an invite link to rejoin challenge %s after "
            "creator removal",
            request.user.id,
            challenge.pk,
        )
        return HttpResponseBadRequest(
            gettext(
                "You were removed from this challenge and cannot rejoin with this link."
            )
        )

    if existing is not None and existing.is_bailed:
        existing.is_bailed = False
        existing.bailed_at = None
        existing.invite_status = ChallengeParticipant.InviteStatus.ACCEPTED
        existing.joined_at = datetime.now(tz=UTC)
        existing.joined_via_link = link
        existing.save(
            update_fields=[
                "is_bailed",
                "bailed_at",
                "invite_status",
                "joined_at",
                "joined_via_link",
            ]
        )
        logger.info(
            "User %s rejoined challenge %s via invite link",
            request.user.id,
            challenge.pk,
        )
        _notify_user_joined(challenge, request.user)
        record_invite_link_use(link)
        request.session.pop("invite_token", None)
        return _hx_redirect(
            request, reverse("challenges:goal-setup", args=[challenge.pk])
        )

    return _render_invite_accept(request, challenge, link)


def _join_challenge_via_link(request, challenge, link):
    """Create the accepted ``ChallengeParticipant`` row for a fresh join.

    The GET branch of ``invite_link_view`` used to call this directly and
    auto-join; TASK-303 moved that to a confirmation page instead, so this is
    now only reached via the ``invite-accept`` POST endpoint below -- kept as
    its own function so the create/notify/record/redirect sequence still
    lives in exactly one place.
    """
    ChallengeParticipant.objects.create(
        challenge=challenge,
        user=request.user,
        invite_status=ChallengeParticipant.InviteStatus.ACCEPTED,
        joined_at=datetime.now(tz=UTC),
        joined_via_link=link,
    )
    logger.info(
        "User %s joined challenge %s via invite link", request.user.id, challenge.pk
    )
    _notify_user_joined(challenge, request.user)
    record_invite_link_use(link)
    request.session.pop("invite_token", None)
    return _hx_redirect(request, reverse("challenges:goal-setup", args=[challenge.pk]))


# Fewer datasets fit comfortably on the invite-accept page's narrow mobile
# card than its wide desktop card (TASK-303) -- these are fixed per-breakpoint
# counts rather than a JS-measured width, since the chart is server-rendered
# from two separate contexts (one per breakpoint, toggled via CSS).
_INVITE_ACCEPT_CHART_TOP_N_MOBILE = 5
_INVITE_ACCEPT_CHART_TOP_N_DESKTOP = 8


def _render_invite_accept(request, challenge, link):
    """Render the accept/decline preview for an authenticated non-participant.

    Reused verbatim by the GET landing branch and by the POST accept view's
    "state changed since page load" fallback (TASK-303) -- both need the same
    challenge preview, not a full guard-ladder rewrite.
    """
    participant_count = challenge.participants.filter(
        invite_status=ChallengeParticipant.InviteStatus.ACCEPTED,
        is_bailed=False,
    ).count()

    leaderboard = rank_participants(challenge, include_unscored=True)
    leader_points = leaderboard[0]["total_points"] if leaderboard else 0
    # When nobody has scored yet, every dense rank is honestly tied at 1 --
    # render "-" instead of a numeric rank so a fresh challenge doesn't imply
    # a meaningless ranking (rank_participants itself stays mathematically
    # honest; this substitution is presentation-only).
    show_ranks = leader_points > 0
    leaderboard_rows = [
        {
            "rank": row["rank"] if show_ranks else "-",
            "user": row["user"],
            "total_points": row["total_points"],
            "bar_pct": (
                round(row["total_points"] / leader_points * 100, 1)
                if leader_points
                else 0
            ),
        }
        for row in leaderboard
    ]

    context = {
        "challenge": challenge,
        "link": link,
        "participant_count": participant_count,
        "custom_lifts": list(challenge.custom_lifts.all()),
        "leaderboard_rows": leaderboard_rows,
        "chart_data_mobile": build_points_over_time(
            challenge, top_n=_INVITE_ACCEPT_CHART_TOP_N_MOBILE
        ),
        "chart_data_desktop": build_points_over_time(
            challenge, top_n=_INVITE_ACCEPT_CHART_TOP_N_DESKTOP
        ),
        "discord_invite_url": SiteSettings.load().discord_invite_url,
        "accept_url": reverse("challenges:invite-accept", args=[link.token]),
    }
    return render(request, "challenges/invite_accept.html", context)


def _invite_link_qr_ip_rate(group, request):
    return settings.RATELIMIT_INVITE_LINK_QR_IP


@require_GET
@never_cache
@ratelimit(group="invite_link_qr_ip", key=client_ip, rate=_invite_link_qr_ip_rate)
def invite_link_qr_view(request, token):
    """PNG QR code encoding a challenge's invite link (TASK-339 / issue #79).

    Deliberately public and unauthenticated, same boundary as
    invite_link_view: the URL this encodes is already public by design (a
    bearer link, not a per-user secret), and gating the image behind auth
    would break the print/screen-display use case the QR exists for.

    Keyed on the token exactly like invite_link_view rather than on the
    challenge, so this can never drift from the link it's meant to
    represent: regenerating a link mints a new token and revokes the old
    one, which makes every image encoding the old token 404 automatically,
    with nothing to separately invalidate.

    404s uniformly whenever ``resolve_invite_token`` doesn't report a live
    link (unknown, expired, revoked, or exhausted) rather than
    invite_link_view's friendlier per-reason pages -- there's no useful
    "here's an image of a link that used to work" response for a bare PNG,
    and treating every dead reason identically means a scan of this
    endpoint gives no signal beyond 404/not-404, so it can't be used to
    enumerate which tokens are currently live any more than the join page
    already can. A *live* link for a since-ended challenge still renders
    normally here -- scanning it lands on invite_link_view's own
    invite_link_ended.html, so the QR never needs to know about that case
    itself.

    ``@never_cache`` plus the per-request rate limit keep this from being
    servable stale past a regeneration by an intermediate cache or CDN.
    """
    _link, reason = resolve_invite_token(token)
    if reason is not None:
        raise Http404

    url = request.build_absolute_uri(reverse("challenges:invite-link", args=[token]))
    png = build_invite_link_qr_png(url)
    # Served inline, with no Content-Disposition: this one URL is both the
    # <img src> preview and the "save QR code" target, and claiming
    # "attachment" while also being rendered inline is a contradiction waiting
    # to confuse someone. Browsers happen to ignore the header for <img>
    # subresource loads, so it worked, but the download is already handled
    # where it belongs -- the template's same-origin <a download="..."> names
    # the file the visitor gets.
    return HttpResponse(png, content_type="image/png")


@login_required
@require_POST
@ratelimit(group="invite_link_ip", key=client_ip, rate=_invite_link_ip_rate)
def invite_accept_view(request, token):
    """Handle the "Accept & Join" click on the invite accept/decline page.

    Re-runs the same guard ladder as ``invite_link_view`` rather than trusting
    that nothing changed between the GET render and this click (TASK-303).
    Fill/end races between the two are explicitly out of this task's scope --
    if state changed (link no longer usable, challenge went terminal, or a
    participant row now exists), this just falls back to re-rendering
    whatever ``invite_link_view`` itself would show for that state, rather
    than building new race-condition UX.
    """
    link, reason = resolve_invite_token(token)

    if reason == "unknown":
        raise Http404

    challenge = link.challenge

    if reason in ("expired", "revoked", "exhausted"):
        logger.warning(
            "User %s POSTed accept for a %s invite link for challenge %s",
            request.user.id,
            reason,
            challenge.pk,
        )
        return render(
            request,
            "challenges/invite_link_invalid.html",
            {"challenge": challenge, "reason": reason},
        )

    if challenge.is_terminal:
        logger.warning(
            "User %s POSTed accept for terminal challenge %s",
            request.user.id,
            challenge.pk,
        )
        return HttpResponseBadRequest(gettext("Challenge is not active"))

    existing = ChallengeParticipant.objects.filter(
        challenge=challenge, user=request.user
    ).first()
    if existing is not None:
        logger.info(
            "User %s already had a participant row for challenge %s by the "
            "time they clicked accept; falling back to the invite-link view",
            request.user.id,
            challenge.pk,
        )
        return redirect("challenges:invite-link", token=token)

    return _join_challenge_via_link(request, challenge, link)


_GOAL_DATA_SESSION_KEY = "goal_setup_data"
_GOAL_STEP_SESSION_KEY = "goal_setup_step_index"


def _goal_setup_steps(challenge, data):
    """Effective step list for the goal-setup wizard so far.

    "inputs" (the shared bodyweight/sex/population/tier/rounding step,
    TASK-248 plan §1c) appears for both standards and history, always --
    history needs it even with no bodyweight-added lift configured, since a
    rounding-increment choice (UAT feedback: raw Epley/uplift math produces
    "crazy numbers") is needed for every history-derived suggestion, not
    just bodyweight-added ones. Whether the bodyweight field ITSELF is
    required within that step is a separate, narrower question — see
    :func:`_goal_setup_needs_bodyweight`. Before "method" is answered,
    assume the longer path so the progress bar shows a stable total rather
    than guessing low and jumping up — standards if it's actually offered,
    history otherwise.
    """
    default_method = (
        CustomGoal.SourceMethod.STANDARDS
        if standards_method_available()
        else CustomGoal.SourceMethod.HISTORY
    )
    method = data.get("method", default_method)
    steps = ["method"]
    if method in (CustomGoal.SourceMethod.STANDARDS, CustomGoal.SourceMethod.HISTORY):
        steps.append("inputs")
    steps.append("chart")
    return steps


def _goal_setup_needs_bodyweight(challenge, data):
    """Whether the "inputs" step's bodyweight field is required.

    True for standards always; for history, only when the challenge has at
    least one bodyweight-added lift configured (history needs a bodyweight
    to convert added weight to e1RM for those lifts; every other lift needs
    nothing bodyweight-shaped at all). No longer equivalent to "inputs is a
    step" (see :func:`_goal_setup_steps`) now that history's rounding
    control means "inputs" is reachable for history regardless.
    """
    method = data.get("method")
    if method == CustomGoal.SourceMethod.STANDARDS:
        return True
    if method == CustomGoal.SourceMethod.HISTORY:
        configured = covered_lift_names(challenge)
        return any(is_bodyweight_added_lift(lift) for lift in configured)
    return False


def _rep_target_goal_setup_view(request, challenge, participant):
    """Single-page Rep Target goal-setup form (issue #85).

    One row per configured lift (target weight + target reps), manual entry
    only, plus a table-wide "Suggest targets" button that re-renders the same
    form prefilled from the participant's synced history -- a convenience
    equivalent to Classic's "Compute", not a save. Reached only from
    goal_setup_view, which has already applied the terminal-challenge and
    has_goal_configured guards, so none of those are repeated here.
    """
    if request.method == "GET":
        try:
            sync_and_score(request.user, challenge)
        except OperationalError:
            logger.exception(
                "Rep target goal-setup sync/score failed for user %s challenge %s",
                request.user.id,
                challenge.pk,
            )
            messages.warning(
                request,
                gettext(
                    "Couldn't refresh your Liftosaur history just now. "
                    "Showing the history we already have."
                ),
            )

    unit = request.user.unit_preference

    if request.method == "POST":
        targets, errors = parse_rep_target_grid(request.POST, challenge, unit)

        if request.POST.get("action") == "suggest":
            suggested, no_history_lifts = suggest_rep_targets_from_history(
                request.user,
                challenge,
                lookback_days=settings.CHALLENGES_GOAL_SUGGESTION_LOOKBACK_DAYS,
                uplift=settings.CHALLENGES_GOAL_SUGGESTION_UPLIFT,
            )
            # Per-field merge: anything the participant already typed is
            # pinned, only blank fields take the suggestion -- same contract
            # as Classic's Compute button. A field a *previous* Suggest
            # filled arrives in this POST as an ordinary value, so keep its
            # marker from the round-tripped hidden input (edited fields were
            # already dropped from it client-side) or a second Suggest would
            # relabel it as typed.
            field_values, suggested_fields = merge_suggested_fields(
                request.POST, suggested, challenge, unit
            )
            suggested_fields |= parse_suggested_fields(request.POST) & set(field_values)
            if no_history_lifts:
                # A toast, not an inline grid row (UAT feedback: the extra
                # full-width row broke the grid's spacing) -- one message
                # naming every lift the suggester couldn't prefill.
                messages.warning(
                    request,
                    ngettext(
                        "No recent history for %(lifts)s. Enter a target manually.",
                        "No recent history for %(lifts)s. Enter targets manually.",
                        len(no_history_lifts),
                    )
                    % {"lifts": ", ".join(no_history_lifts)},
                )
            context = build_rep_target_goal_context(
                request.user,
                challenge,
                field_values=field_values,
                suggested_fields=suggested_fields,
                source_note=gettext(
                    "Blank fields were filled from your recent history; "
                    "values you typed were kept. Review every row before "
                    "confirming."
                ),
            )
            return render(request, "challenges/rep_target_goal_setup.html", context)

        other_errors = errors + rep_target_goal_is_complete(targets, challenge)
        if other_errors:
            # Echo the raw per-field input back (not just the rows that
            # parsed), and keep the suggested-cell styling for suggested
            # fields that still hold a value.
            field_values, _ = merge_suggested_fields(request.POST, {}, challenge, unit)
            suggested_fields = parse_suggested_fields(request.POST) & set(field_values)
            context = build_rep_target_goal_context(
                request.user,
                challenge,
                field_values=field_values,
                suggested_fields=suggested_fields,
                errors=other_errors,
            )
            return render(request, "challenges/rep_target_goal_setup.html", context)

        # Same provenance rule as Classic's wizard: a goal the history
        # suggester contributed to is HISTORY, one typed end to end is CUSTOM.
        source_method = (
            RepTargetGoal.SourceMethod.HISTORY
            if parse_suggested_fields(request.POST)
            else RepTargetGoal.SourceMethod.CUSTOM
        )
        goal_name = default_goal_name(
            source_method,
            uplift=settings.CHALLENGES_GOAL_SUGGESTION_UPLIFT
            if source_method == RepTargetGoal.SourceMethod.HISTORY
            else None,
        )
        save_rep_target_goal(
            participant,
            goal_name,
            targets,
            source_method=source_method,
        )
        # Score the pool already pulled at this view's GET entry -- local-DB
        # only -- so the leaderboard reflects the new targets immediately.
        sync_and_score(request.user, challenge, sync=False)
        activate_draft_for_creator(challenge, request.user)
        return redirect(f"/challenges/{challenge.pk}/")

    context = build_rep_target_goal_context(request.user, challenge)
    return render(request, "challenges/rep_target_goal_setup.html", context)


@never_cache
@login_required
def goal_setup_view(request, pk):
    """One-time, per-participant goal-setting wizard (TASK-248).

    Four methods — strength standards, suggested from Liftosaur history,
    manual entry, or JSON paste (TASK-306) — all materialise into the same
    flat CustomGoal/CustomGoalTarget shape (challenges.goal_builders;
    TASK-248 plan §3), so
    scoring never has to know which method produced a chart. Session-tracked,
    and every step shares this one URL -- @never_cache (UAT feedback: after
    using the in-wizard "Back" link then editing a field, "Continue" could
    land back on the method step instead of the recalculated chart) sets
    Cache-Control: no-store so a browser's history cache can never restore a
    stale rendering of one step in place of whatever the session says is
    actually current.
    namespaced by challenge pk so two concurrent joins never collide.
    Charts are locked once saved (AC#4): has_goal_configured already redirects
    away below, and save_custom_goal is create-only as defence-in-depth.
    """
    challenge = get_object_or_404(Challenge, pk=pk)
    participant = ChallengeParticipant.objects.filter(
        challenge=challenge, user=request.user
    ).first()
    if participant is None:
        logger.warning(
            "User %s attempted goal-setup for challenge %s without participating",
            request.user.id,
            challenge.pk,
        )
        raise PermissionDenied

    if challenge.is_terminal:
        logger.warning(
            "User %s attempted goal-setup for locked challenge %s (status %s)",
            request.user.id,
            challenge.pk,
            challenge.status,
        )
        messages.info(
            request, gettext("This challenge has ended; goals can no longer be set.")
        )
        return redirect(reverse("challenges:detail", args=[pk]))

    if participant.has_goal_configured:
        messages.info(
            request,
            gettext("Your chart for this challenge is locked and can't be changed."),
        )
        return redirect(f"/challenges/{challenge.pk}/")

    pk_key = str(challenge.pk)
    all_data = request.session.get(_GOAL_DATA_SESSION_KEY, {})
    all_indexes = request.session.get(_GOAL_STEP_SESSION_KEY, {})

    if request.method == "GET" and request.GET.get("cancel") == "1":
        # Logged at info level (UAT feedback: "Continue" sometimes lands
        # back at the wizard's start with no obvious cause) -- this is the
        # only code path that discards wizard progress outright, so a log
        # line here confirms or rules it out as the explanation if it
        # recurs, without needing to reproduce it live. A no-op pop for a
        # REP_TARGET participant, who never wrote these session keys.
        logger.info(
            "User %s cancelled goal-setup wizard for challenge %s at step index %s",
            request.user.id,
            challenge.pk,
            all_indexes.get(pk_key, 0),
        )
        all_data.pop(pk_key, None)
        all_indexes.pop(pk_key, None)
        request.session[_GOAL_DATA_SESSION_KEY] = all_data
        request.session[_GOAL_STEP_SESSION_KEY] = all_indexes
        return redirect(reverse("challenges:detail", args=[pk]))

    if challenge.mode == Challenge.Mode.REP_TARGET:
        # Rep Target has no strength-standards/JSON method and no multi-step
        # inputs wizard (issue #85) -- manual entry plus a "Suggest targets"
        # convenience fits on one page, so it skips the session-tracked
        # multi-step machinery entirely rather than reusing it for a wizard
        # of one real step.
        return _rep_target_goal_setup_view(request, challenge, participant)

    data = all_data.get(pk_key, {})
    index = all_indexes.get(pk_key, 0)
    # GET-only (TASK-264 UAT: "Continue" after "Back" occasionally reset to
    # the method step): goal_setup_method.html and goal_setup_inputs.html's
    # <form method="post"> have no explicit action=, so a browser submits
    # them to the current document URL -- which, right after following the
    # "Back" link, still carries ?back=1. Without this method guard, that
    # query string got reapplied to the Continue POST on top of its own
    # step-advance, double-decrementing the index and tripping the
    # wizard_step staleness guard below into bouncing back to "method".
    # ?back=1/?cancel=1 are GET-navigation links, never POST body content, so
    # gating both on request.method == "GET" is airtight regardless of what
    # a template's form action does or doesn't carry.
    if request.method == "GET" and request.GET.get("back") == "1" and index > 0:
        index -= 1
        all_indexes[pk_key] = index
        request.session[_GOAL_STEP_SESSION_KEY] = all_indexes

    if request.method == "GET" and index == 0:
        # Once per wizard entry, not per step: refresh the lifter's shared
        # LiftHistory pool (cooldown-gated API pull) then score it for this
        # challenge (local-DB only) — three steps must not mean three pulls.
        try:
            sync_and_score(request.user, challenge)
        except OperationalError:
            # Losing a race for the database write lock must not cost the user
            # the wizard (UAT: a brand-new invite-link joiner got a 500 here).
            # Fall through and render the step against whatever is already
            # pooled rather than redirecting — this is the index == 0 GET, so a
            # redirect to the same URL risks a loop.
            logger.exception(
                "Goal-setup sync/score failed for user %s challenge %s",
                request.user.id,
                challenge.pk,
            )
            messages.warning(
                request,
                gettext(
                    "Couldn't refresh your Liftosaur history just now. "
                    "Showing the history we already have."
                ),
            )

    steps = _goal_setup_steps(challenge, data)
    step = steps[index]

    # The history method needs the lifter's own pooled lift history to
    # suggest anything. Joining a challenge no longer requires a tracker
    # connection at all, and even a connected tracker (Liftosaur, Hevy, or
    # Wger -- see User.has_connected_tracker) can have zero pooled
    # LiftHistory: a fresh Wger instance, a sync that silently failed, or a
    # lookback window with nothing recent in it. Both cases would otherwise
    # silently produce an all-blank chart with no explanation, so both are
    # caught here -- with different copy on the recovery screen itself (see
    # _history_needed_response). Gated on data (not the method-step's own
    # form) so it applies once past "method" regardless of how it was
    # reached, and never blocks the method-selection step itself.
    if (
        step != "method"
        and data.get("method") == CustomGoal.SourceMethod.HISTORY
        and not LiftHistory.objects.filter(user=request.user).exists()
    ):
        if request.method == "POST" and request.POST.get("build_manually"):
            # Bail out of history suggestions into manual entry instead.
            # CUSTOM has no "inputs" step (_goal_setup_steps), so the
            # session's current index now maps straight to "chart" --
            # no index change needed, only the method itself.
            data = {**data, "method": CustomGoal.SourceMethod.CUSTOM}
            all_data[pk_key] = data
            request.session[_GOAL_DATA_SESSION_KEY] = all_data
            logger.info(
                "User %s opted out of history suggestions into manual "
                "entry for challenge %s",
                request.user.id,
                challenge.pk,
            )
            return redirect(reverse("challenges:goal-setup", args=[pk]))

        recovery_error = _ensure_history_source(request)
        if recovery_error is not None:
            return _history_needed_response(
                request,
                challenge,
                tracker_connected=request.user.has_connected_tracker,
                error=recovery_error or None,
                action_url=reverse("challenges:goal-setup", args=[pk]),
                cancel_url=reverse("challenges:goal-setup", args=[pk]) + "?cancel=1",
            )
        # recovery_error is None here means a credential was just
        # connected (with its initial pull already run synchronously
        # inside _ensure_history_source -- see that function's docstring
        # for why it must not be the usual fire-and-forget backfill
        # thread) or a CSV import landed rows. Redirect to a fresh GET so
        # the actual step renders normally instead of trying to parse
        # this POST as that step's own form. If the pull actually pooled
        # nothing (an empty tracker), the guard above fires again on that
        # GET -- now with tracker_connected=True, so the recovery screen
        # explains the tracker is connected but empty, not disconnected.
        return redirect(reverse("challenges:goal-setup", args=[pk]))

    posted_step = request.POST.get("wizard_step")
    if request.method == "POST" and posted_step and posted_step != step:
        # A stale page was resubmitted -- most commonly the browser's own
        # Back button, which can show a cached render of an earlier (or
        # since-skipped) step while the session has already moved past it.
        # All three step templates carry a hidden wizard_step field matching
        # what they were rendered for, so a genuine stale page presents a
        # mismatched (not missing) value; a request with no wizard_step
        # field at all (a script, a test, an old pre-this-fix cached page)
        # is left to the normal per-step dispatch below, unchanged. Without
        # this, a mismatched POST's fields get parsed as the CURRENT step's
        # own submission and produce confusing validation errors on fields
        # the user never saw (UAT feedback) -- drop it and re-render
        # whatever step is actually current instead.
        return redirect(reverse("challenges:goal-setup", args=[pk]))

    def _save_step(new_data, *, advance=True):
        all_data[pk_key] = new_data
        request.session[_GOAL_DATA_SESSION_KEY] = all_data
        if advance:
            all_indexes[pk_key] = index + 1
            request.session[_GOAL_STEP_SESSION_KEY] = all_indexes
        return redirect(reverse("challenges:goal-setup", args=[pk]))

    step_context = {
        "challenge": challenge,
        "step": step,
        "step_number": index + 1,
        "total_steps": len(steps),
    }

    if step == "method":
        return _goal_setup_method_step(
            request, data, on_valid=_save_step, step_context=step_context
        )
    if step == "inputs":
        needs_bodyweight = _goal_setup_needs_bodyweight(challenge, data)
        return _goal_setup_inputs_step(
            request,
            data,
            needs_bodyweight=needs_bodyweight,
            on_valid=_save_step,
            step_context=step_context,
        )
    return _goal_setup_chart_step(
        request, challenge, participant, data, step_context=step_context
    )


_COMPUTE_LOG_MAX_ENTRIES = 200


@login_required
@require_POST
def goal_setup_compute_log_view(request, pk):
    """Fire-and-forget sink for the manual-grid Compute button's own stats.

    The multi-formula, multi-anchor blend it runs (TASK-306 follow-up) is
    entirely client-side JS -- this endpoint exists purely so that math has
    somewhere to land as structured, SigNoz-queryable log lines, so a future
    "my numbers looked wrong" report has raw anchors and per-formula results
    to reconstruct from, not just a bug report with no data behind it. It
    does not read or write anything else -- no DB row, no response body
    beyond the status code -- so a malformed or truncated payload is a
    logging gap, not a broken request; failures here must never surface to
    the user or block their goal-setup flow.

    Only requires participation (not the fuller goal_setup_view guard
    ladder -- no terminal-challenge/has_goal_configured checks) since this
    never mutates challenge state; it only observes math a participant just
    ran on their own screen.
    """
    challenge = get_object_or_404(Challenge, pk=pk)
    participant_exists = ChallengeParticipant.objects.filter(
        challenge=challenge, user=request.user
    ).exists()
    if not participant_exists:
        logger.warning(
            "User %s attempted to log Compute stats for challenge %s without "
            "participating",
            request.user.id,
            pk,
        )
        raise PermissionDenied

    try:
        entries = json.loads(request.body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        logger.warning(
            "Discarding unparsable Compute-log payload for challenge %s from user %s",
            pk,
            request.user.id,
        )
        return HttpResponse(status=204)

    if not isinstance(entries, list):
        logger.warning(
            "Discarding non-list Compute-log payload for challenge %s from user %s",
            pk,
            request.user.id,
        )
        return HttpResponse(status=204)

    for entry in entries[:_COMPUTE_LOG_MAX_ENTRIES]:
        if not isinstance(entry, dict):
            continue
        logger.info(
            "Goal-setup compute run",
            extra={
                "challenge_id": pk,
                "lift_name": entry.get("lift_name"),
                "target_rep": entry.get("target_rep"),
                "method": entry.get("method"),
                "anchors": entry.get("anchors"),
                "formula_spread_kg": entry.get("formula_spread_kg"),
                "anchor_spread_kg": entry.get("anchor_spread_kg"),
                "pre_clamp_kg": entry.get("pre_clamp_kg"),
                "blended_kg": entry.get("blended_kg"),
                "rounding_increment_kg": entry.get("rounding_increment_kg"),
                "rounded_kg": entry.get("rounded_kg"),
            },
        )
    return HttpResponse(status=204)


def _goal_setup_method_step(request, data, *, on_valid, step_context):
    standards_available = standards_method_available()
    if request.method == "POST":
        form = GoalMethodForm(request.POST, standards_available=standards_available)
        if form.is_valid():
            data = {**data, "method": form.cleaned_data["method"]}
            return on_valid(data)
    else:
        form = GoalMethodForm(
            initial={"method": data.get("method", "")},
            standards_available=standards_available,
        )

    return render(
        request,
        "challenges/goal_setup_method.html",
        {**step_context, "form": form, "standards_available": standards_available},
    )


def _goal_setup_inputs_step(request, data, *, needs_bodyweight, on_valid, step_context):
    method = data["method"]
    unit = request.user.unit_preference
    if request.method == "POST":
        form = GoalInputsForm(
            request.POST,
            method=method,
            needs_bodyweight=needs_bodyweight,
            unit=unit,
        )
        if form.is_valid():
            new_data = {**data}
            if form.cleaned_data["bodyweight_kg"] is not None:
                new_data["bodyweight_kg"] = str(form.cleaned_data["bodyweight_kg"])
            if method == CustomGoal.SourceMethod.STANDARDS:
                new_data["sex"] = form.cleaned_data["sex"]
                new_data["population"] = form.cleaned_data["population"]
                new_data["snapshot_version"] = form.cleaned_data["snapshot_version"]
                new_data["tier"] = form.cleaned_data["tier"]
            if method == CustomGoal.SourceMethod.HISTORY:
                new_data["uplift"] = str(form.cleaned_data["uplift"])
            if form.needs_rounding:
                # The token ("none"/"kg:2.5"/"lb:5"), not just the resolved
                # amount, so a later "Back" round trip can echo the exact
                # choice back as this step's initial.
                new_data["rounding_increment"] = form.cleaned_data["rounding_increment"]
                if form.cleaned_data["rounding_amount"] is not None:
                    new_data["rounding_amount"] = str(
                        form.cleaned_data["rounding_amount"]
                    )
                    new_data["rounding_unit"] = form.cleaned_data["rounding_unit"]
                else:
                    new_data.pop("rounding_amount", None)
                    new_data.pop("rounding_unit", None)
            return on_valid(new_data)
    else:
        display_bodyweight = ""
        stored_bodyweight_kg = data.get("bodyweight_kg")
        if stored_bodyweight_kg:
            display_value, _ = to_display_weight(Decimal(stored_bodyweight_kg), unit)
            display_bodyweight = display_value
        stored_uplift = data.get("uplift")
        display_uplift_percent = (
            Decimal(stored_uplift) * 100
            if stored_uplift
            else settings.CHALLENGES_GOAL_SUGGESTION_UPLIFT * 100
        )
        form = GoalInputsForm(
            method=method,
            needs_bodyweight=needs_bodyweight,
            unit=unit,
            initial={
                # Re-entering this step (e.g. via "Back", or after switching
                # method and back again) must not lose a bodyweight already
                # given this session, redisplayed in the account's own
                # unit_preference -- no separate per-field unit selector to
                # drift out of sync with it (TASK-248 UAT feedback).
                "bodyweight": display_bodyweight,
                "sex": data.get("sex", ""),
                "population": data.get("population", ""),
                "tier": data.get("tier", ""),
                "rounding_increment": data.get("rounding_increment", ""),
                "uplift_percent": display_uplift_percent,
            },
        )

    return render(
        request,
        "challenges/goal_setup_inputs.html",
        {**step_context, "form": form, "method": method},
    )


def _goal_setup_chart_step(request, challenge, participant, data, *, step_context):
    method = data["method"]
    unit = request.user.unit_preference
    raw_bodyweight_kg = data.get("bodyweight_kg")
    bodyweight_kg = Decimal(raw_bodyweight_kg) if raw_bodyweight_kg else None
    raw_rounding_amount = data.get("rounding_amount")
    rounding_amount = Decimal(raw_rounding_amount) if raw_rounding_amount else None
    rounding_unit = data.get("rounding_unit") or "kg"
    raw_uplift = data.get("uplift")
    uplift = (
        Decimal(raw_uplift)
        if raw_uplift
        else Decimal(str(settings.CHALLENGES_GOAL_SUGGESTION_UPLIFT))
    )
    # Fed to default_goal_name() at save time below, so the saved goal's name
    # describes how its chart was built (standards tier, or history uplift).
    method_kwargs = (
        {"tier": data.get("tier"), "population": data.get("population")}
        if method == CustomGoal.SourceMethod.STANDARDS
        else {"uplift": uplift}
        if method == CustomGoal.SourceMethod.HISTORY
        else {}
    )

    if request.method == "POST":
        form = CustomGoalForm(
            request.POST,
            challenge=challenge,
            unit=unit,
            method=method,
            method_kwargs=method_kwargs,
        )
        if form.is_valid():
            if method == CustomGoal.SourceMethod.STANDARDS:
                source_detail = standards_source_detail(
                    population=data["population"],
                    snapshot_version=data["snapshot_version"],
                    tier=data["tier"],
                    sex=data["sex"],
                    bodyweight_kg=bodyweight_kg,
                    rounding_amount=rounding_amount,
                    rounding_unit=rounding_unit,
                )
            elif method == CustomGoal.SourceMethod.HISTORY:
                source_detail = history_source_detail(
                    uplift=uplift,
                    lookback_days=settings.CHALLENGES_GOAL_SUGGESTION_LOOKBACK_DAYS,
                    rounding_amount=rounding_amount,
                    rounding_unit=rounding_unit,
                )
            else:
                source_detail = {}
            save_custom_goal(
                participant,
                default_goal_name(method, **method_kwargs),
                form.targets,
                source_method=method,
                source_detail=source_detail,
            )
            # Score the pool already pulled at wizard entry — local-DB only —
            # so the leaderboard reflects the new targets immediately.
            sync_and_score(request.user, challenge, sync=False)
            activate_draft_for_creator(challenge, request.user)
            pk_key = str(challenge.pk)
            all_data = request.session.get(_GOAL_DATA_SESSION_KEY, {})
            all_data.pop(pk_key, None)
            request.session[_GOAL_DATA_SESSION_KEY] = all_data
            all_indexes = request.session.get(_GOAL_STEP_SESSION_KEY, {})
            all_indexes.pop(pk_key, None)
            request.session[_GOAL_STEP_SESSION_KEY] = all_indexes
            return redirect(f"/challenges/{challenge.pk}/")
        context = build_custom_goal_context(
            request.user,
            challenge,
            method=method,
            targets_json=form.data.get("targets_json", ""),
            targets=form.targets,
            errors=form.banner_errors(),
            unknown_lifts=form.unknown_lifts,
            acknowledge_unknown_lifts=bool(form.data.get("acknowledge_unknown_lifts")),
            computed_fields=set(
                filter(None, form.data.get("computed_fields", "").split(","))
            ),
        )
        context.update(step_context)
        return render(request, "challenges/custom_goal_setup.html", context)

    unavailable_lifts: set[str] = set()
    assisted_only_lifts: set[str] = set()
    targets = None
    source_note = ""
    if method == CustomGoal.SourceMethod.STANDARDS:
        targets, unavailable_lifts = suggest_from_standards(
            challenge,
            population=data["population"],
            snapshot_version=data["snapshot_version"],
            sex=data["sex"],
            bodyweight_kg=bodyweight_kg,
            tier=data["tier"],
            rounding_amount=rounding_amount,
            rounding_unit=rounding_unit,
        )
        unavailable_lifts = set(unavailable_lifts)
        source_note = gettext(
            "Prefilled from FitnessVolt strength standards. Review every "
            "row before confirming — this chart is locked once you save it."
        )
    elif method == CustomGoal.SourceMethod.HISTORY:
        targets, needs_decision, assisted_only_lifts = suggest_from_history(
            request.user,
            challenge,
            bodyweight_kg=bodyweight_kg,
            uplift=uplift,
            lookback_days=settings.CHALLENGES_GOAL_SUGGESTION_LOOKBACK_DAYS,
            rounding_amount=rounding_amount,
            rounding_unit=rounding_unit,
        )
        unavailable_lifts = set(needs_decision)
        assisted_only_lifts = set(assisted_only_lifts)
        source_note = gettext(
            "Prefilled from your recent lift history, uplifted {percent}%. "
            "Review every row before confirming — this chart is locked once "
            "you save it."
        ).format(percent=f"{float(uplift * 100):g}")

    context = build_custom_goal_context(
        request.user,
        challenge,
        method=method,
        targets=targets,
        unavailable_lifts=unavailable_lifts,
        assisted_only_lifts=assisted_only_lifts,
        source_note=source_note,
    )
    context.update(step_context)
    return render(request, "challenges/custom_goal_setup.html", context)


def _require_challenge_member(request, pk):
    """Return (challenge, participant) for a member, else raise PermissionDenied.

    A member is a non-bailed ``ChallengeParticipant`` row for ``request.user``
    with ``invite_status == ACCEPTED``. A departed member is denied
    unconditionally: they used to keep read access to OPEN challenges, but that
    carve-out was retired along with open visibility itself (TASK-272 — every
    challenge is invite-only now, so there is no public reading left to be
    consistent with).
    Extracted from the detail view's original guard so other views needing the
    same viewer boundary (e.g. the co-participant chart view) share one
    implementation rather than a copy that could drift.
    """
    challenge = get_object_or_404(Challenge.objects.select_related("creator"), pk=pk)

    participant = ChallengeParticipant.objects.filter(
        challenge=challenge, user=request.user
    ).first()
    is_accepted = (
        participant is not None
        and participant.invite_status == ChallengeParticipant.InviteStatus.ACCEPTED
    )
    if not is_accepted or participant.is_bailed:
        logger.warning(
            "User %s denied access to challenge %s detail "
            "(participant=%s, invite_status=%s)",
            request.user.id,
            challenge.pk,
            participant is not None,
            participant.invite_status if participant else None,
        )
        raise PermissionDenied
    return challenge, participant


@login_required
def challenge_detail_view(request, pk):
    """Challenge detail page. Refreshes tracker data on visit.

    On every visit the requesting user is synced first so their own leaderboard
    position reflects the latest workout data, then every other active
    (accepted, non-bailed) participant is synced.

    Sync is run synchronously: "Synchronous for MVP; revisit with Celery if p95
    latency exceeds 3s in production." A typical friend group is 5-8 people and
    each sync is dominated by a couple of tracker API calls, which keeps the
    request comfortably under the 3s threshold at MVP scale in the common case.
    A task queue would add Redis + worker infrastructure that is not justified
    at this scale. PARTICIPANT_SYNC_BUDGET_SECONDS is the backstop for the
    uncommon case -- a challenge where many participants are simultaneously
    outside their sync cooldown -- so this loop's worst case stays well short
    of the 60s gunicorn worker timeout regardless of participant count.

    Renders the challenge header, the requesting user's goal tier, and the
    leaderboard.
    """
    challenge, participant = _require_challenge_member(request, pk)

    needs_goal_setup = (
        not participant.has_goal_configured
        and not participant.is_bailed
        and not challenge.is_terminal
    )
    if needs_goal_setup:
        logger.info(
            "Redirecting user %s to goal setup for challenge %s (no goal configured)",
            request.user.id,
            challenge.pk,
        )
        messages.info(request, gettext("Set your goal to view this challenge."))
        return redirect(reverse("challenges:goal-setup", args=[pk]))

    # Fetched once and reused for both the sync loop (excluding request.user,
    # who is synced separately above it in participants_to_score) and the
    # leaderboard's chart_url lookup below — a single query rather than one
    # queryset for "others" and a second per-row lookup for the map.
    accepted_participants = list(
        challenge.participants.filter(
            invite_status=ChallengeParticipant.InviteStatus.ACCEPTED,
            is_bailed=False,
        ).select_related("user")
    )
    others = [p for p in accepted_participants if p.user_id != request.user.id]
    participant_by_user_id = {p.user_id: p for p in accepted_participants}

    is_locked = challenge.is_terminal

    # Compose the two decoupled steps for the requesting user and every other
    # accepted, non-bailed participant. The cooldown-gated pull only runs while
    # the challenge is live (a locked challenge takes no writes), but scoring
    # runs on EVERY detail open regardless of whether a fresh pull happened — it
    # is local-DB only and cheap, and a locked challenge's ledger is already a
    # no-op inside process_scored_set. This greedy scoring keeps the leaderboard
    # current without coupling display to the API cooldown.
    #
    # A wall-clock budget (see PARTICIPANT_SYNC_BUDGET_SECONDS) additionally
    # caps how much of this loop is spent on fresh pulls, independent of
    # participant count -- once it's used up, remaining participants are still
    # scored, just from whatever is already pooled rather than a fresh pull.
    participants_to_score = [request.user, *(other.user for other in others)]
    sync_deadline = time.monotonic() + PARTICIPANT_SYNC_BUDGET_SECONDS
    budget_exhausted_logged = False
    for participant_user in participants_to_score:
        within_budget = time.monotonic() < sync_deadline
        if not within_budget and not budget_exhausted_logged:
            logger.warning(
                "Challenge %s detail sync budget (%ss) exhausted; scoring "
                "remaining participants from already-pooled data without a "
                "fresh pull",
                challenge.pk,
                PARTICIPANT_SYNC_BUDGET_SECONDS,
            )
            budget_exhausted_logged = True
        try:
            sync_and_score(
                participant_user, challenge, sync=not is_locked and within_budget
            )
        except Exception:
            # One participant's tracker must never take down a page shared by
            # the whole challenge. sync_and_score's own service layer degrades
            # on the failures it knows about (API errors, network errors, DB
            # contention), so reaching here means something genuinely
            # unexpected -- log it with the participant attached and score the
            # rest of the field from whatever is already pooled.
            logger.exception(
                "Skipping participant %s on detail view of challenge %s: "
                "sync/score raised",
                participant_user.id,
                challenge.pk,
            )

    logger.info(
        "Scored %s participant(s) on detail view of challenge %s for user %s",
        len(participants_to_score),
        challenge.pk,
        request.user.id,
    )

    ranked_entries = rank_participants(challenge, include_unscored=True)
    # See the matching comment in _render_invite_accept: nobody-has-scored-yet
    # means every dense rank is a meaningless tie, so it renders as "-".
    show_ranks = any(entry["total_points"] > 0 for entry in ranked_entries)

    leaderboard = []
    for entry in ranked_entries:
        entry_user = entry["user"]
        is_self = entry_user.pk == request.user.pk
        chart_url = None
        name = entry_user.effective_display_name
        if entry_user.is_active:
            entry_participant = participant_by_user_id.get(entry_user.pk)
            if entry_participant is not None and not is_self:
                chart_url = reverse(
                    "challenges:participant-chart",
                    args=[challenge.pk, entry_participant.pk],
                )
        # else: no chart_url -- participant_chart_view itself requires
        # user__is_active=True, so a link here would just 404. Deactivated
        # (self-serve-deleted) users already show under their generated
        # pseudonym with a "(deleted)" suffix (name computed above via
        # effective_display_name); there's no separate identity left to mask.
        leaderboard.append(
            {
                "rank": entry["rank"] if show_ranks else "-",
                "name": name,
                "total_points": entry["total_points"],
                "is_self": is_self,
                "chart_url": chart_url,
            }
        )

    chart_data = build_points_over_time(challenge)
    by_lift_data = build_points_by_lift(challenge)
    recent_activity = build_recent_scoring_activity(challenge, request.user)

    personal_data = build_personal_data(request.user, challenge, participant)

    context = {
        "challenge": challenge,
        "participant": participant,
        "leaderboard": leaderboard,
        "chart_data": chart_data,
        "by_lift_data": by_lift_data,
        "recent_activity": recent_activity,
        "personal_data": personal_data,
        "is_rep_target_mode": challenge.mode == Challenge.Mode.REP_TARGET,
        # A terminal challenge is fully read-only: the summary cards drop their
        # self-report face and the flip affordance with it. Matches the server
        # guard in manual_lift_view/manual_rep_target_view exactly. Bailed
        # participants need no separate flag -- bailing detaches the goal, so
        # build_personal_data returns None and no cards render at all.
        "can_self_report": not is_locked,
        "last_synced_at": last_synced_at(request.user),
        "mobile_header_title": challenge.name,
    }

    return render(request, "challenges/detail.html", context)


@login_required
@require_GET
def participant_chart_view(request, pk, participant_pk):
    """Read-only view of a co-participant's locked goal chart (TASK-252).

    Viewer boundary matches challenge_detail_view exactly (membership guard
    shared via _require_challenge_member). Subject boundary mirrors the
    convention every existing cross-participant view already agrees on
    (leaderboard/points-by-lift/others): ACCEPTED, not bailed, and active —
    scoped to this challenge so a participant_pk from another challenge can
    never be read through a challenge the viewer happens to also be in.

    No sync_and_score call: the detail page already synced everyone on the
    visit that produced this link, and this is a cheap fragment fetch that
    must not reintroduce that latency (D7).
    """
    challenge, _viewer_participant = _require_challenge_member(request, pk)

    subject = get_object_or_404(
        ChallengeParticipant.objects.select_related("user", "custom_goal"),
        pk=participant_pk,
        challenge=challenge,
        invite_status=ChallengeParticipant.InviteStatus.ACCEPTED,
        is_bailed=False,
        user__is_active=True,
    )

    chart = build_participant_chart(request.user, challenge, subject)

    logger.info(
        "User %s viewed participant %s's chart in challenge %s",
        request.user.id,
        subject.pk,
        challenge.pk,
    )

    context = {
        "challenge": challenge,
        "subject": subject,
        "chart": chart,
        "mobile_header_title": challenge.name,
    }
    if is_htmx(request):
        return render(request, "challenges/_participant_chart.html", context)
    return render(request, "challenges/participant_chart.html", context)


@login_required
@require_POST
def manual_lift_view(request, pk):
    """Self-report a completed set from the Summary tab's flip-card (TASK-25).

    Membership guard matches every other personal-performance surface
    (``_require_challenge_member``): self-report still requires
    ``has_goal_configured``, same as the rest of "Your Performance" — this does
    not solve pre-goal-setup onboarding, an accepted scope boundary.

    Always returns the single affected card's HTML fragment (this endpoint is
    only ever called by the flip-card's own htmx form), on its front face and
    now showing the improved score, plus an out-of-band Django message naming
    the points earned and the lift.

    There is no "logged but didn't beat your best" outcome: ``submit_manual_lift``
    refuses any set that cannot raise the participant's score, so reaching the
    success path always means the lift's best moved. The carousel disables
    those entries anyway, so the 400 below is for a stale card or a hand-made
    request, not a route the UI can walk into.

    A COMPLETED/CANCELLED challenge is read-only: the detail page stops
    rendering the self-report card entirely (``can_self_report``), so a POST
    here means a page that was already open when the challenge closed, or a
    hand-made request. It is rejected before any write -- without this guard
    the LiftHistory row was still written and then scored nothing (the ledger
    lock in ``scoring.services.process_scored_set``), reporting a bogus
    "Logged 0 points" back to the lifter.
    """
    challenge, participant = _require_challenge_member(request, pk)

    response = _terminal_status_response(
        request,
        challenge,
        gettext("This challenge has ended; no further sets can be logged."),
        action="self-report a lift in",
    )
    if response is not None:
        return response

    if not participant.has_goal_configured:
        raise PermissionDenied

    lift = request.POST.get("lift", "")
    try:
        rep_count = int(request.POST.get("rep_count", ""))
    except (TypeError, ValueError):
        return HttpResponseBadRequest(gettext("Invalid rep count."))

    try:
        performed_at = date.fromisoformat(request.POST.get("performed_at", ""))
    except ValueError:
        return HttpResponseBadRequest(gettext("Invalid date."))
    if performed_at > date.today():
        return HttpResponseBadRequest(gettext("Date cannot be in the future."))

    result = submit_manual_lift(
        user=request.user,
        challenge=challenge,
        participant=participant,
        lift=lift,
        rep_count=rep_count,
        performed_at=performed_at,
    )
    if result is None:
        return HttpResponseBadRequest(gettext("Could not log this lift."))
    _history_row, points_earned = result

    messages.success(
        request,
        ngettext(
            "Logged %(points)s point on %(lift)s.",
            "Logged %(points)s points on %(lift)s.",
            points_earned,
        )
        % {"points": points_earned, "lift": lift},
    )

    personal_data = build_personal_data(request.user, challenge, participant)
    card = next((c for c in personal_data["summary_cards"] if c["lift"] == lift), None)
    if card is None:
        return HttpResponseBadRequest(gettext("Unknown lift for this challenge."))

    logger.info(
        "User %s self-reported %s %sRM in challenge %s for %s point(s)",
        request.user.id,
        lift,
        rep_count,
        challenge.pk,
        points_earned,
    )

    context = {
        "card": card,
        "display_unit": personal_data["display_unit"],
        "challenge": challenge,
        "start_rep_count": rep_count,
        "can_self_report": True,
        "oob_messages": True,
    }
    return render(request, "challenges/_summary_card.html", context)


@login_required
@require_POST
def manual_rep_target_view(request, pk):
    """Self-report a completed set from the REP_TARGET Summary tab's flip-card
    (issue #85 follow-up) -- the REP_TARGET sibling of ``manual_lift_view``.

    Same membership/``has_goal_configured`` guard, same "always returns the
    single affected card's fragment plus an OOB message" response shape, and
    the same "carousel disables non-improving entries, so a 400 here means a
    stale card or a hand-made request" caveat as Classic. The one input
    difference: Rep Target's carousel varies reps, not weight, so there is no
    weight field here -- ``submit_manual_rep_target_set`` always logs at the
    goal's own fixed ``target_weight``.

    Same read-only guard on a COMPLETED/CANCELLED challenge as Classic.
    """
    challenge, participant = _require_challenge_member(request, pk)

    response = _terminal_status_response(
        request,
        challenge,
        gettext("This challenge has ended; no further sets can be logged."),
        action="self-report a rep target set in",
    )
    if response is not None:
        return response

    if not participant.has_goal_configured:
        raise PermissionDenied

    lift = request.POST.get("lift", "")
    try:
        rep_count = int(request.POST.get("rep_count", ""))
    except (TypeError, ValueError):
        return HttpResponseBadRequest(gettext("Invalid rep count."))
    if rep_count < 1 or rep_count > MAX_TARGET_REPS:
        return HttpResponseBadRequest(gettext("Invalid rep count."))

    try:
        performed_at = date.fromisoformat(request.POST.get("performed_at", ""))
    except ValueError:
        return HttpResponseBadRequest(gettext("Invalid date."))
    if performed_at > date.today():
        return HttpResponseBadRequest(gettext("Date cannot be in the future."))

    result = submit_manual_rep_target_set(
        user=request.user,
        challenge=challenge,
        participant=participant,
        lift=lift,
        rep_count=rep_count,
        performed_at=performed_at,
    )
    if result is None:
        return HttpResponseBadRequest(gettext("Could not log this set."))
    _history_row, points_earned = result

    messages.success(
        request,
        ngettext(
            "Logged %(points)s point on %(lift)s.",
            "Logged %(points)s points on %(lift)s.",
            points_earned,
        )
        % {"points": points_earned, "lift": lift},
    )

    personal_data = build_rep_target_personal_data(request.user, challenge, participant)
    card = next((c for c in personal_data["summary_cards"] if c["lift"] == lift), None)
    if card is None:
        return HttpResponseBadRequest(gettext("Unknown lift for this challenge."))

    logger.info(
        "User %s self-reported %s reps on %s in challenge %s for %s point(s)",
        request.user.id,
        rep_count,
        lift,
        challenge.pk,
        points_earned,
    )

    context = {
        "card": card,
        "display_unit": personal_data["display_unit"],
        "challenge": challenge,
        "start_rep_count": rep_count,
        "can_self_report": True,
        "oob_messages": True,
    }
    return render(request, "challenges/_rep_target_summary_card.html", context)


@login_required
def challenge_settings_view(request, pk):
    """Creator/staff settings page: manage participants, the invite link, and
    the challenge's lifecycle actions.

    Staff get access (``allow_staff=True``) so a moderator can rescue a
    challenge — mirroring the staff override on cancel and transfer, and
    matching the Cancel button's pre-existing staff visibility on detail.html
    before it moved here.
    """
    challenge = _get_challenge_for_creator(request, pk, allow_staff=True)
    context = _participants_section_context(challenge)
    context["rename_form"] = RenameChallengeForm(initial={"name": challenge.name})
    context["rename_editing"] = False
    context["history_window_form"] = HistoryWindowForm(
        initial={"history_window": challenge.history_window}
    )
    context["history_window_editing"] = False
    return render(request, "challenges/settings.html", context)


def _get_own_participant(request, pk):
    """Return the requesting user's own participant row for a challenge, or 403.

    The viewer boundary for acting on your own membership (``bail_view``) —
    looser than :func:`_require_challenge_member`, which additionally demands
    ACCEPTED and non-bailed; the state checks bail needs are its own.
    """
    challenge = get_object_or_404(Challenge, pk=pk)
    participant = ChallengeParticipant.objects.filter(
        challenge=challenge, user=request.user
    ).first()
    if participant is None:
        logger.warning(
            "User %s attempted to act on their membership of challenge %s "
            "without being a participant",
            request.user.id,
            challenge.pk,
        )
        raise PermissionDenied
    return participant


@login_required
def bail_view(request, pk):
    """Leave a challenge. GET confirms; POST freezes the participant's ledger."""
    participant = _get_own_participant(request, pk)
    challenge = participant.challenge

    # is_terminal, not status == COMPLETED: a CANCELLED challenge is equally
    # read-only, and bailing from one would still detach the participant's
    # locked goal and stamp bailed_at on a dead challenge's row.
    if challenge.is_terminal:
        logger.warning(
            "User %s tried to bail from %s challenge %s",
            request.user.id,
            challenge.status,
            pk,
        )
        return HttpResponseBadRequest(gettext("This challenge has already ended."))

    if (
        participant.invite_status != ChallengeParticipant.InviteStatus.ACCEPTED
        or participant.is_bailed
    ):
        logger.warning(
            "User %s tried to bail from challenge %s in invalid state "
            "(invite_status=%s, is_bailed=%s)",
            request.user.id,
            pk,
            participant.invite_status,
            participant.is_bailed,
        )
        return HttpResponseBadRequest(gettext("You cannot leave this challenge."))

    if request.method == "POST":
        participant.is_bailed = True
        participant.bailed_at = datetime.now(tz=UTC)
        detach_active_goal(participant)
        detach_active_rep_target_goal(participant)
        participant.save(
            update_fields=["is_bailed", "bailed_at", "custom_goal", "rep_target_goal"]
        )
        logger.info("User %s bailed from challenge %s", request.user.id, pk)
        messages.success(request, gettext("You have left the challenge."))
        return redirect("challenges:dashboard")

    return _render_confirm_action(
        request,
        challenge,
        title=gettext("leave challenge"),
        verb=gettext("leave"),
        detail=gettext(
            "Leaving freezes your scoring — no further points will be earned "
            "for you in this challenge. Your past entries stay visible. This "
            "action cannot be undone."
        ),
        action_url=reverse("challenges:bail", args=[challenge.pk]),
        submit_label=gettext("leave challenge"),
        cancel_url=reverse("challenges:dashboard"),
    )


def _get_challenge_for_creator(request, pk, *, allow_staff=False):
    """Return a challenge the requesting user created, or raise 403.

    When ``allow_staff`` is True a staff user passes the check even when they did
    not create the challenge — the moderation/rescue override used by cancel,
    remove, and ownership transfer. Actions without a staff override (close, and
    the invite-link share/regenerate pair, which hand out a join capability
    rather than moderate) keep the default.
    """
    challenge = get_object_or_404(Challenge, pk=pk)
    if challenge.creator_id != request.user.id and not (
        allow_staff and request.user.is_staff
    ):
        logger.warning(
            "User %s denied creator action on challenge %s "
            "(creator=%s, allow_staff=%s)",
            request.user.id,
            challenge.pk,
            challenge.creator_id,
            allow_staff,
        )
        raise PermissionDenied
    return challenge


def _terminal_status_response(request, challenge, message, *, action):
    """400 + warning log when the challenge is COMPLETED/CANCELLED, else None.

    ``action`` is the verb phrase spliced into the shared log line so each call
    site's existing log output is preserved byte-for-byte.
    """
    if not challenge.is_terminal:
        return None
    logger.warning(
        "User %s tried to %s challenge %s in status %s",
        request.user.id,
        action,
        challenge.pk,
        challenge.status,
    )
    return HttpResponseBadRequest(message)


def _ended_invite_link_response(request, challenge, message, *, action):
    """400 + warning log once ``challenge.end_date`` has actually passed, else None.

    Additional to (not a replacement for) ``_terminal_status_response``: status
    only flips to COMPLETED/CANCELLED once close_challenges actually runs, and
    there's a real window where end_date has passed but status is still
    ACTIVE. Invite-link creation/regeneration/update must be blocked for that
    window too, so this checks the live instant directly rather than trusting
    ``is_terminal``.
    """
    if timezone.now() < challenge_end_instant(challenge):
        return None
    logger.warning(
        "User %s tried to %s challenge %s after its end_date (%s) had passed",
        request.user.id,
        action,
        challenge.pk,
        challenge.end_date,
    )
    return HttpResponseBadRequest(message)


def _render_confirm_action(
    request,
    challenge,
    *,
    title,
    verb,
    prompt_suffix="",
    detail,
    action_url,
    submit_label,
    cancel_url,
    cancel_label=None,
):
    """Render the shared confirm-action page for a destructive challenge action."""
    return render(
        request,
        "challenges/confirm_action.html",
        {
            "challenge": challenge,
            "confirm_title": title,
            "confirm_verb": verb,
            "confirm_prompt_suffix": prompt_suffix,
            "confirm_detail": detail,
            "confirm_action_url": action_url,
            "confirm_submit_label": submit_label,
            "confirm_cancel_url": cancel_url,
            "confirm_cancel_label": cancel_label or gettext("cancel"),
        },
    )


def _invite_link_form_for(link):
    """An InviteLinkOptionsForm pre-filled from ``link``'s current values.

    Without this, the Custom expiry/Max uses inputs always render blank next
    to a live link's "Expires in N days" text, which reads as if the two are
    unrelated quantities -- they're the same field, one shown as a relative
    countdown and the other as an editable absolute value. ``link`` may be
    ``None`` (no live link yet), in which case the form is left blank.
    """
    if link is None:
        return InviteLinkOptionsForm()
    return InviteLinkOptionsForm(
        initial={
            "expires_at": (
                timezone.localtime(link.expires_at) if link.expires_at else None
            ),
            "max_uses": link.max_uses,
            "never_expires": link.expires_at is None,
        },
        challenge=link.challenge,
    )


def _participants_section_context(challenge):
    """Context for the Settings page's creator-only sections.

    ``participant_rows`` is every participant row regardless of status, except
    bailed rows — voluntarily-left members and creator-removed members alike are
    erased from this list once they leave (TASK-199). Legacy INVITED/DECLINED
    rows can still appear (nothing creates them any more, but old rows survive),
    which is why this builds its own list rather than reusing the detail view's
    accepted-only ``others`` queryset. Deactivated (self-serve-deleted) users
    show under their generated pseudonym with a "(deleted)" suffix
    (User.effective_display_name), matching the leaderboard.

    ``can_remove``/``can_become_owner`` gate the accepted-participant actions.

    ``current_invite_link`` also lives here (not its own helper) because every
    non-htmx caller of this function (challenge_settings_view,
    rename_challenge_view, history_window_view) re-renders the full Settings
    page and would otherwise need to remember to add it separately — a small
    drift risk this avoids (TASK-249).
    """
    rows = challenge.participants.select_related("user").order_by("created_at")
    participant_rows = [
        {
            "pk": row.pk,
            "name": row.user.effective_display_name,
            "invite_status": row.invite_status,
            "user_id": row.user_id,
            "can_remove": (
                row.invite_status == ChallengeParticipant.InviteStatus.ACCEPTED
                and not row.is_bailed
                and row.user_id != challenge.creator_id
            ),
            "can_become_owner": (
                row.invite_status == ChallengeParticipant.InviteStatus.ACCEPTED
                and not row.is_bailed
                and row.user.is_active
                and row.user_id != challenge.creator_id
            ),
        }
        for row in rows
        if not row.is_bailed
    ]
    link = current_invite_link(challenge)
    return {
        "challenge": challenge,
        "participant_rows": participant_rows,
        "is_locked": challenge.is_terminal,
        "invite_link_locked": (
            challenge.is_terminal or timezone.now() >= challenge_end_instant(challenge)
        ),
        "current_invite_link": link,
        "invite_link_form": _invite_link_form_for(link),
        "invite_link_editing": False,
    }


@login_required
def remove_participant_view(request, pk, participant_pk):
    """Remove a participant from a challenge. GET confirms; POST removes.

    Creator or staff (moderation override, mirroring cancel). Allowed on DRAFT
    and ACTIVE challenges; blocked on COMPLETED/CANCELLED — stricter than bail,
    which only blocks COMPLETED, but a cancelled challenge takes no changes.
    PRG back to detail, matching the bail/close/cancel confirm-page pattern.
    """
    challenge = _get_challenge_for_creator(request, pk, allow_staff=True)

    response = _terminal_status_response(
        request,
        challenge,
        gettext("This challenge can no longer be modified."),
        action="remove a participant from",
    )
    if response is not None:
        return response

    participant = get_object_or_404(
        ChallengeParticipant.objects.select_related("user"),
        pk=participant_pk,
        challenge=challenge,
    )

    if participant.user_id == challenge.creator_id:
        logger.warning(
            "User %s tried to remove the creator from challenge %s",
            request.user.id,
            pk,
        )
        return HttpResponseBadRequest(
            gettext("The creator cannot be removed from their own challenge.")
        )

    if (
        participant.invite_status != ChallengeParticipant.InviteStatus.ACCEPTED
        or participant.is_bailed
    ):
        logger.warning(
            "User %s tried to remove participant %s from challenge %s in invalid "
            "state (invite_status=%s, is_bailed=%s)",
            request.user.id,
            participant.pk,
            pk,
            participant.invite_status,
            participant.is_bailed,
        )
        return HttpResponseBadRequest(gettext("This participant cannot be removed."))

    name = participant.user.effective_display_name

    if request.method == "POST":
        remove_participant(participant)
        logger.info(
            "User %s removed participant %s from challenge %s",
            request.user.id,
            participant.pk,
            pk,
        )
        messages.success(
            request,
            gettext("%(name)s has been removed from the challenge.") % {"name": name},
        )
        return redirect(reverse("challenges:settings", args=[pk]))

    return _render_confirm_action(
        request,
        challenge,
        title=gettext("remove participant"),
        verb=gettext("remove %(name)s from") % {"name": name},
        detail=gettext(
            "Removing freezes their scoring — no further points will be earned "
            "for them in this challenge. Their past entries stay on the "
            "leaderboard. They cannot rejoin, even with an invite link. This "
            "action cannot be undone."
        ),
        action_url=reverse("challenges:remove", args=[challenge.pk, participant.pk]),
        submit_label=gettext("remove participant"),
        cancel_url=reverse("challenges:settings", args=[challenge.pk]),
    )


@login_required
def close_challenge_view(request, pk):
    """Close an active challenge early. GET confirms; POST runs the close."""
    challenge = _get_challenge_for_creator(request, pk)

    response = _terminal_status_response(
        request,
        challenge,
        gettext("This challenge can no longer be closed."),
        action="close",
    )
    if response is not None:
        return response

    if request.method == "POST":
        close_challenge(challenge)
        logger.info("User %s manually closed challenge %s", request.user.id, pk)
        messages.success(request, gettext("Challenge closed."))
        return redirect(reverse("challenges:detail", args=[pk]))

    return _render_confirm_action(
        request,
        challenge,
        title=gettext("close challenge"),
        verb=gettext("close"),
        prompt_suffix=gettext(" early"),
        detail=gettext(
            "Closing runs a final sync for every participant, locks the "
            "leaderboard, and notifies everyone that the challenge has "
            "ended. This action cannot be undone."
        ),
        action_url=reverse("challenges:close", args=[challenge.pk]),
        submit_label=gettext("close challenge"),
        cancel_url=reverse("challenges:detail", args=[challenge.pk]),
    )


@login_required
def rename_challenge_view(request, pk):
    """Rename a challenge from its Settings page.

    Creator-only with the staff rescue override (``allow_staff=True``), matching
    the Settings page's own access pattern. Rejected once the challenge is
    terminal (COMPLETED/CANCELLED), mirroring the lock precedent used by the
    other write actions on this page. Whitespace-only names strip to "" and fail
    the field's default ``required`` validation, matching the create form.

    GET renders the section in display mode (plain text + pencil) by default,
    or edit mode (an inline input replacing the text) when the pencil's
    ``?edit=1`` is present — this is the click-to-edit toggle, no JS beyond
    htmx's request/swap. POST validates and saves: on success the section
    re-renders in display mode with the new name; on failure it re-renders in
    edit mode with the bound form's error. Renders the rename section partial
    for HTMX requests (section-swap), and falls back to a PRG redirect to the
    Settings page for non-HTMX submits.
    """
    challenge = _get_challenge_for_creator(request, pk, allow_staff=True)

    response = _terminal_status_response(
        request,
        challenge,
        gettext("This challenge can no longer be renamed."),
        action="rename",
    )
    if response is not None:
        return response

    rename_editing = request.GET.get("edit") == "1"
    if request.method == "POST":
        form = RenameChallengeForm(request.POST)
        if form.is_valid():
            challenge.name = form.cleaned_data["name"]
            challenge.save(update_fields=["name"])
            logger.info("User %s renamed challenge %s", request.user.id, pk)
            messages.success(request, gettext("Challenge name updated."))
            if not is_htmx(request):
                return redirect(reverse("challenges:settings", args=[pk]))
            form = RenameChallengeForm(initial={"name": challenge.name})
            rename_editing = False
        else:
            rename_editing = True
    else:
        form = RenameChallengeForm(initial={"name": challenge.name})

    if is_htmx(request):
        return render(
            request,
            "challenges/_rename_section.html",
            {
                "challenge": challenge,
                "rename_form": form,
                "rename_editing": rename_editing,
                "oob_messages": True,
            },
        )
    context = _participants_section_context(challenge)
    context["rename_form"] = form
    context["rename_editing"] = rename_editing
    return render(request, "challenges/settings.html", context)


@login_required
def history_window_view(request, pk):
    """Creator/staff control (Settings page) for the point-eligible window.

    Point-eligible window defaults to FROM_START at creation (TASK-247) and can
    only be changed here afterward — e.g. to admit a late joiner fairly by
    switching to FROM_JOIN mid-challenge. Click-to-edit HTMX section-swap
    mirroring rename_challenge_view; blocked once the challenge is terminal,
    matching every other Settings action.
    """
    challenge = _get_challenge_for_creator(request, pk, allow_staff=True)

    response = _terminal_status_response(
        request,
        challenge,
        gettext("This challenge's point-eligible window can no longer be changed."),
        action="change the point-eligible window of",
    )
    if response is not None:
        return response

    editing = request.GET.get("edit") == "1"
    if request.method == "POST":
        form = HistoryWindowForm(request.POST)
        if form.is_valid():
            challenge.history_window = form.cleaned_data["history_window"]
            challenge.save(update_fields=["history_window"])
            logger.info(
                "User %s set challenge %s history_window to %s",
                request.user.id,
                pk,
                challenge.history_window,
            )
            messages.success(request, gettext("Point-eligible window updated."))
            if not is_htmx(request):
                return redirect(reverse("challenges:settings", args=[pk]))
            form = HistoryWindowForm(
                initial={"history_window": challenge.history_window}
            )
            editing = False
        else:
            editing = True
    else:
        form = HistoryWindowForm(initial={"history_window": challenge.history_window})

    if is_htmx(request):
        return render(
            request,
            "challenges/_history_window_section.html",
            {
                "challenge": challenge,
                "history_window_form": form,
                "history_window_editing": editing,
                "oob_messages": True,
            },
        )
    context = _participants_section_context(challenge)
    context["rename_form"] = RenameChallengeForm(initial={"name": challenge.name})
    context["rename_editing"] = False
    context["history_window_form"] = form
    context["history_window_editing"] = editing
    return render(request, "challenges/settings.html", context)


@login_required
@require_POST
def regenerate_invite_link_view(request, pk):
    """Mint a fresh shareable invite link for a challenge, revoking the old one.

    Creator-only — no staff override, matching share_challenge_view: handing
    out a join capability is a social action, not moderation. Blocked once the
    challenge is terminal, matching every other Settings action. HTMX-branches
    like rename_challenge_view/history_window_view: an htmx request gets the
    invite-link section back (with an out-of-band success message); a plain
    request PRG-redirects to Settings.

    Carries the incumbent link's expiry/max-uses forward onto the new one
    (only its token and use_count are actually fresh) -- regenerating is "give
    me a new URL", not "reset my settings", so an owner who'd set a custom
    expiry or a use cap doesn't lose it just because the old link leaked.
    Takes no form, since there's nothing here for a user to get wrong; those
    settings are adjusted afterward via the invite-link section's
    click-to-edit pencil (update_invite_link_view), which edits the link in
    place without minting a new token.
    """
    challenge = _get_challenge_for_creator(request, pk)

    response = _terminal_status_response(
        request,
        challenge,
        gettext("This challenge can no longer accept invites."),
        action="regenerate the invite link for",
    )
    if response is not None:
        return response

    response = _ended_invite_link_response(
        request,
        challenge,
        gettext("This challenge can no longer accept invites."),
        action="regenerate the invite link for",
    )
    if response is not None:
        return response

    incumbent = current_invite_link(challenge)
    new_link = regenerate_invite_link(
        challenge,
        request.user,
        expires_at=incumbent.expires_at if incumbent else None,
        max_uses=incumbent.max_uses if incumbent else None,
        never_expires=(incumbent.expires_at is None) if incumbent else False,
    )
    logger.info(
        "User %s regenerated the invite link for challenge %s", request.user.id, pk
    )
    messages.success(request, gettext("A new invite link has been generated."))

    if is_htmx(request):
        return render(
            request,
            "challenges/_invite_link_section.html",
            {
                "challenge": challenge,
                "current_invite_link": new_link,
                "invite_link_form": _invite_link_form_for(new_link),
                "invite_link_editing": False,
                "invite_link_locked": False,
                "oob_messages": True,
            },
        )
    return redirect(reverse("challenges:settings", args=[pk]))


@login_required
def update_invite_link_view(request, pk):
    """Adjust the challenge's current invite link's expiry/max-uses in place.

    Unlike regenerate_invite_link_view, this does not mint a new token or
    touch use_count -- it's for an owner tweaking the limits on a link
    they've already shared, without invalidating it. 404s if there's no live
    link to update (the Settings/Share UI never renders this form in that
    state, so reaching here without one means a stale page).

    Click-to-edit toggle mirroring rename_challenge_view: GET renders display
    mode (relative-time/uses-count text plus a pencil) by default, or edit
    mode (the Expiry/Max uses inputs) when the pencil's ``?edit=1`` is
    present. POST validates and saves: success drops back to display mode,
    failure re-renders edit mode with the bound form's errors.
    """
    challenge = _get_challenge_for_creator(request, pk)

    response = _terminal_status_response(
        request,
        challenge,
        gettext("This challenge can no longer accept invites."),
        action="update the invite link for",
    )
    if response is not None:
        return response

    response = _ended_invite_link_response(
        request,
        challenge,
        gettext("This challenge can no longer accept invites."),
        action="update the invite link for",
    )
    if response is not None:
        return response

    link = current_invite_link(challenge)
    if link is None:
        raise Http404

    editing = request.GET.get("edit") == "1"
    if request.method == "POST":
        form = InviteLinkOptionsForm(request.POST, challenge=challenge)
        if form.is_valid():
            link = update_invite_link(
                link,
                expires_at=form.cleaned_data["expires_at"],
                max_uses=form.cleaned_data["max_uses"],
                never_expires=form.cleaned_data["never_expires"],
            )
            logger.info(
                "User %s updated the invite link for challenge %s",
                request.user.id,
                pk,
            )
            messages.success(request, gettext("The invite link has been updated."))
            if not is_htmx(request):
                return redirect(reverse("challenges:settings", args=[pk]))
            form = _invite_link_form_for(link)
            editing = False
        else:
            messages.error(request, gettext("Could not update the invite link."))
            editing = True
    else:
        form = _invite_link_form_for(link)

    if is_htmx(request):
        return render(
            request,
            "challenges/_invite_link_section.html",
            {
                "challenge": challenge,
                "current_invite_link": link,
                "invite_link_form": form,
                "invite_link_editing": editing,
                "invite_link_locked": False,
                "oob_messages": True,
            },
        )
    return redirect(reverse("challenges:settings", args=[pk]))


@login_required
@require_GET
def share_challenge_view(request, pk):
    """Post-creation share screen: hand the new owner their invite link (AC#4).

    Where the create wizard lands once the challenge exists. Without it a fresh
    challenge's brand-new link has no first point of contact — the owner would
    drop straight into goal setup with no idea how to invite anyone now that
    there is no user-search invite step.

    Creator-only with no staff override, matching regenerate_invite_link_view:
    handing out a join capability is a social action, not moderation. Stays a
    plain page in a page-transition flow (no htmx branch) — it has no
    in-progress sibling state a swap would need to preserve.
    """
    challenge = _get_challenge_for_creator(request, pk)
    link = current_invite_link(challenge)
    return render(
        request,
        "challenges/share.html",
        {
            "challenge": challenge,
            "current_invite_link": link,
            "invite_link_form": _invite_link_form_for(link),
            "invite_link_editing": False,
        },
    )


@login_required
def transfer_ownership_view(request, pk, user_id):
    """Transfer challenge ownership to another participant.

    GET confirms; POST runs it.

    Creator-only, with a staff override so moderators can rescue a challenge
    orphaned by a deactivated creator. The new owner must be an active, accepted,
    non-bailed participant who is not already the owner.
    """
    challenge = _get_challenge_for_creator(request, pk, allow_staff=True)

    response = _terminal_status_response(
        request,
        challenge,
        gettext("This challenge's ownership can no longer be transferred."),
        action="transfer",
    )
    if response is not None:
        return response

    target = get_object_or_404(
        ChallengeParticipant.objects.select_related("user"),
        challenge=challenge,
        user_id=user_id,
    )
    if (
        target.invite_status != ChallengeParticipant.InviteStatus.ACCEPTED
        or target.is_bailed
        or not target.user.is_active
        or target.user_id == challenge.creator_id
    ):
        logger.warning(
            "User %s tried to transfer challenge %s to ineligible user %s",
            request.user.id,
            pk,
            user_id,
        )
        return HttpResponseBadRequest(
            gettext(
                "Ownership can only be transferred to an active, accepted participant."
            )
        )

    name = target.user.display_name or target.user.username
    requester_is_participant = ChallengeParticipant.objects.filter(
        challenge=challenge,
        user=request.user,
        invite_status=ChallengeParticipant.InviteStatus.ACCEPTED,
    ).exists()
    done_url = (
        reverse("challenges:detail", args=[pk])
        if requester_is_participant
        else reverse("challenges:dashboard")
    )

    if request.method == "POST":
        transfer_ownership(challenge, target.user)
        logger.info(
            "User %s transferred challenge %s to user %s",
            request.user.id,
            pk,
            target.user_id,
        )
        messages.success(
            request, gettext("Ownership transferred to %(name)s.") % {"name": name}
        )
        return redirect(done_url)

    return _render_confirm_action(
        request,
        challenge,
        title=gettext("transfer ownership"),
        verb=gettext("transfer ownership of"),
        prompt_suffix=gettext(" to %(name)s") % {"name": name},
        detail=gettext(
            "%(name)s will gain full control of this challenge — closing, "
            "cancelling, inviting, and removing participants. You will remain "
            "a regular participant. Only the new owner can transfer ownership "
            "back to you, so this cannot be undone on your own."
        )
        % {"name": name},
        action_url=reverse("challenges:transfer", args=[challenge.pk, target.user_id]),
        submit_label=gettext("transfer ownership"),
        cancel_url=done_url,
    )


@login_required
def cancel_challenge_view(request, pk):
    """Cancel a challenge (voids it). GET confirms; POST sets status=cancelled.

    Unlike close, cancel runs no final sync, sends no notifications, and does no
    scoring — it simply flips the challenge to cancelled. Staff users may cancel
    any challenge as a moderation tool, regardless of creator.
    """
    challenge = _get_challenge_for_creator(request, pk, allow_staff=True)

    response = _terminal_status_response(
        request,
        challenge,
        gettext("This challenge can no longer be cancelled."),
        action="cancel",
    )
    if response is not None:
        return response

    if request.method == "POST":
        challenge.status = Challenge.Status.CANCELLED
        challenge.save(update_fields=["status"])
        logger.info("User %s cancelled challenge %s", request.user.id, pk)
        messages.success(request, gettext("Challenge cancelled."))
        return redirect("challenges:dashboard")

    return _render_confirm_action(
        request,
        challenge,
        title=gettext("cancel challenge"),
        verb=gettext("cancel"),
        detail=gettext(
            "Cancelling voids the challenge entirely — no final sync runs, "
            "no scores are recorded, and no closing notifications are sent. The "
            "challenge is removed from the Find Challenges list. This "
            "action cannot be undone."
        ),
        action_url=reverse("challenges:cancel", args=[challenge.pk]),
        submit_label=gettext("cancel challenge"),
        cancel_url=reverse("challenges:detail", args=[challenge.pk]),
        cancel_label=gettext("go back"),
    )


@login_required
@require_POST
def delete_draft_challenge_view(request, pk):
    """Delete a draft challenge the requesting user is starting over on (#1).

    POST-only (confirmation is handled inline by the caller, e.g. htmx's
    hx-confirm or a JS confirm() on the link/button, matching the wizard's
    existing cancel-link convention) -- unlike cancel_challenge_view and the
    other lifecycle actions on this page there is no separate GET-confirm
    page here. Creator-only with no staff override: this is a self-service
    "start over on my own draft", not moderation, so it does not reuse
    _get_challenge_for_creator's allow_staff escape hatch. Only DRAFT
    challenges are eligible -- once a challenge has gone ACTIVE (or beyond)
    it has real participants and history riding on it, so "delete" is no
    longer the right action; cancel_challenge_view covers that case.
    Soft-deletes via delete_draft_challenge (status -> CANCELLED) so the row
    and everything under it survive for audit; the challenge just
    disappears from find_challenges_view and the dashboard like any other
    cancelled challenge.
    """
    challenge = _get_challenge_for_creator(request, pk)

    if challenge.status != Challenge.Status.DRAFT:
        logger.warning(
            "User %s tried to delete non-draft challenge %s (status %s)",
            request.user.id,
            pk,
            challenge.status,
        )
        return HttpResponseBadRequest(gettext("Only draft challenges can be deleted."))

    delete_draft_challenge(challenge)
    logger.info("User %s deleted draft challenge %s", request.user.id, pk)
    messages.success(request, gettext("Draft challenge deleted."))

    if is_htmx(request):
        return render(
            request,
            "components/_messages_oob.html",
            {},
        )
    return redirect("challenges:find")
