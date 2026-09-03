"""Service functions for challenge lifecycle operations."""

import json
import logging
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from io import BytesIO
from zoneinfo import ZoneInfo

import qrcode
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.db.models import Case, CharField, F, Q, When
from django.db.models.functions import Lower
from django.utils import timezone
from django.utils.translation import gettext
from PIL import Image, ImageDraw

from accounts.timezones import user_zoneinfo
from accounts.units import to_display_weight
from challenges.custom_goals import (
    _bodyweight_added_lift_names,
    detach_active_goal,
    grid_field_name,
)
from challenges.events import record_challenge_event
from challenges.models import (
    Challenge,
    ChallengeEvent,
    ChallengeInviteLink,
    ChallengeLift,
    ChallengeParticipant,
    CustomGoal,
    CustomGoalTarget,
    RepTargetGoalTarget,
)
from challenges.rep_target_goals import (
    detach_active_rep_target_goal,
    rep_target_field_names,
)
from challenges.standards import covered_lift_names
from core.models import LiftHistory, LiftSource
from hevy_api.services import sync_user_lifts as sync_hevy_lifts
from liftosaur.services import sync_user_lifts
from notifications.models import Notification
from scoring.domain.calculator import (
    best_score_for_rep_target,
    best_score_for_set,
    format_added_weight,
    is_assisted_equipment,
    is_bodyweight_added_lift,
    points_for_rep_count,
)
from scoring.models import PointEarnEvent
from scoring.services import score_pooled_history
from wger.services import sync_wger_lifts

# Default rep-count the self-report carousel opens on for a lift with no
# current-best PointEarnEvent -- 10RM, the easiest target (TASK-25 design
# review). A lift with a current best opens on the stop matching it instead;
# see _default_manual_rep_count.
MANUAL_DEFAULT_REP_COUNT_NO_HISTORY = 10

logger = logging.getLogger(__name__)


def sync_and_score(user, challenge, *, sync=True) -> None:
    """Refresh a lifter's shared pool (optionally) then score it for a challenge.

    The two concerns stay explicitly composed rather than hidden inside one
    another: ``sync_user_lifts`` is a cooldown-gated API pull with no scoring
    side effect, and ``score_pooled_history`` is a local-DB-only rescore. Pass
    ``sync=False`` to run scoring alone — used where a pull would be wrong (a
    locked challenge takes no writes) or redundant (the goal-setup POST already
    pulled on the matching GET).

    All three live-sync trackers pull into the same shared pool, so a lifter
    could in principle have several connected; each pull is independently
    cooldown-gated and a no-op when its own credentials are unset.
    """
    if sync:
        sync_user_lifts(user)
        sync_hevy_lifts(user)
        sync_wger_lifts(user)
    score_pooled_history(user=user, challenge=challenge)


def submit_manual_lift(
    *,
    user,
    challenge,
    participant: ChallengeParticipant,
    lift: str,
    rep_count: int,
    performed_at,
) -> tuple[LiftHistory, bool] | None:
    """Self-report a completed set against one of the participant's own rep-max
    targets (TASK-25), for a lifter with no workout tracker connected.

    The weight is never taken from the caller — it is always the participant's
    own ``CustomGoalTarget`` for ``(lift, rep_count)``, so a self-report can
    only ever confirm "I hit my own target", never fabricate an arbitrary
    number. Returns ``None`` when the participant has no goal configured yet,
    the goal does not cover this ``(lift, rep_count)`` cell, or the set could
    not raise their score on this lift — the caller treats all three as a
    validation failure.

    That last guard is why a self-report can never be a no-op write: an entry
    scoring at or below the current best is refused outright rather than
    recorded and then reported as an improvement. The carousel disables those
    entries client-side, so reaching this branch means a stale card (the page
    was open while the same lift scored elsewhere) or a hand-made request.

    The written ``LiftHistory`` row always takes the model's own
    ``equipment=""`` default. That field only matters for filtering assisted-
    machine sets out of scoring on bodyweight-added lifts (see
    ``scoring.services.process_scored_set`` /
    ``scoring.domain.calculator.is_assisted_equipment``) — a real concern for
    Liftosaur sync, which parses equipment automatically from data the tracker
    already recorded, but not for self-report: a manually-reported bodyweight
    lift should just score normally, and self-report is already an
    honor-system channel (the confirm button), so an extra disclosure question
    here would add friction without adding real protection.

    Writes a ``source=MANUAL`` ``LiftHistory`` row (``get_or_create``, so a
    duplicate resubmission of the identical set is a no-op rather than an
    ``IntegrityError`` — the unique constraint on ``LiftHistory`` still applies;
    a lost race for the same insert is handled the same way, matching the
    tolerant-of-races convention ``liftosaur.services`` already uses), then
    reuses :func:`scoring.services.score_pooled_history` for the actual
    scoring/best-promotion — this function never touches ``PointEarnEvent``
    directly.

    Returns ``(history_row, points_earned)`` — the points this set actually
    scored, for the caller to report back. Because a set that cannot raise the
    participant's score is refused above, a successful return always means the
    lift's current best moved.

    Refuses outright on a COMPLETED/CANCELLED challenge. The ledger lock in
    ``scoring.services.process_scored_set`` already stops the set from
    scoring, but that is downstream of the ``LiftHistory`` write -- so without
    this guard a self-report against a finished challenge still persisted a
    row and then reported zero points earned.
    """
    if challenge.is_terminal:
        logger.warning(
            "Manual lift self-report rejected for user %s: challenge %s is %s",
            user.id,
            challenge.pk,
            challenge.status,
        )
        return None

    if not participant.has_goal_configured:
        logger.warning(
            "Manual lift self-report rejected for user %s: no goal configured "
            "for challenge %s",
            user.id,
            challenge.pk,
        )
        return None

    target = CustomGoalTarget.objects.filter(
        goal=participant.custom_goal, lift=lift, rep_count=rep_count
    ).first()
    if target is None:
        logger.warning(
            "Manual lift self-report rejected for user %s: no target for "
            "%s %sRM in challenge %s",
            user.id,
            lift,
            rep_count,
            challenge.pk,
        )
        return None

    # Refuse anything that cannot raise the participant's score on this lift.
    # What the set scores is not 11 - rep_count: best_score_for_set takes the
    # highest-point rung the set clears, so a tied or non-monotonic ladder can
    # make one entry score another's points (see _manual_targets_for_lift).
    # Deriving both sides the same way is what keeps this guard identical to
    # the points_delta the carousel showed when the button was pressed.
    thresholds = {
        row.rep_count: row.target_weight
        for row in CustomGoalTarget.objects.filter(
            goal=participant.custom_goal, lift=lift
        )
    }
    scored = best_score_for_set(rep_count, target.target_weight, thresholds)
    would_earn = scored[0] if scored is not None else 0
    current_points = (
        PointEarnEvent.objects.filter(
            user=user, challenge=challenge, lift=lift, is_current_best=True
        )
        .values_list("points_earned", flat=True)
        .first()
        or 0
    )
    if would_earn <= current_points:
        logger.warning(
            "Manual lift self-report rejected for user %s: %s %sRM would earn "
            "%s point(s), not beating the current best of %s in challenge %s",
            user.id,
            lift,
            rep_count,
            would_earn,
            current_points,
            challenge.pk,
        )
        return None

    lookup = {
        "user": user,
        "lift": lift,
        "performed_at": performed_at,
        "reps": rep_count,
        "weight_kg": target.target_weight,
    }
    try:
        history_row, created = LiftHistory.objects.get_or_create(
            **lookup,
            defaults={"source": LiftSource.MANUAL},
        )
    except IntegrityError:
        logger.warning(
            "Manual lift self-report for user %s raced a duplicate insert for "
            "%s %sRM on %s; reusing the existing row",
            user.id,
            lift,
            rep_count,
            performed_at,
        )
        history_row = LiftHistory.objects.get(**lookup)
        created = False

    logger.info(
        "Manual lift self-report: user %s logged %s %sRM for challenge %s (new row=%s)",
        user.id,
        lift,
        rep_count,
        challenge.pk,
        created,
    )

    score_pooled_history(user=user, challenge=challenge)

    # Report what this set actually scored, read back from the event scoring
    # just wrote rather than from the pre-write `would_earn` estimate: the two
    # differ when the set falls outside the challenge's history window, where
    # the guard above still passes but nothing is scored.
    scored_event = (
        PointEarnEvent.objects.filter(
            user=user,
            challenge=challenge,
            lift=lift,
            performed_at=performed_at,
            reps=rep_count,
        )
        .order_by("-synced_at")
        .first()
    )
    return history_row, scored_event.points_earned if scored_event else 0


def submit_manual_rep_target_set(
    *,
    user,
    challenge,
    participant: ChallengeParticipant,
    lift: str,
    rep_count: int,
    performed_at,
) -> tuple[LiftHistory, int] | None:
    """Self-report a completed set against a REP_TARGET goal's single
    ``(target_weight, target_reps)`` target for one lift (issue #85 follow-up),
    for a lifter with no workout tracker connected. Sibling of
    :func:`submit_manual_lift`.

    The weight is never taken from the caller, same "confirm my own target,
    never fabricate a number" principle as Classic — every logged set is
    written at exactly the participant's own ``target_weight`` for this lift.
    Rep count is the only caller-supplied number, since reps toward the
    target is Rep Target's only free scoring axis (Classic's free axis is
    which rep-max rung was hit; here the weight is fixed and only the rep
    count varies).

    Returns ``None`` when the participant has no rep target goal configured,
    the goal does not cover this lift, or the set could not raise the
    participant's score on this lift — the same three-way validation-failure
    contract :func:`submit_manual_lift` documents. The carousel disables
    entries that cannot raise the score, so reaching that last branch means a
    stale card or a hand-made request, not a route the UI can walk into.

    Returns ``(history_row, points_earned)``, mirroring ``submit_manual_lift``.
    Also refuses outright on a COMPLETED/CANCELLED challenge, for the same
    reason ``submit_manual_lift`` does.
    """
    if challenge.is_terminal:
        logger.warning(
            "Manual rep target self-report rejected for user %s: challenge %s is %s",
            user.id,
            challenge.pk,
            challenge.status,
        )
        return None

    if participant.rep_target_goal_id is None:
        logger.warning(
            "Manual rep target self-report rejected for user %s: no goal "
            "configured for challenge %s",
            user.id,
            challenge.pk,
        )
        return None

    target = RepTargetGoalTarget.objects.filter(
        goal=participant.rep_target_goal, lift=lift
    ).first()
    if target is None:
        logger.warning(
            "Manual rep target self-report rejected for user %s: no target "
            "for %s in challenge %s",
            user.id,
            lift,
            challenge.pk,
        )
        return None

    would_earn = (
        best_score_for_rep_target(
            rep_count, target.target_weight, target.target_reps, target.target_weight
        )
        or 0
    )
    current_points = (
        PointEarnEvent.objects.filter(
            user=user, challenge=challenge, lift=lift, is_current_best=True
        )
        .values_list("points_earned", flat=True)
        .first()
        or 0
    )
    if would_earn <= current_points:
        logger.warning(
            "Manual rep target self-report rejected for user %s: %s reps on "
            "%s would earn %s point(s), not beating the current best of %s "
            "in challenge %s",
            user.id,
            rep_count,
            lift,
            would_earn,
            current_points,
            challenge.pk,
        )
        return None

    lookup = {
        "user": user,
        "lift": lift,
        "performed_at": performed_at,
        "reps": rep_count,
        "weight_kg": target.target_weight,
    }
    try:
        history_row, created = LiftHistory.objects.get_or_create(
            **lookup,
            defaults={"source": LiftSource.MANUAL},
        )
    except IntegrityError:
        logger.warning(
            "Manual rep target self-report for user %s raced a duplicate "
            "insert for %s reps on %s on %s; reusing the existing row",
            user.id,
            rep_count,
            lift,
            performed_at,
        )
        history_row = LiftHistory.objects.get(**lookup)
        created = False

    logger.info(
        "Manual rep target self-report: user %s logged %s reps on %s for "
        "challenge %s (new row=%s)",
        user.id,
        rep_count,
        lift,
        challenge.pk,
        created,
    )

    score_pooled_history(user=user, challenge=challenge)

    scored_event = (
        PointEarnEvent.objects.filter(
            user=user,
            challenge=challenge,
            lift=lift,
            performed_at=performed_at,
            reps=rep_count,
        )
        .order_by("-synced_at")
        .first()
    )
    return history_row, scored_event.points_earned if scored_event else 0


def order_by_effective_name(queryset):
    """Annotate ``effective_name`` (display_name, falling back to username when
    blank) and order case-insensitively by it."""
    return queryset.annotate(
        effective_name=Case(
            When(display_name="", then=F("username")),
            default=F("display_name"),
            output_field=CharField(),
        )
    ).order_by(Lower("effective_name"))


def visible_participant_count(challenge) -> int:
    """How many people a participant-facing surface should say are in a challenge.

    One definition of "is in this challenge", shared by every surface that
    prints a headcount (the invite-link landing pages and the accept/decline
    preview), so none of them can drift from the leaderboard's own membership
    rule: accepted, not bailed, and not a deactivated (self-serve-deleted)
    account. The last clause is the one that is easy to forget and the reason
    this is a function -- ``scoring.services.rank_participants`` and the chart
    builders drop deleted accounts, so a count that kept them would advertise
    more lifters than the leaderboard below it lists.
    """
    return challenge.participants.filter(
        invite_status=ChallengeParticipant.InviteStatus.ACCEPTED,
        is_bailed=False,
        user__is_active=True,
    ).count()


def get_co_participants(user):
    """Other users sharing an accepted, played challenge with ``user``.

    Powers the dashboard's "People you've played with" card. "Played" means
    ACTIVE or COMPLETED; the status set is spelled out rather than derived
    from ``Challenge.is_terminal`` because that predicate also covers
    CANCELLED. Deduplicates across multiple shared challenges. Exclusions:

    - DRAFT challenges — nobody has competed in them yet.
    - CANCELLED challenges — nobody played them, so they earn no roster.
    - Challenges the viewer bailed from — a departed participant loses all
      visibility into that challenge, and resurfacing its roster here would
      reopen that hole.
    - Co-participants who bailed — matches the leaderboard and participants
      section, which both drop bailed rows.
    - Deactivated accounts — every other cross-participant surface either
      filters or masks them, and this card has no content besides the name,
      so a masked row would be pure noise.

    Ordering is alphabetical by effective name: deterministic and cheap. A
    relevance sort (active-shared first, or shared-challenge-count desc) is
    not worth the annotation complexity while typical lists are under ten
    people.
    """
    played_challenge_ids = ChallengeParticipant.objects.filter(
        user=user,
        invite_status=ChallengeParticipant.InviteStatus.ACCEPTED,
        is_bailed=False,
        challenge__status__in=(Challenge.Status.ACTIVE, Challenge.Status.COMPLETED),
    ).values_list("challenge_id", flat=True)

    co_participant_ids = (
        ChallengeParticipant.objects.filter(
            challenge_id__in=played_challenge_ids,
            invite_status=ChallengeParticipant.InviteStatus.ACCEPTED,
            is_bailed=False,
        )
        .exclude(user=user)
        .values_list("user_id", flat=True)
        .distinct()
    )

    User = get_user_model()
    return order_by_effective_name(
        User.objects.filter(pk__in=co_participant_ids, is_active=True)
    )


def current_invite_link(challenge):
    """The challenge's single live (non-revoked, non-expired) invite link, if any.

    A null ``expires_at`` means "never expires" (see ChallengeInviteLink),
    so it's admitted alongside the not-yet-expired case.
    """
    return ChallengeInviteLink.objects.filter(
        Q(expires_at__isnull=True) | Q(expires_at__gt=timezone.now()),
        challenge=challenge,
        revoked_at__isnull=True,
    ).first()


def challenge_timezone(challenge) -> ZoneInfo:
    """The IANA zone to interpret ``challenge``'s start_date/end_date in.

    Priority: the creator's pinned accounts.User.timezone (an explicit
    Settings choice); then their opportunistically-persisted
    detected_timezone (accounts.middleware.UserTimezoneMiddleware's best
    last-known browser zone for an "automatic" account); then UTC. There's
    no *live* browser-detected cookie available here the way a request has
    one (accounts.timezones.resolve_timezone's cookie fallback) --
    close_challenges runs from a cron with no request in sight -- so
    detected_timezone is what stands in for that. Checking ``creator_id``
    first (rather than deferencing ``.creator`` directly) also lets callers
    pass an unsaved, creator-less Challenge -- a pattern this app's
    endgame-window tests use deliberately to stay a real-DB-free unit test
    (see test_personal_performance.TestFlagEndgameSuggestion).
    """
    if not challenge.creator_id:
        return ZoneInfo("UTC")
    return user_zoneinfo(challenge.creator)


def challenge_instant(challenge, day, time_of_day) -> datetime:
    """``day``/``time_of_day`` interpreted in ``challenge``'s timezone, as UTC.

    The single conversion point for "when does this civil date/time actually
    happen" for a challenge -- used to compute the end-of-competition instant
    (``time.max`` on ``end_date``) consistently across close_challenges and
    _within_endgame_window, both of which need a timezone that's still
    meaningful with no request in sight (a cron), hence the creator's
    *persisted* pin. See ``challenge_display_end_of_day`` for the
    invite-link expiry's separate, request-time notion of "end of the
    selected day".
    """
    local_dt = datetime.combine(day, time_of_day, tzinfo=challenge_timezone(challenge))
    return local_dt.astimezone(UTC)


def challenge_end_instant(challenge) -> datetime:
    """The instant ``challenge.end_date`` ends, in the creator's timezone."""
    return challenge_instant(challenge, challenge.end_date, time.max)


def challenge_display_end_of_day(challenge, day):
    """``day``'s end-of-day, in *this request's* active display timezone.

    Deliberately independent of challenge_timezone/challenge_end_instant:
    those exist so close_challenges (a cron, no request) still has a
    meaningful timezone via the creator's persisted pin. The invite-link
    expiry picker, by contrast, only ever renders inside a live request, and
    labels itself with whatever timezone UserTimezoneMiddleware activated for
    it (see the Expiry field's "(TZ, UTC offset)" hint) -- so its own "end of
    the selected day" must match that same active timezone, not the
    creator's separately-configured Settings preference, or the two would
    silently disagree by however many hours those timezones differ.
    """
    local_dt = datetime.combine(day, time.max, tzinfo=timezone.get_current_timezone())
    return local_dt.astimezone(UTC)


def _default_invite_link_expiry(challenge):
    """End-of-day expiry for ``challenge.end_date``.

    Callers (regenerate_invite_link, update_invite_link) are only ever
    reached for a challenge that hasn't ended yet -- both views reject the
    request outright once challenge_end_instant has passed -- so end_of_day
    is always still in the future here.
    """
    return challenge_display_end_of_day(challenge, challenge.end_date)


def regenerate_invite_link(
    challenge, by_user, *, expires_at=None, max_uses=None, never_expires=False
) -> ChallengeInviteLink:
    """Mint a fresh live invite link for ``challenge``, revoking any incumbent.

    Only one link is ever live per challenge (the DB constraint on
    ChallengeInviteLink enforces it) — regenerating is how an owner
    invalidates a link that's gotten out, which is what gives regeneration a
    security purpose. Revoking the incumbent (including one that is merely
    expired-but-unrevoked) happens inside the same transaction as the new
    row's creation, satisfying that constraint atomically.

    ``expires_at``, when given, is used verbatim -- validation (e.g. must be
    in the future) is the caller's/form's responsibility. When omitted, the
    default is the challenge's own end_date (end of that day); see
    ``_default_invite_link_expiry``. ``never_expires=True`` overrides both:
    the resulting link's ``expires_at`` is always None regardless of
    whatever ``expires_at`` was passed. ``max_uses`` is None for unlimited
    uses; the overrides are independent of each other and combinable. A
    fresh row's use_count always starts at 0, never carried over from the
    revoked incumbent.

    The token is 6 random bytes (48 bits of entropy, `secrets.token_urlsafe`
    encodes to a fixed 8 characters) -- matching Discord invite codes' length
    and security margin. That's plenty against brute force (2**48 guesses)
    given the worst case of a guessed token is joining a challenge, not an
    account compromise; invite_link_view has no rate limiting today, so
    revisit this if that ever becomes a real concern (add rate limiting
    there, the way login already has it, rather than lengthening the token).
    """
    with transaction.atomic():
        ChallengeInviteLink.objects.filter(
            challenge=challenge, revoked_at__isnull=True
        ).update(revoked_at=timezone.now())
        link = ChallengeInviteLink.objects.create(
            challenge=challenge,
            token=secrets.token_urlsafe(6),
            created_by=by_user,
            expires_at=(
                None
                if never_expires
                else expires_at or _default_invite_link_expiry(challenge)
            ),
            max_uses=max_uses,
        )
    logger.info(
        "Regenerated invite link %s for challenge %s by user %s (expires_at=%s, "
        "max_uses=%s)",
        link.pk,
        challenge.pk,
        by_user.id,
        link.expires_at.isoformat() if link.expires_at else "never",
        max_uses,
    )
    return link


def update_invite_link(
    link, *, expires_at=None, max_uses=None, never_expires=False
) -> ChallengeInviteLink:
    """Adjust ``link``'s expiry/max-uses in place, without touching its token.

    Unlike ``regenerate_invite_link``, this keeps the same row (and therefore
    the same shareable URL and use_count) live -- it's for an owner tweaking
    an already-shared link's limits, not invalidating it. ``expires_at``
    missing/blank falls back to the challenge's default (its own end_date),
    same as a fresh link would get; ``never_expires=True`` overrides that to
    None regardless of ``expires_at``. ``max_uses`` missing/blank means
    unlimited. All are independent, matching ``regenerate_invite_link``.
    """
    link.expires_at = (
        None
        if never_expires
        else expires_at or _default_invite_link_expiry(link.challenge)
    )
    link.max_uses = max_uses
    link.save(update_fields=["expires_at", "max_uses"])
    logger.info(
        "Updated invite link %s for challenge %s (expires_at=%s, max_uses=%s)",
        link.pk,
        link.challenge_id,
        link.expires_at.isoformat() if link.expires_at else "never",
        max_uses,
    )
    return link


def record_invite_link_use(link) -> None:
    """Increment ``link``'s use_count for a completed join (fresh or rejoin).

    Not called on the "already an active member, redirect" path in
    invite_link_view -- that's a revisit, not a new use. A single .update()
    call is already atomic, so no wrapping transaction is needed.
    """
    ChallengeInviteLink.objects.filter(pk=link.pk).update(use_count=F("use_count") + 1)
    logger.debug("Recorded a use of invite link %s", link.pk)


def resolve_invite_token(token):
    """Resolve a bearer token to ``(link, reason)``.

    ``reason`` is ``None`` on success, or one of ``"unknown"`` / ``"expired"``
    / ``"revoked"`` / ``"exhausted"`` — distinct reasons so the invite-link
    landing view can render four different responses without a second query.
    ``link`` is ``None`` only when ``reason == "unknown"``.
    """
    link = (
        ChallengeInviteLink.objects.select_related("challenge", "created_by")
        .filter(token=token)
        .first()
    )
    if link is None:
        return None, "unknown"
    if link.revoked_at is not None:
        return link, "revoked"
    if link.is_expired:
        return link, "expired"
    if link.max_uses is not None and link.use_count >= link.max_uses:
        return link, "exhausted"
    return link, None


# very lift's mark, transcribed from static/logo.svg: a rounded green tile
# holding a barbell. The SVG is nothing but axis-aligned rounded rects, so
# redrawing it with Pillow avoids taking on an SVG rasteriser (cairosvg and
# friends pull in native cairo) purely to stamp a logo onto a QR code.
#
# Coordinates are the SVG's own, centre-relative, in a 242.4243-unit box; the
# renderer scales them to whatever pixel size it is handed. Kept in the SVG's
# paint order because the inner plates deliberately overlap the outer ones.
#
# NOTE: this is a transcription, not a live read of static/logo.svg -- if the
# mark is ever redrawn, this must be updated to match. The decode test guards
# scannability, not brand fidelity.
_LOGO_WIDTH_RATIO = 0.22
_LOGO_UNIT_BOX = 242.4243
_LOGO_GREEN = (19, 104, 67)
_LOGO_WHITE = (255, 255, 255)
_LOGO_PALE = (200, 236, 216)
_LOGO_SHAPES = (
    # (x, y, width, height, corner_radius, fill)
    (-121.2122, -121.2122, 242.4243, 242.4243, 6.0, _LOGO_GREEN),  # tile
    (-76.9231, -11.3637, 153.8461, 22.7273, 1.0, _LOGO_WHITE),  # bar
    (-90.9091, -30.3031, 30.303, 60.6061, 1.5, _LOGO_WHITE),  # left outer plate
    (-68.1819, -45.4546, 22.7273, 90.9091, 1.0, _LOGO_PALE),  # left inner plate
    (60.606, -30.3031, 30.303, 60.6061, 1.5, _LOGO_WHITE),  # right outer plate
    (45.4545, -45.4546, 22.7273, 90.9091, 1.0, _LOGO_PALE),  # right inner plate
)


def _render_logo_tile(size: int) -> Image.Image:
    """Draw the very lift mark as an RGB image ``size`` pixels square."""
    scale = size / _LOGO_UNIT_BOX
    half = _LOGO_UNIT_BOX / 2
    tile = Image.new("RGB", (size, size), _LOGO_WHITE)
    draw = ImageDraw.Draw(tile)
    for x, y, width, height, radius, fill in _LOGO_SHAPES:
        left = (x + half) * scale
        top = (y + half) * scale
        draw.rounded_rectangle(
            (left, top, left + width * scale, top + height * scale),
            radius=max(1.0, radius * scale),
            fill=fill,
        )
    return tile


def build_invite_link_qr_png(url: str) -> bytes:
    """Render ``url`` as a PNG QR code with very lift's mark at its centre
    (TASK-339 / issue #79).

    Error correction H (~30% recoverable) rather than the library's default L
    or the M this started on. That is not cosmetic: the centred logo covers
    modules outright, and the code stays scannable only because the level's
    redundancy can reconstruct what the logo hides. Dropping back to M would
    leave roughly half the headroom and start producing codes that fail on
    marginal scans -- silently, since the image still looks like a QR code.
    The cost is small for a URL this short (version 5 / 37 modules against M's
    version 4 / 33).

    The logo is held to ``_LOGO_WIDTH_RATIO`` of the code's width, so it
    occludes ~5% of the code's area against the ~30% budget -- deliberately
    conservative, because that budget is also what absorbs the glare, creasing
    and print noise these are meant to survive on a flyer or a gym screen. The
    white pad around it keeps the mark from reading as part of the data.

    box_size=10 keeps each module comfortably scannable at arm's length once
    printed; border=4 is the spec's minimum quiet zone, below which some
    scanners refuse to lock on.
    """
    qr = qrcode.QRCode(
        error_correction=qrcode.constants.ERROR_CORRECT_H,
        box_size=10,
        border=4,
    )
    qr.add_data(url)
    qr.make(fit=True)
    image = qr.make_image(fill_color="black", back_color="white").convert("RGB")

    logo_size = int(image.width * _LOGO_WIDTH_RATIO)
    pad = max(2, logo_size // 12)
    backing = Image.new("RGB", (logo_size + pad * 2, logo_size + pad * 2), _LOGO_WHITE)
    backing.paste(_render_logo_tile(logo_size), (pad, pad))
    image.paste(
        backing,
        (
            (image.width - backing.width) // 2,
            (image.height - backing.height) // 2,
        ),
    )

    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def remove_participant(participant) -> None:
    """Remove a participant from a challenge (creator/staff moderation action).

    Removal is bail-plus-flag: is_bailed/bailed_at carry the scoring freeze so
    every existing bail filter applies unchanged, while removed_by_creator marks
    this as a creator-initiated removal (blocks self-rejoin, drives the REMOVED
    notification). Removal is permanent for V1 — nothing clears the flag, and no
    invite link readmits a removed user. The removed user's PointEarnEvent
    history is left untouched, so their scored entries stay on the leaderboard
    exactly like a voluntarily-bailed lifter. custom_goal is detached (not
    deleted) same as bail_view -- moot for this V1 since a removed user can
    never rejoin to trigger the resurrection bail_view's own detach guards
    against, but kept consistent in case that ever changes.

    State validation (ACCEPTED, not already bailed, challenge not locked) is
    the caller's responsibility — the view owns those guards, matching bail_view.
    """
    with transaction.atomic():
        participant.is_bailed = True
        participant.bailed_at = datetime.now(tz=UTC)
        participant.removed_by_creator = True
        detach_active_goal(participant)
        detach_active_rep_target_goal(participant)
        participant.save(
            update_fields=[
                "is_bailed",
                "bailed_at",
                "removed_by_creator",
                "custom_goal",
                "rep_target_goal",
            ]
        )
        # No notification for a deactivated (self-serve-deleted) account:
        # is_active=False blocks login, so the row could never be read. The
        # removal itself still happens -- the scoring freeze and the flag are
        # what the caller asked for, and neither depends on anyone reading it.
        if participant.user.is_active:
            Notification.objects.create(
                user=participant.user,
                event_type=Notification.EventType.REMOVED_FROM_CHALLENGE,
                challenge=participant.challenge,
            )
        record_challenge_event(
            participant.challenge,
            ChallengeEvent.EventType.REMOVED,
            actor=participant.user,
        )
    logger.info(
        "Removed participant %s (user %s) from challenge %s",
        participant.pk,
        participant.user_id,
        participant.challenge_id,
    )


def transfer_ownership(challenge, new_owner) -> None:
    """Reassign a challenge's ownership to ``new_owner`` and notify them.

    Reassigning ``Challenge.creator`` is the only state change: the old owner
    keeps their existing ACCEPTED participant row and becomes a regular
    participant, and the new owner's participant row is untouched. Wrapped in
    ``transaction.atomic()`` so the reassignment and its notification persist
    together. Eligibility and status guards are the caller's responsibility (the
    view), matching ``close_challenge``'s guard-free contract.
    """
    old_creator_id = challenge.creator_id
    with transaction.atomic():
        challenge.creator = new_owner
        challenge.save(update_fields=["creator"])
        Notification.objects.create(
            user=new_owner,
            event_type=Notification.EventType.OWNERSHIP_TRANSFERRED,
            challenge=challenge,
        )
        record_challenge_event(
            challenge,
            ChallengeEvent.EventType.OWNERSHIP_TRANSFERRED,
            actor=new_owner,
        )
    logger.info(
        "Challenge %s ownership transferred from %s to %s",
        challenge.pk,
        old_creator_id,
        new_owner.id,
    )


def challenges_needing_new_owner(user):
    """Non-terminal challenges ``user`` created, paired with eligible successors.

    Used by the self-serve account-deletion flow (accounts.views.delete_account_view)
    to offer an optional ownership-reassignment picker before anonymizing the
    account, since deletion would otherwise silently strand any challenge
    ``user`` still owns behind a creator who can never log back in.

    Each row's ``candidates`` are ordered by ``joined_at`` ascending -- the
    same eligibility as ``transfer_ownership_view`` (ACCEPTED, non-bailed,
    active, not the creator) -- so ``candidates[0]`` is the longest-tenured
    participant, the default the picker preselects and what a submission
    falls back to if the user never opens the picker at all.

    Challenges with no eligible successor are omitted entirely: there's no
    choice to offer, and they're left exactly as today (owned by the
    soon-to-be-anonymized creator, rescuable only by staff) rather than
    inventing a fallback owner from nothing.
    """
    rows = []
    challenges = Challenge.objects.filter(creator=user).exclude(
        status__in=Challenge.TERMINAL_STATUSES
    )
    for challenge in challenges:
        candidates = [
            participant.user
            for participant in ChallengeParticipant.objects.filter(
                challenge=challenge,
                invite_status=ChallengeParticipant.InviteStatus.ACCEPTED,
                is_bailed=False,
                user__is_active=True,
            )
            .exclude(user=user)
            .select_related("user")
            .order_by("joined_at")
        ]
        if not candidates:
            continue
        rows.append(
            {
                "challenge": challenge,
                "candidates": candidates,
                "field_name": f"new_owner__{challenge.pk}",
            }
        )
    return rows


def create_challenge(creator, cleaned_data) -> Challenge:
    """Create a challenge with its creator's participant row and invite link.

    The owner sets only timeframe and lifts (TASK-248) — every challenge is
    CUSTOM, and each participant builds their own goal chart at join. The
    Challenge, its configured lifts, the creator's accepted participant row and
    its live invite link (TASK-249) are written inside a single
    ``transaction.atomic()`` block: if any write fails partway through, the
    whole set rolls back and nothing is persisted (no orphaned challenge).
    Everyone else joins through that link — challenges are invite-only and
    there is no per-user invite mechanism (TASK-272).
    """
    with transaction.atomic():
        challenge = Challenge.objects.create(
            name=cleaned_data["name"],
            creator=creator,
            start_date=cleaned_data["start_date"],
            end_date=cleaned_data["end_date"],
            status=Challenge.Status.DRAFT,
            mode=cleaned_data.get("mode", Challenge.Mode.CLASSIC),
            history_window=cleaned_data["history_window"],
            plate_unit=cleaned_data["plate_unit"],
            smallest_plate=cleaned_data["smallest_plate_kg"],
        )

        ChallengeLift.objects.bulk_create(
            [
                ChallengeLift(challenge=challenge, name=name)
                for name in cleaned_data.get("custom_lift_names", [])
            ]
        )

        ChallengeParticipant.objects.create(
            challenge=challenge,
            user=creator,
            invite_status=ChallengeParticipant.InviteStatus.ACCEPTED,
            joined_at=datetime.now(tz=UTC),
        )

        # Every challenge has a live shareable invite link from birth — it is
        # the only way anyone else joins (TASK-249 AC#1, TASK-272).
        regenerate_invite_link(challenge, creator)

    logger.info("Created challenge %s by user %s", challenge.pk, creator.id)

    return challenge


def close_challenge(challenge) -> None:
    """Run the end-of-challenge sequence and lock the ledger.

    1. For each accepted, non-bailed participant, explicitly compose a final
       sync_user_lifts() pull followed by score_pooled_history() — the two
       concerns stay separate here as everywhere. A single failure is logged and
       does not abort the close.
    2. Flip the challenge to completed and save — this is the ledger lock that
       makes process_scored_set() a no-op.
    3. Create a challenge_closed Notification for every accepted participant,
       including bailed ones -- they were part of the challenge. Deactivated
       (self-serve-deleted) accounts are the one exclusion: ``is_active=False``
       blocks login, so the row could never be read.

    Idempotent: returns immediately if the challenge is already in a terminal
    status. CANCELLED counts -- closing a cancelled challenge would otherwise
    resurrect it as COMPLETED and fire challenge_closed notifications for a
    challenge that was voided.
    """
    if challenge.is_terminal:
        logger.info(
            "close_challenge: %s already %s; no-op", challenge.id, challenge.status
        )
        return

    active_participants = ChallengeParticipant.objects.filter(
        challenge=challenge,
        invite_status=ChallengeParticipant.InviteStatus.ACCEPTED,
        is_bailed=False,
    ).select_related("user")

    for participant in active_participants:
        try:
            sync_and_score(participant.user, challenge)
        except Exception:
            logger.exception(
                "Final sync/score failed for user %s closing challenge %s",
                participant.user.id,
                challenge.id,
            )

    challenge.status = Challenge.Status.COMPLETED
    challenge.save(update_fields=["status"])
    record_challenge_event(challenge, ChallengeEvent.EventType.CLOSED)
    logger.info("Challenge %s closed", challenge.id)

    accepted_participants = ChallengeParticipant.objects.filter(
        challenge=challenge,
        invite_status=ChallengeParticipant.InviteStatus.ACCEPTED,
        user__is_active=True,
    ).select_related("user")

    for participant in accepted_participants:
        Notification.objects.create(
            user=participant.user,
            event_type=Notification.EventType.CHALLENGE_CLOSED,
            challenge=challenge,
        )


def _round_to_increment(value, increment):
    """Round a Decimal to the nearest multiple of increment.

    The result is quantised to one decimal place — the precision every
    displayed weight carries via ``to_display_weight``.
    """
    if increment <= 0:
        return value.quantize(Decimal("0.1"))
    rounded = (value / increment).quantize(Decimal("1")) * increment
    return rounded.quantize(Decimal("0.1"))


def _kg_to_display(weight_kg, unit, challenge, *, snap=True):
    """Convert a kg weight to the viewing user's display unit.

    When ``snap`` is True (the default), the weight is first snapped to the
    challenge's loadable barbell increment. The smallest plate available at
    the venue (``challenge.smallest_plate``, stored in kg) sets the minimum
    loadable barbell change: one plate per side, so the grid is
    ``2 x smallest_plate``. Snapping happens in kg — a physical fact about the
    equipment — then the value is converted to ``unit``, so the viewing user's
    personal unit_preference always governs the displayed unit regardless of the
    challenge's configured plate_unit.

    Snapping is only correct for *computed* plate-loadable targets (rep-max
    thresholds, weight gaps). For *actual recorded* weights a user really lifted,
    pass ``snap=False``: snapping real data to a kg grid derived from a lb plate
    size introduces round-trip drift (e.g. a logged 95 lb rendering as 94.6), so
    such weights are only unit-converted and rounded to normal 0.1 precision.
    """
    if weight_kg is None:
        return None
    weight = Decimal(weight_kg)
    if snap:
        increment_kg = 2 * challenge.smallest_plate
        weight = _round_to_increment(weight, increment_kg)
    value, _ = to_display_weight(weight, unit)
    return value


def _weight_display(weight_kg, lift, unit, challenge, *, snap=True):
    """Format a stored weight for display.

    For bodyweight-added lifts the stored weight already IS the added weight
    (TASK-248 — there is no total-load reconstruction anywhere in this
    product), so it is formatted relative to bodyweight as a string ("BW",
    "+5", "-10"). All other lifts return the absolute weight as a Decimal in
    the display unit.

    ``snap`` is threaded through to :func:`_kg_to_display`; pass ``snap=False``
    for actual recorded weights so they don't pick up plate-grid drift.
    """
    display = _kg_to_display(weight_kg, unit, challenge, snap=snap)
    if is_bodyweight_added_lift(lift):
        return format_added_weight(display)
    return display


def _goal_weight_display(weight_kg, is_bodyweight_added, unit):
    """Format a goal-setup weight (kg) for the suggested-ladder preview.

    For bodyweight-added lifts the value IS the added weight already (never
    total load), so it is formatted as a string ("BW"/"+5"/"-10"). For other
    lifts returns the absolute weight converted to the display unit.
    """
    if is_bodyweight_added:
        display, _ = to_display_weight(weight_kg, unit)
        return format_added_weight(display)
    value, _ = to_display_weight(weight_kg, unit)
    return value


# "Close to goal" highlight: flag unscored (no_points) cards whose gap to the
# first point is within settings.CHALLENGES_CLOSE_TO_GOAL_GAP_FRACTION of the
# rep-adjusted threshold (in raw kg, pre-snap, so it is unit-independent and
# comparable across lifts of very different magnitudes), OR that are within
# settings.CHALLENGES_CLOSE_TO_GOAL_REPS_GAP additional reps at the current
# weight. Both thresholds are env-configurable (see root/settings.py).
# When more than CLOSE_TO_GOAL_MAX_HIGHLIGHTS qualify, only the closest few are
# flagged — a passive "go claim this" nudge, never a ranking or recommendation
# (TASK-202).
CLOSE_TO_GOAL_MAX_HIGHLIGHTS = 3


def _first_point_gap(
    threshold_at, candidate_sets, display_unit, challenge, *, snap=True
):
    """Report the two-dimensional gap to a lift's first point.

    Earning a point is a genuinely 2-D condition — enough WEIGHT *and* enough
    REPS — because ``best_score_for_set`` awards a point at rep-count ``n`` only
    when ``performed_weight >= threshold_for_reps(one_rm, n)`` and
    ``min(reps, 10) >= n``. Thresholds get lighter as reps rise, so the easiest
    point for a set sits at ``n = min(reps, 10)``. Rather than collapse this into
    one reps-agnostic scalar, we surface two independent PATHS to the same first
    point (OR, not AND):

    - WEIGHT path: extra weight needed at the reps already performed,
      ``threshold_for_reps(one_rm, min(reps, 10)) - load`` (clamped at zero).
    - REPS path: keep the weight and perform enough more reps (up to 10) that the
      lightening threshold drops to the current weight. This exists only when the
      weight already clears the 10-rep floor ``threshold_for_reps(one_rm, 10)``;
      otherwise no rep count would earn the point and only the weight path shows.

    ``candidate_sets`` is an iterable of ``(weight_kg, reps, performed_at)`` — the
    weight recorded exactly as stored (for bodyweight-added lifts this IS the
    added weight; TASK-248 removed the total-load conversion entirely). The set
    evaluated is the one nearest to a point — the least weight needed at the reps
    performed, breaking ties toward the higher rep count (the lighter, more
    achievable threshold) — never blindly ``max(weight)``.

    ``threshold_at`` is a callable ``reps -> Decimal`` returning the kg threshold
    for a rep count (1–10), read from the participant's own flat target table
    (the same table scoring.services._GoalTargets reads), or None when the
    participant's goal does not cover this lift. Either way the numbers come
    from the same source scoring uses, so the display cannot drift from scoring
    by construction. Gaps are measured exactly against the threshold — the
    scorer applies no fuzz band, so neither does the display.

    Returns ``(selected_load_kg, selected_reps, selected_date, weight_gap_display,
    reps_gap, gap_fraction)`` where ``weight_gap_display`` is a Decimal in the
    user's display unit (>= 0), ``reps_gap`` is an int (additional reps) or None,
    and ``gap_fraction`` is the raw-kg weight gap divided by the rep-adjusted
    threshold — a unit-independent, pre-snap closeness metric for the
    close-to-goal highlight (None when no threshold is known or it is
    non-positive). Returns ``(None, None, None, None, None, None)`` when there are
    no candidate sets, and ``(selected_load_kg, selected_reps, selected_date,
    None, None, None)`` when the participant's goal does not cover this lift.
    """
    sets = [
        (Decimal(load), int(reps), performed_at)
        for load, reps, performed_at in candidate_sets
        if int(reps) > 0
    ]
    if not sets:
        return None, None, None, None, None, None

    if threshold_at is None:
        # No threshold to measure against — surface the heaviest set for display
        # but no gap can be computed.
        selected_load, selected_reps, selected_date = max(sets, key=lambda s: s[0])
        return selected_load, selected_reps, selected_date, None, None, None

    floor = threshold_at(10)

    def _weight_gap_kg(load, reps):
        gap = threshold_at(min(reps, 10)) - load
        return gap if gap > Decimal("0") else Decimal("0")

    selected_load, selected_reps, selected_date = min(
        sets, key=lambda s: (_weight_gap_kg(s[0], s[1]), -min(s[1], 10))
    )

    selected_gap_kg = _weight_gap_kg(selected_load, selected_reps)
    weight_gap = _kg_to_display(
        selected_gap_kg,
        display_unit,
        challenge,
        snap=snap,
    )

    # Raw-kg closeness metric for the close-to-goal highlight, normalized by the
    # rep-adjusted threshold so it is unit-independent and comparable across
    # lifts of very different magnitudes. Computed pre-snap from the same
    # threshold_at the scorer uses, so it cannot drift from scoring.
    denom = threshold_at(min(selected_reps, 10))
    gap_fraction = selected_gap_kg / denom if denom > Decimal("0") else None

    reps_gap = None
    effective_reps = min(selected_reps, 10)
    if selected_load >= floor and effective_reps < 10:
        # The weight clears the 10-rep floor, so a lighter (higher-rep) threshold
        # exists that it already meets. Find the fewest extra reps to reach it.
        for n in range(effective_reps + 1, 11):
            if selected_load >= threshold_at(n):
                reps_gap = n - selected_reps
                break

    return (
        selected_load,
        selected_reps,
        selected_date,
        weight_gap,
        reps_gap,
        gap_fraction,
    )


def _next_point_gap(threshold_at, current_best, display_unit, challenge, *, snap=True):
    """Weight gap from a scored lift's current best to its *next* point.

    A scored current-best earned ``points_earned = 11 - n`` where ``n`` is the
    smallest rep count whose threshold the lifted weight cleared (subject to
    ``n <= reps``). The next point ``p + 1`` targets ``reps = 11 - (p + 1)``, a
    *heavier* threshold at *fewer* reps. Because that rep count is strictly
    below the one already satisfied, the reps constraint is already met — doing
    more reps at the current weight can never earn the next point. So this gap
    is **weight-only by construction** (no reps path); do not "fix" it to mirror
    :func:`_first_point_gap`'s two-path shape, which only applies to the unscored
    first-point case.

    Returns ``(weight_gap_display, gap_fraction)`` where ``weight_gap_display``
    is a Decimal in the user's display unit (>= 0) and ``gap_fraction`` is the
    raw-kg gap over the target threshold — the same unit-independent, pre-snap
    closeness metric :func:`_first_point_gap` produces, so gaps stay comparable
    across scored and unscored cards. Returns ``(None, None)`` when there is no
    next point (``points_earned >= 10``, AC #7) or the participant's goal does
    not cover this lift.
    """
    p = current_best.points_earned
    if p >= 10 or threshold_at is None:
        return None, None

    target_kg = threshold_at(11 - (p + 1))
    gap_kg = target_kg - current_best.weight
    if gap_kg < Decimal("0"):
        gap_kg = Decimal("0")

    weight_gap = _kg_to_display(gap_kg, display_unit, challenge, snap=snap)
    gap_fraction = gap_kg / target_kg if target_kg > Decimal("0") else None
    return weight_gap, gap_fraction


def _gap_card(
    card,
    state,
    threshold_at,
    candidate_sets,
    lift,
    unit,
    challenge,
):
    """Fill a summary card's "nearest set" gap fields from candidate sets.

    Collapses the two byte-identical gap blocks (no-points-yet and window
    fallback) into one call: run :func:`_first_point_gap`, then write
    ``state`` plus the selected set and its weight/reps gap onto ``card``.

    Every challenge's targets are hand-entered flat values, never computed
    plate-loadable ones (TASK-248), so both :func:`_first_point_gap` and the
    ``best_weight`` display always run with ``snap=False``.
    """
    selected_load, selected_reps, selected_date, weight_gap, reps_gap, gap_fraction = (
        _first_point_gap(threshold_at, candidate_sets, unit, challenge, snap=False)
    )
    card.update(
        {
            "state": state,
            "best_weight": _weight_display(
                selected_load, lift, unit, challenge, snap=False
            ),
            "best_reps": selected_reps,
            "best_date": selected_date,
            "weight_gap": weight_gap,
            "reps_gap": reps_gap,
            "gap_fraction": gap_fraction,
        }
    )


@dataclass(frozen=True)
class _PersonalDataParams:
    """Display parameters shared by every per-lift personal-data helper.

    Bundled so the extracted steps take one immutable context object instead of
    threading several positional args through each call.
    """

    user: object
    challenge: Challenge
    window_start: datetime
    display_unit: str
    goal_label: str


def _threshold_at_for_lift(lift, *, targets_by_lift):
    """Build the ``reps -> kg`` rep-max threshold callable for one lift.

    threshold_at(reps) -> kg is the single source of rep-max thresholds for both
    the gap math and the standards table, so neither can drift from scoring.
    Every challenge's targets are a flat, static per-lift table — the same one
    scoring reads verbatim (scoring.services._GoalTargets) — so this just
    closes over the participant's own table. Returns None when the
    participant's goal does not cover this lift.
    """
    lift_targets = targets_by_lift[lift]

    def threshold_at(reps, _targets=lift_targets):
        return _targets[reps]

    return threshold_at


def _summary_card_for_lift(
    lift, params, *, current_best, lift_window_events, threshold_at
):
    """Classify one lift's "Your Performance" summary card (4-way state).

    scored / no_points / no_data_before_window / no_data. There is no
    "no_data_at_weight" state (TASK-248 removed it along with bodyweight):
    every challenge's targets are static, so a lift's window events are never
    filtered by a bodyweight anchor — every scoreable event in the window is
    a candidate.
    """
    card = {
        "lift": lift,
        "state": "no_data",
        "is_bodyweight_added": is_bodyweight_added_lift(lift),
    }
    if current_best is not None:
        card.update(
            {
                "state": "scored",
                "points_earned": current_best.points_earned,
                "weight": _weight_display(
                    current_best.weight,
                    lift,
                    params.display_unit,
                    params.challenge,
                    snap=False,
                ),
                "reps": current_best.reps,
                "date": current_best.performed_at,
                # Every challenge is CUSTOM — there is no built-in tier
                # vocabulary left to show, so this is always the goal name.
                "tier_satisfied": params.goal_label,
            }
        )
        # Weight-only gap to the next point up, for the endgame suggestion
        # (TASK-212). Computed unconditionally (cheap, no queries); the
        # time-gate lives in _flag_endgame_suggestion. Distinct key names keep
        # it clear of _flag_close_to_goal's gap_fraction and the unscored
        # template blocks.
        next_point_weight_gap, next_point_gap_fraction = _next_point_gap(
            threshold_at,
            current_best,
            params.display_unit,
            params.challenge,
            snap=False,
        )
        card["next_point_weight_gap"] = next_point_weight_gap
        card["next_point_gap_fraction"] = next_point_gap_fraction
    elif lift_window_events:
        _gap_card(
            card,
            "no_points",
            threshold_at,
            ((e.weight, e.reps, e.performed_at) for e in lift_window_events),
            lift,
            params.display_unit,
            params.challenge,
        )
    else:
        # No PointEarnEvent in the window at all — this only happens when
        # every LiftHistory row on this lift in the window is an
        # assisted-equipment set on a bodyweight-added lift (those never get
        # scored, not even an audit row — see scoring.services
        # process_scored_set's §1b skip). Fall back to raw LiftHistory,
        # excluding assisted rows so the card never advertises a gap the
        # scorer will not honour.
        fallback_rows = LiftHistory.objects.filter(
            user=params.user,
            lift=lift,
            performed_at__gte=params.window_start.date(),
        )
        is_bw_added = is_bodyweight_added_lift(lift)
        candidate_sets = [
            (row.weight_kg, row.reps, row.performed_at)
            for row in fallback_rows
            if not (is_bw_added and is_assisted_equipment(row.equipment))
        ]
        if candidate_sets:
            _gap_card(
                card,
                "no_points",
                threshold_at,
                candidate_sets,
                lift,
                params.display_unit,
                params.challenge,
            )
        else:
            # Still no scoreable data in the window. Distinguish "never
            # logged this lift" from "logged it, but only before this
            # participant's challenge window opened" — the latter is
            # easily mistaken for a broken sync when the lifter has
            # months of real history and just saw it render on the
            # goal-setup page moments earlier (TASK-107).
            has_history_in_window = LiftHistory.objects.filter(
                user=params.user,
                lift=lift,
                performed_at__gte=params.window_start.date(),
            ).exists()
            if not has_history_in_window:
                has_history_before_window = LiftHistory.objects.filter(
                    user=params.user,
                    lift=lift,
                    performed_at__lt=params.window_start.date(),
                ).exists()
                if has_history_before_window:
                    card.update(
                        {
                            "state": "no_data_before_window",
                            "window_start_date": params.window_start.date(),
                        }
                    )
    return card


def _manual_targets_for_lift(lift, params, *, threshold_at, current_best):
    """Build the 10RM..1RM target list a summary card's self-report carousel
    pages through (TASK-25).

    One entry per rep count with the same weight ``threshold_at`` already gives
    the Standards table, so the carousel can never show a number that drifts
    from what scoring/the standards table use. ``is_current_best`` mirrors
    :func:`_standards_row_for_lift`'s own cell test exactly (``11 -
    current_best.points_earned == reps``): the current-best PointEarnEvent's
    reps field is the reps actually PERFORMED, which can differ from the rep
    count whose threshold it satisfied, so the satisfied rep count is always
    derived from points_earned, never read off current_best.reps directly.
    """
    current_best_reps = (
        11 - current_best.points_earned if current_best is not None else None
    )
    current_points = current_best.points_earned if current_best is not None else 0
    # Confirming an entry writes a set of exactly (reps, threshold_at(reps)),
    # so what it will score is not 11 - reps but whatever best_score_for_set
    # makes of that set against the whole table -- and that can be MORE.
    # best_score_for_set walks 1RM..10RM and takes the first (highest-point)
    # threshold met, so a set at the 10RM weight also clears the 9RM rung
    # whenever the 9RM target is <= the 10RM one. Ladders tie like that
    # routinely once an Epley ladder is rounded to plate increments, and a
    # hand-entered grid can be non-monotonic outright. Running the real
    # scorer here is what keeps the carousel's promise equal to the award.
    thresholds = {n: threshold_at(n) for n in range(1, 11)}
    targets = []
    for reps in range(10, 0, -1):
        scored = best_score_for_set(reps, thresholds[reps], thresholds)
        points_if_logged = scored[0] if scored is not None else 0
        points_delta = points_if_logged - current_points
        targets.append(
            {
                "rep_count": reps,
                "weight": _weight_display(
                    threshold_at(reps),
                    lift,
                    params.display_unit,
                    params.challenge,
                    snap=False,
                ),
                "is_current_best": reps == current_best_reps,
                "points_delta": points_delta,
            }
        )
    return targets


def _default_manual_rep_count(current_best):
    """Starting rep count for a summary card's self-report carousel (TASK-25,
    revised after UAT: the carousel used to open on the most recently logged
    set, which could be a deload/warm-up/failed attempt with no visible
    relationship to anything the card shows).

    Opens on the stop matching the participant's current best -- exactly the
    ``current_best`` event :func:`_manual_targets_for_lift` already derives
    ``is_current_best`` from, so the carousel's opening stop, its highlighted
    stop, and the points on the card's front face can never disagree; all
    three now come from the same ``current_best`` value. This does mean the
    carousel can open with Confirm disabled (there's nothing to gain by
    re-confirming a best already on record) -- accepted, since opening
    anywhere else would show a number the card itself doesn't display.

    No current-best event (this lift has never scored) falls back to
    ``MANUAL_DEFAULT_REP_COUNT_NO_HISTORY`` (10RM, the easiest target).
    ``current_best`` is never a zero-point event -- :func:`scoring.services.
    _persist_audit_row` writes sub-threshold sets with ``is_current_best=
    False``, so a lift that has only ever scored 0 points has no
    ``current_best`` row and takes this same fallback.
    """
    if current_best is None:
        return MANUAL_DEFAULT_REP_COUNT_NO_HISTORY
    return 11 - current_best.points_earned


def _standards_row_for_lift(lift, params, *, threshold_at, current_best, rep_columns):
    """Build one lift's standards-table row (10RM..1RM cells).

    ``rep_columns`` is the ``[{"reps": n, "points": points_for_rep_count(n)}]``
    list built alongside the header row, so column order/count can never
    drift between the header's point labels and each row's cells.
    """
    cells = []
    for col in rep_columns:
        reps = col["reps"]
        cell = {"reps": reps, "weight": None, "is_current_best": False}
        if threshold_at is not None:
            cell["weight"] = _weight_display(
                threshold_at(reps),
                lift,
                params.display_unit,
                params.challenge,
                snap=False,
            )
        if current_best is not None and (11 - current_best.points_earned) == reps:
            cell["is_current_best"] = True
        cells.append(cell)
    return {
        "lift": lift,
        "cells": cells,
        "is_bodyweight_added": is_bodyweight_added_lift(lift),
    }


def _flag_close_to_goal(summary_cards):
    """Set ``close_to_goal`` on the unscored cards nearest their first point.

    A card qualifies when it is an unscored ``no_points`` card with a known gap
    that is either within ``settings.CHALLENGES_CLOSE_TO_GOAL_GAP_FRACTION`` of
    the rep-adjusted threshold (inclusive) or within
    ``settings.CHALLENGES_CLOSE_TO_GOAL_REPS_GAP`` additional reps at the
    current weight (inclusive) — both thresholds are env-configurable. Only
    ``no_points`` cards are eligible: scored / no-data cards have nothing to
    claim.

    When more than ``CLOSE_TO_GOAL_MAX_HIGHLIGHTS`` qualify, only the closest few
    are flagged so the signal isn't diluted late in a challenge. The sort exists
    solely to enforce that cap — it is never surfaced, so this stays a passive
    binary flag, not a ranking or recommendation of which lift to train next
    (TASK-202). Non-qualifying cards get no key; the template reads truthiness.
    """
    gap_fraction_threshold = Decimal(
        str(settings.CHALLENGES_CLOSE_TO_GOAL_GAP_FRACTION)
    )
    reps_gap_threshold = settings.CHALLENGES_CLOSE_TO_GOAL_REPS_GAP
    qualifiers = [
        card
        for card in summary_cards
        if card.get("state") == "no_points"
        and card.get("gap_fraction") is not None
        and (
            card["gap_fraction"] <= gap_fraction_threshold
            or (
                card.get("reps_gap") is not None
                and card["reps_gap"] <= reps_gap_threshold
            )
        )
    ]
    qualifiers.sort(key=lambda card: (card["gap_fraction"], card["lift"]))
    for card in qualifiers[:CLOSE_TO_GOAL_MAX_HIGHLIGHTS]:
        card["close_to_goal"] = True


def _within_endgame_window(challenge):
    """True when a challenge is in its endgame window and still live.

    The window opens ``settings.CHALLENGES_ENDGAME_WINDOW_DAYS`` days before
    ``end_date`` and closes at ``end_date`` (inclusive both ends). It is defined
    purely by ``end_date`` with no ``start_date`` dependency, so a short
    challenge is in its window from day one (doc-5 §1). A terminal
    (COMPLETED/CANCELLED) challenge is excluded even if its ``end_date`` is
    still ahead — a dead challenge shouldn't nag.

    Both boundaries are resolved through challenge_instant/challenge_timezone
    (the creator's pinned timezone, UTC fallback) rather than a naive
    ``date.today()``, so this agrees with close_challenges and the
    invite-link default expiry about when a challenge's day actually starts
    and ends.
    """
    if challenge.is_terminal:
        return False
    window_open_date = challenge.end_date - timedelta(
        days=settings.CHALLENGES_ENDGAME_WINDOW_DAYS
    )
    window_open_instant = challenge_instant(challenge, window_open_date, time.min)
    now = timezone.now()
    return window_open_instant <= now <= challenge_end_instant(challenge)


def _flag_endgame_suggestion(summary_cards, challenge):
    """Flag the single closest point-gap as the endgame suggestion (TASK-212).

    In the final stretch of a challenge (see :func:`_within_endgame_window`),
    surface exactly one motivational suggestion restating a participant's nearest
    point-gap — the next point up for an already-scored lift, or the first point
    for an unscored one. Guardrail (PRD §1/§8): the flag only ever marks a card
    whose kg/reps gap was already computed for display; it carries no training or
    exercise recommendation, just a restatement of that gap.

    Qualifiers, gated on the two ENDGAME settings (kept separate from the
    close-to-goal pair so they can be tuned independently):

    - ``scored`` cards via the weight path only — the scored next-point gap is
      weight-only by construction (see :func:`_next_point_gap`). Cards at
      ``points_earned == 10`` never qualify (their gap is None, AC #7).
    - ``no_points`` cards via the same weight-OR-reps shape as close-to-goal.

    Gaps are compared across scored and unscored cards by the unit-independent
    ``gap_fraction`` (correction: raw-kg gaps against different thresholds are
    not directly comparable). Only the single smallest-fraction card is flagged
    (AC #4), ties broken by lift name. The flagged card gets
    ``endgame_suggestion`` (``"next_point"`` or ``"first_point"``) and, for
    unscored cards, ``endgame_suggestion_via`` (``"weight"``, ``"reps"``, or
    ``"both"`` when the lift qualifies on weight and reps simultaneously) so the
    template can pick the matching copy without re-evaluating thresholds. When
    both qualify, both distances are restated rather than collapsing to one.
    """
    if not _within_endgame_window(challenge):
        return

    gap_fraction_threshold = Decimal(str(settings.CHALLENGES_ENDGAME_GAP_FRACTION))
    reps_gap_threshold = settings.CHALLENGES_ENDGAME_REPS_GAP

    qualifiers = []
    for card in summary_cards:
        state = card.get("state")
        if state == "scored":
            fraction = card.get("next_point_gap_fraction")
            if fraction is not None and fraction <= gap_fraction_threshold:
                qualifiers.append((fraction, card["lift"], card, "next_point", None))
        elif state == "no_points":
            fraction = card.get("gap_fraction")
            if fraction is None:
                continue
            by_weight = fraction <= gap_fraction_threshold
            reps_gap = card.get("reps_gap")
            by_reps = reps_gap is not None and reps_gap <= reps_gap_threshold
            if by_weight and by_reps:
                via = "both"
            elif by_weight:
                via = "weight"
            elif by_reps:
                via = "reps"
            else:
                continue
            qualifiers.append((fraction, card["lift"], card, "first_point", via))

    if not qualifiers:
        return

    qualifiers.sort(key=lambda q: (q[0], q[1]))
    _, _, card, kind, via = qualifiers[0]
    card["endgame_suggestion"] = kind
    if via is not None:
        card["endgame_suggestion_via"] = via


def _next_reps_for_rep_target(current_points, target_reps, target_weight) -> int | None:
    """Fewest reps (assuming the weight gate is already met) that would score
    more than ``current_points`` against a Rep Target goal.

    Reuses ``best_score_for_rep_target`` itself (called with
    ``performed_weight == target_weight``, satisfying the gate exactly) rather
    than inverting its round-half-up formula by hand -- this is what keeps the
    "N more reps" nudge mathematically identical to what scoring would actually
    award, the same "single source of truth" reasoning _threshold_at_for_lift
    documents for Classic. Returns None when ``current_points`` is already 10
    (there is no next point).
    """
    for reps in range(1, target_reps + 1):
        points = best_score_for_rep_target(
            reps, target_weight, target_reps, target_weight
        )
        if points is not None and points > current_points:
            return reps
    return None


def _rep_target_point_columns(target_reps, target_weight, current_points):
    """Build the 10-column ``[{points, reps, is_current_best}]`` ladder for one
    lift's Rep Target goal -- the "how many reps for N points" sibling of
    Classic's rep-max table ("how much weight for N points"), both driven by
    the same 10-point scale. Reuses ``best_score_for_rep_target`` itself
    (``performed_weight == target_weight``, satisfying the gate exactly)
    rather than inverting its round-half-up formula by hand, the same
    reasoning :func:`_next_reps_for_rep_target` documents.

    A low ``target_reps`` can make some point values unreachable at exactly
    that rep count (e.g. target_reps=5 jumps straight from 0 to 2 points at
    1 rep -- there's no reps count that scores exactly 1) -- the minimal reps
    that scores AT LEAST that many points is shown instead, so consecutive
    columns may repeat the same rep count. This mirrors
    best_score_for_rep_target's own documented behavior, not a bug in the
    column builder.

    ``current_points`` is the participant's actual current-best score for
    this lift (``None`` if they haven't scored it yet); the matching column
    is flagged ``is_current_best``, mirroring Classic's ``cell.is_current_best``.
    """
    # Points are monotone in reps, so one pass over 1..target_reps finds the
    # first rep count reaching every point value -- no per-column rescan.
    first_reps_for_points: dict[int, int] = {}
    for reps in range(1, target_reps + 1):
        points = best_score_for_rep_target(
            reps, target_weight, target_reps, target_weight
        )
        if not points:
            continue
        for target_points in range(1, min(points, 10) + 1):
            first_reps_for_points.setdefault(target_points, reps)
        if len(first_reps_for_points) == 10:
            break
    return [
        {
            "points": target_points,
            "reps": first_reps_for_points.get(target_points),
            "is_current_best": current_points == target_points,
        }
        for target_points in range(1, 11)
    ]


def _manual_targets_for_rep_target(
    lift, params, *, target_weight, target_reps, current_points
):
    """Build the reps-first self-report carousel a REP_TARGET summary card
    pages through -- the sibling of :func:`_manual_targets_for_lift`.

    Reps, not points, is the axis the lifter actually picks: unlike Classic
    (whose free axis is which rep-max rung to confirm, with weight varying
    per rung), Rep Target's weight is a single fixed gate -- the goal's own
    ``target_weight`` -- so every stop asks "how many reps did you do" and
    shows what that scores, never the other way around.

    Each stop is the fewest reps that earns a new, distinct point value,
    computed by walking ``best_score_for_rep_target`` (the real scorer, not
    an inversion of its formula) over ``1..target_reps`` and keeping only the
    first rep count to reach each points value. Reps that score 0, or that
    repeat a points value an earlier (fewer-reps) stop already reached, are
    not real choices, so they are dropped rather than padded in to reach a
    fixed count: the stop count is ``min(target_reps, 10)``, not always 10.

    Because every stop's label IS the reps that earns its listed points
    (both computed here from the same scorer that later awards the set),
    confirming a stop can never score more or less than its own label
    promises -- there is no rung-tie hazard to guard against here, unlike a
    rep-max ladder where two Classic targets can tie.
    """
    weight_display = _weight_display(
        target_weight, lift, params.display_unit, params.challenge, snap=False
    )
    current_points = current_points or 0
    targets = []
    seen_points = set()
    for reps in range(1, target_reps + 1):
        points = (
            best_score_for_rep_target(reps, target_weight, target_reps, target_weight)
            or 0
        )
        if points == 0 or points in seen_points:
            continue
        seen_points.add(points)
        targets.append(
            {
                "points": points,
                "rep_count": reps,
                "weight": weight_display,
                "is_current_best": points == current_points,
                "points_delta": points - current_points,
            }
        )
        if len(targets) == 10:
            break
    return targets


def _default_manual_rep_count_for_rep_target(current_points, manual_targets):
    """Starting rep count for a REP_TARGET summary card's self-report carousel
    -- sibling of :func:`_default_manual_rep_count`, revised the same way
    after the same UAT finding.

    Opens on the stop matching ``current_points`` -- the same value
    :func:`_manual_targets_for_rep_target` derives each stop's
    ``is_current_best`` from -- so the opening stop, the highlighted stop,
    and the points on the card's front face can never disagree. No scoring
    history, or history that only ever scored zero points (``current_points``
    falsy), falls back to the first (fewest-reps, lowest-points) stop -- the
    Rep Target equivalent of Classic's 10RM fallback.

    Matched against ``manual_targets`` itself (rather than re-derived) so the
    default always lands on a stop that actually exists in the list, which
    can be shorter than 10 entries for a small ``target_reps``.
    """
    if current_points:
        match = next((t for t in manual_targets if t["points"] == current_points), None)
        if match is not None:
            return match["rep_count"]
    return manual_targets[0]["rep_count"]


def build_rep_target_personal_data(user, challenge, participant):
    """The REP_TARGET sibling of :func:`build_personal_data`.

    Each summary card tracks progress toward one lift's single (target_weight,
    target_reps) goal instead of a rep-max ladder: a progress bar/fraction
    ("12/20 reps -> 6 pts") once the weight gate is met, or a weight-gate
    message before it is. Reuses the exact same "Close to goal"/"Final
    stretch" tuning constants and flagging functions as Classic
    (_flag_close_to_goal/_flag_endgame_suggestion) rather than a separate set
    (issue #85 open question #1) -- both functions key off generic
    state/gap_fraction/reps_gap card fields, and Rep Target's weight-gate gap
    and reps-to-next-point gap slot into that same vocabulary cleanly enough
    that a second set of near-duplicate CHALLENGES_* settings would only add
    surface area for the same UX concept.

    Each card also carries ``point_columns`` (see
    :func:`_rep_target_point_columns`) -- unused by the summary cards
    themselves, but consumed by the "Goals" tab
    (templates/challenges/_rep_target_goal_tab.html), the Rep Target
    equivalent of Classic's rep-max ladder table: 10 columns of "reps needed
    for N points" instead of Classic's 10 columns of "weight needed for N
    points", with the matching column highlighted the same way Classic
    highlights its one current-best cell.

    Returns None under the same conditions as build_personal_data: no
    effective window start, or the participant hasn't configured a goal yet.
    """
    window_start = challenge.window_start_for(participant)
    if window_start is None or participant.rep_target_goal_id is None:
        return None

    display_unit = user.unit_preference
    goal = participant.rep_target_goal
    targets_by_lift = {
        target.lift: (target.target_weight, target.target_reps)
        for target in goal.targets.all()
    }
    lift_names = sorted(targets_by_lift)
    goal_label = goal.name

    # Window-independent, matching Classic (D5/TASK-164): a bail/rejoin resets
    # joined_at, which would otherwise blank a card for points that still
    # stand on the leaderboard.
    current_best_by_lift = {
        event.lift: event
        for event in PointEarnEvent.objects.filter(
            user=user, challenge=challenge, is_current_best=True
        )
    }
    events = list(
        PointEarnEvent.objects.filter(
            user=user, challenge=challenge, performed_at__gte=window_start
        )
    )

    params = _PersonalDataParams(
        user=user,
        challenge=challenge,
        window_start=window_start,
        display_unit=display_unit,
        goal_label=goal_label,
    )

    # Batched lookups, one query each however many lifts the goal holds: the
    # bodyweight-added set, and the LiftHistory fallback for lifts with no
    # point event (same idiom as fetching the events once above).
    bw_added_lifts = _bodyweight_added_lift_names(set(lift_names))
    fallback_lifts = [
        lift
        for lift in lift_names
        if lift not in current_best_by_lift and not any(e.lift == lift for e in events)
    ]
    fallback_rows_by_lift: dict[str, list] = {}
    if fallback_lifts:
        for row in LiftHistory.objects.filter(
            user=user, lift__in=fallback_lifts, performed_at__gte=window_start.date()
        ):
            fallback_rows_by_lift.setdefault(row.lift, []).append(row)

    summary_cards = []
    for lift in lift_names:
        target_weight, target_reps = targets_by_lift[lift]
        is_bw_added = lift in bw_added_lifts
        card = {
            "lift": lift,
            "is_bodyweight_added": is_bw_added,
            "target_weight": _weight_display(
                target_weight, lift, display_unit, challenge, snap=False
            ),
            "target_reps": target_reps,
            "state": "no_data",
        }

        current_best = current_best_by_lift.get(lift)
        if current_best is not None:
            points = current_best.points_earned
            progress_reps = min(current_best.reps, target_reps)
            card.update(
                {
                    "state": "scored",
                    "points_earned": points,
                    "progress_reps": progress_reps,
                    "weight": _weight_display(
                        current_best.weight, lift, display_unit, challenge, snap=False
                    ),
                    "date": current_best.performed_at,
                }
            )
            if points < 10:
                next_reps = _next_reps_for_rep_target(
                    points, target_reps, target_weight
                )
                if next_reps is not None:
                    card["reps_gap"] = max(next_reps - progress_reps, 1)
                    # A reps-based closeness fraction, not a weight one -- the
                    # weight gate is already met for a scored card, so the
                    # only thing left to close is reps. Same convention as
                    # Classic's _next_point_gap: the REMAINING gap over the
                    # target (not the total the next point requires), so it's
                    # comparable against CHALLENGES_ENDGAME_GAP_FRACTION and
                    # shrinks as the lifter closes in.
                    card["next_point_gap_fraction"] = Decimal(
                        card["reps_gap"]
                    ) / Decimal(target_reps)
        else:
            lift_window_events = [e for e in events if e.lift == lift]
            candidate_rows = [(e.weight, e.performed_at) for e in lift_window_events]
            if not candidate_rows:
                candidate_rows = [
                    (row.weight_kg, row.performed_at)
                    for row in fallback_rows_by_lift.get(lift, [])
                    if not (is_bw_added and is_assisted_equipment(row.equipment))
                ]
            if candidate_rows:
                best_weight_kg, best_date = max(candidate_rows, key=lambda r: r[0])
                gap_kg = target_weight - best_weight_kg
                if gap_kg < Decimal("0"):
                    gap_kg = Decimal("0")
                card.update(
                    {
                        "state": "no_points",
                        "best_weight": _weight_display(
                            best_weight_kg, lift, display_unit, challenge, snap=False
                        ),
                        "best_date": best_date,
                        "weight_gap": _kg_to_display(
                            gap_kg, display_unit, challenge, snap=False
                        ),
                        "gap_fraction": (
                            gap_kg / target_weight
                            if target_weight > Decimal("0")
                            else None
                        ),
                    }
                )

        card["point_columns"] = _rep_target_point_columns(
            target_reps, target_weight, card.get("points_earned")
        )
        card["manual_targets"] = _manual_targets_for_rep_target(
            lift,
            params,
            target_weight=target_weight,
            target_reps=target_reps,
            current_points=card.get("points_earned"),
        )
        summary_cards.append(card)

    _flag_close_to_goal(summary_cards)
    _flag_endgame_suggestion(summary_cards, challenge)

    for card in summary_cards:
        card["manual_default_rep_count"] = _default_manual_rep_count_for_rep_target(
            card.get("points_earned"), card["manual_targets"]
        )

    return {
        "summary_cards": summary_cards,
        "point_range": list(range(1, 11)),
        "display_unit": display_unit,
        "goal_label": goal_label,
    }


def build_personal_data(user, challenge, participant):
    """Build the personal performance context for the challenge detail page.

    Returns a dict with:
    - summary_cards: one card per lift in the participant's goal, scoped to
      events with performed_at >= the window start.
    - standards_rows: rows of per-lift rep-max target weights (10RM..1RM) from
      the participant's own CustomGoal, with the current-best cell flagged.
    - display_unit: the unit gap/weights are shown in (the user's stored
      unit_preference, defaulting to kg).

    Returns None when the participant has not configured a goal or has no
    effective window start, since the standards set and participation-window
    filter each depend on one of them. The window start is the participant's
    join timestamp (from_join mode) or the challenge start date (from_start).

    Dispatches to :func:`build_rep_target_personal_data` for a REP_TARGET
    challenge, which has no rep-max ladder to build ``standards_rows`` from.
    """
    if challenge.mode == Challenge.Mode.REP_TARGET:
        return build_rep_target_personal_data(user, challenge, participant)

    window_start = challenge.window_start_for(participant)
    if window_start is None or participant.custom_goal_id is None:
        return None

    display_unit = user.unit_preference

    targets_by_lift = custom_targets_from_goal(participant.custom_goal)
    lift_names = sorted(targets_by_lift)
    goal_label = participant.custom_goal.name

    params = _PersonalDataParams(
        user=user,
        challenge=challenge,
        window_start=window_start,
        display_unit=display_unit,
        goal_label=goal_label,
    )

    events = list(
        PointEarnEvent.objects.filter(
            user=user,
            challenge=challenge,
            performed_at__gte=window_start,
        )
    )

    # The participant's current-best per lift is a window-independent fact: it is
    # the is_current_best=True event the leaderboard and points-over-time chart
    # both credit (neither filters by window_start). Sourcing the card's scored
    # state from these directly — rather than from the window-scoped `events`
    # above — keeps "Your Performance" consistent with the chart and leaderboard
    # for a participant who bailed and rejoined: rejoin resets joined_at to now
    # (so a FROM_JOIN window restarts for future scoring), which would otherwise
    # push every pre-bail current-best before window_start and blank the card,
    # even though those points still stand on the leaderboard and chart (TASK-164).
    # The window-scoped `events` still drive the unscored gap/fallback cards, so a
    # lift the participant has genuinely not scored yet is unaffected.
    current_best_by_lift = {
        event.lift: event
        for event in PointEarnEvent.objects.filter(
            user=user,
            challenge=challenge,
            is_current_best=True,
        )
    }

    rep_columns = [
        {"reps": n, "points": points_for_rep_count(n)} for n in range(10, 0, -1)
    ]

    summary_cards = []
    standards_rows = []
    for lift in lift_names:
        lift_window_events = [e for e in events if e.lift == lift]
        # A scored current-best counts wherever it was logged, so the card stays
        # consistent with the chart and leaderboard (window-independent lookup).
        current_best = current_best_by_lift.get(lift)
        threshold_at = _threshold_at_for_lift(lift, targets_by_lift=targets_by_lift)
        card = _summary_card_for_lift(
            lift,
            params,
            current_best=current_best,
            lift_window_events=lift_window_events,
            threshold_at=threshold_at,
        )
        card["manual_targets"] = _manual_targets_for_lift(
            lift, params, threshold_at=threshold_at, current_best=current_best
        )
        summary_cards.append(card)
        standards_rows.append(
            _standards_row_for_lift(
                lift,
                params,
                threshold_at=threshold_at,
                current_best=current_best,
                rep_columns=rep_columns,
            )
        )

    _flag_close_to_goal(summary_cards)
    _flag_endgame_suggestion(summary_cards, challenge)

    for card in summary_cards:
        card["manual_default_rep_count"] = _default_manual_rep_count(
            current_best_by_lift.get(card["lift"])
        )

    return {
        "summary_cards": summary_cards,
        "standards_rows": standards_rows,
        "rep_columns": rep_columns,
        "display_unit": display_unit,
        "goal_label": goal_label,
    }


def build_rep_target_participant_chart(
    viewer, challenge, subject_participant
) -> dict | None:
    """The REP_TARGET sibling of :func:`build_participant_chart`.

    Same peer-view contract: the subject's locked targets and scored points
    only, in the ``viewer``'s unit_preference, with none of the self-directed
    coaching signals. The table it feeds is the reps-per-point ladder rather
    than Classic's weight-per-point one, so the row shape matches what
    _rep_target_goal_table.html renders for the subject's own Goals tab.

    Provenance is thinner than Classic's by construction: RepTargetGoal
    records only a source_method, with no source_detail, so there is no
    standards attribution to surface and no uplift/lookback sentence to
    compose.

    Returns None when the subject has not finished goal setup
    (``rep_target_goal_id is None``), same signal the caller already handles.
    """
    if subject_participant.rep_target_goal_id is None:
        return None

    subject_user = subject_participant.user
    goal = subject_participant.rep_target_goal
    display_unit = viewer.unit_preference
    targets_by_lift = {
        target.lift: (target.target_weight, target.target_reps)
        for target in goal.targets.all()
    }

    # Window-independent, matching the leaderboard and Classic's chart
    # (D5/TASK-164): a rejoin resets joined_at, which would otherwise blank
    # rows for points that still stand on the leaderboard.
    current_best_by_lift = {
        event.lift: event
        for event in PointEarnEvent.objects.filter(
            user=subject_user, challenge=challenge, is_current_best=True
        )
    }

    bw_added_lifts = _bodyweight_added_lift_names(set(targets_by_lift))
    target_rows = []
    point_rows = []
    for lift in sorted(targets_by_lift):
        target_weight, target_reps = targets_by_lift[lift]
        is_bw_added = lift in bw_added_lifts
        current_best = current_best_by_lift.get(lift)
        points = current_best.points_earned if current_best is not None else 0
        target_rows.append(
            {
                "lift": lift,
                "is_bodyweight_added": is_bw_added,
                "target_weight": _weight_display(
                    target_weight, lift, display_unit, challenge, snap=False
                ),
                "target_reps": target_reps,
                "point_columns": _rep_target_point_columns(
                    target_reps,
                    target_weight,
                    points if current_best is not None else None,
                ),
            }
        )
        point_rows.append(
            {
                "lift": lift,
                "points_earned": points,
                "weight": (
                    _weight_display(
                        current_best.weight, lift, display_unit, challenge, snap=False
                    )
                    if current_best is not None
                    else None
                ),
                "reps": current_best.reps if current_best is not None else None,
                "date": current_best.performed_at if current_best is not None else None,
                "is_bodyweight_added": is_bw_added,
            }
        )

    logger.debug(
        "Built rep target participant chart for subject %s in challenge %s: %d lift(s)",
        subject_user.id,
        challenge.pk,
        len(target_rows),
    )

    return {
        "subject_name": subject_user.display_name or subject_user.username,
        "goal_name": goal.name,
        "locked_at": goal.created_at,
        "provenance": {
            "method_label": goal.get_source_method_display(),
            "is_standards": False,
            "snapshot_version": None,
            "history_sentence": None,
        },
        "is_rep_target": True,
        "target_rows": target_rows,
        "point_rows": point_rows,
        "point_range": list(range(1, 11)),
        "display_unit": display_unit,
        "total_points": sum(row["points_earned"] for row in point_rows),
    }


def build_participant_chart(viewer, challenge, subject_participant) -> dict | None:
    """Build a read-only view of a co-participant's locked goal chart.

    Unlike :func:`build_personal_data` (self-directed coaching signals for the
    subject), this is a peer view: it renders the subject's target grid and
    scored points only, with the ``viewer``'s own unit_preference governing
    display (not the subject's — see ``Challenge.plate_unit``'s docstring).
    Deliberately omits ``close_to_goal``, ``endgame_suggestion``, and the
    unscored ``LiftHistory`` fallback — those are self-directed nudges over
    the subject's unscored training log, not "how did this score happen".

    Returns None when the subject has not finished goal setup yet
    (``custom_goal_id is None`` — an ACCEPTED participant who joined but has
    not completed the wizard); the caller renders a "hasn't set a goal yet"
    state.

    Dispatches to :func:`build_rep_target_participant_chart` for a REP_TARGET
    challenge, mirroring how build_personal_data splits by mode.
    """
    if challenge.mode == Challenge.Mode.REP_TARGET:
        return build_rep_target_participant_chart(
            viewer, challenge, subject_participant
        )

    if subject_participant.custom_goal_id is None:
        return None

    subject_user = subject_participant.user
    goal = subject_participant.custom_goal
    display_unit = viewer.unit_preference

    targets_by_lift = custom_targets_from_goal(goal)
    lift_names = sorted(targets_by_lift)
    goal_label = goal.name

    params = _PersonalDataParams(
        user=subject_user,
        challenge=challenge,
        window_start=challenge.window_start_for(subject_participant),
        display_unit=display_unit,
        goal_label=goal_label,
    )

    # Window-independent, matching the leaderboard (D5/TASK-164): a rejoin
    # resets joined_at, which would otherwise blank cards for points that
    # still stand on the leaderboard.
    current_best_by_lift = {
        event.lift: event
        for event in PointEarnEvent.objects.filter(
            user=subject_user,
            challenge=challenge,
            is_current_best=True,
        )
    }

    rep_columns = [
        {"reps": n, "points": points_for_rep_count(n)} for n in range(10, 0, -1)
    ]

    standards_rows = []
    point_rows = []
    for lift in lift_names:
        current_best = current_best_by_lift.get(lift)
        threshold_at = _threshold_at_for_lift(lift, targets_by_lift=targets_by_lift)
        standards_rows.append(
            _standards_row_for_lift(
                lift,
                params,
                threshold_at=threshold_at,
                current_best=current_best,
                rep_columns=rep_columns,
            )
        )
        if current_best is not None:
            point_rows.append(
                {
                    "lift": lift,
                    "points_earned": current_best.points_earned,
                    "weight": _weight_display(
                        current_best.weight,
                        lift,
                        display_unit,
                        challenge,
                        snap=False,
                    ),
                    "reps": current_best.reps,
                    "date": current_best.performed_at,
                    "is_bodyweight_added": is_bodyweight_added_lift(lift),
                }
            )
        else:
            point_rows.append(
                {
                    "lift": lift,
                    "points_earned": 0,
                    "weight": None,
                    "reps": None,
                    "date": None,
                    "is_bodyweight_added": is_bodyweight_added_lift(lift),
                }
            )

    total_points = sum(row["points_earned"] for row in point_rows)

    # Provenance disclosure (D6): show the method label and, for STANDARDS
    # goals, the licensing-required FitnessVolt attribution. Deliberately
    # NEVER read sex/bodyweight_kg (the values TASK-248 worked to keep
    # ephemeral) or population/tier — FitnessVoltStandardCache is a
    # weight-class x percentile table per (population, lift, sex), so
    # (population, tier, sex) plus a displayed 1RM target inverts to the
    # subject's approximate bodyweight. Showing tier would be a false
    # comfort, not a compromise.
    source_detail = goal.source_detail
    provenance = {
        "method_label": goal.get_source_method_display(),
        "is_standards": goal.source_method == CustomGoal.SourceMethod.STANDARDS,
        "snapshot_version": None,
        "history_sentence": None,
    }
    if goal.source_method == CustomGoal.SourceMethod.STANDARDS:
        provenance["snapshot_version"] = source_detail.get("snapshot_version")
    elif goal.source_method == CustomGoal.SourceMethod.HISTORY:
        # Composed here (gettext + str.format), not as a template
        # {% blocktrans %}, matching the wizard's own uplift-sentence
        # pattern (challenges/views.py's source_note) — a literal "%" inside
        # a blocktrans body is doubled to "%%" by Django's own compiler,
        # which would silently desync from the source-string coverage test's
        # simpler extraction regex (see root/tests/test_translations.py).
        uplift_pct = source_detail.get("uplift")
        lookback_days = source_detail.get("lookback_days")
        sentence = gettext(
            "Suggested from history: +{percent}% uplift, based on the last {days} days"
        ).format(percent=f"{float(uplift_pct) * 100:g}", days=lookback_days)
        rounding_amount = source_detail.get("rounding_amount")
        rounding_unit = source_detail.get("rounding_unit")
        if rounding_amount is not None:
            sentence += " " + gettext(
                "(rounded to the nearest {amount} {unit})"
            ).format(amount=rounding_amount, unit=rounding_unit)
        provenance["history_sentence"] = sentence

    logger.debug(
        "Built participant chart for subject %s in challenge %s: %d lift(s)",
        subject_user.id,
        challenge.pk,
        len(lift_names),
    )

    return {
        "subject_name": subject_user.display_name or subject_user.username,
        "goal_name": goal_label,
        "locked_at": goal.created_at,
        "provenance": provenance,
        "standards_rows": standards_rows,
        "point_rows": point_rows,
        "rep_columns": rep_columns,
        "display_unit": display_unit,
        "total_points": total_points,
    }


def custom_targets_from_goal(goal) -> dict:
    """Read a saved CustomGoal's rows back into a ``{lift: {rep: kg}}`` table."""
    targets: dict[str, dict] = {}
    for target in goal.targets.all():
        targets.setdefault(target.lift, {})[target.rep_count] = target.target_weight
    return targets


def build_custom_goal_context(
    user,
    challenge,
    *,
    method=CustomGoal.SourceMethod.CUSTOM,
    targets_json="",
    targets=None,
    errors=None,
    unavailable_lifts=None,
    assisted_only_lifts=None,
    source_note="",
    unknown_lifts=None,
    acknowledge_unknown_lifts=False,
    computed_fields=None,
) -> dict:
    """Build the render context for the goal-setup wizard's "chart" step.

    ``targets`` is a ``{lift: {rep: kg}}`` table used to prefill the grid —
    a standards/history suggestion, or a partially-parsed submission on a
    failed save. Each cell carries its POST field name and its value
    converted to the user's display unit.

    ``unavailable_lifts`` (AC#3 — no history/no published standard/no
    bodyweight entered) and ``assisted_only_lifts`` (TASK-248 plan §1b — a
    bodyweight-added lift whose recent history is entirely machine-assisted,
    so it will never score) both mark rows the participant must decide on
    explicitly rather than receiving a silent computed default.

    JSON-paste (``is_json_method``) is its own top-level goal-setup method
    (TASK-306), not a toggle inside the manual-entry screen: pasting JSON
    over a standards/history-prefilled grid would let the saved targets
    diverge from what ``source_detail`` claims produced them, silently
    mislabelling the goal's provenance. ``CustomGoalForm`` enforces this
    server-side too (§ its ``clean``), so this is belt only. The Compute
    calculator (``show_calculator``) is offered only on the manual-entry
    (CUSTOM) grid — the standards/history grids are already fully prefilled,
    and JSON has no grid at all.

    ``unknown_lifts`` (TASK-314) names lift(s) present in a JSON-pasted
    payload but not configured for the challenge -- passed through from
    ``CustomGoalForm.unknown_lifts`` on a failed submit so the template can
    offer an explicit "ignore and continue" checkbox rather than silently
    dropping them or always blocking the save. ``acknowledge_unknown_lifts``
    echoes back whatever the checkbox's raw POST value was, so a re-render
    (e.g. because acknowledging alone wasn't enough -- another error was
    also present) doesn't reset a checkbox the user already ticked.

    ``computed_fields`` (a set of grid field names, e.g. ``{"target__0__1"}``)
    is which cells the Compute calculator filled in on the client, echoed
    back from the hidden ``computed_fields`` POST field so a failed submit
    (e.g. a non-monotonic table) can re-render each cell's "computed" vs.
    "pinned" styling correctly instead of the client-side JS defaulting
    every non-blank cell to "pinned" on a fresh page load -- that distinction
    only ever lived in transient DOM state before this, so a server
    round-trip used to silently erase it.
    """
    unit = user.unit_preference
    targets = targets or {}
    unavailable_lifts = unavailable_lifts or set()
    assisted_only_lifts = assisted_only_lifts or set()
    computed_fields = computed_fields or set()
    lifts = []
    for lift_index, lift in enumerate(sorted(covered_lift_names(challenge))):
        lift_targets = targets.get(lift) or {}
        cells = []
        # Rendered 10RM..1RM (easiest-to-hardest, left to right), matching the
        # rep_range convention every other rep-max table in this app uses.
        for rep in range(10, 0, -1):
            kg = lift_targets.get(rep)
            value = ""
            if kg is not None:
                display, _ = to_display_weight(kg, unit)
                value = display
            field = grid_field_name(lift_index, rep)
            cells.append(
                {
                    "rep": rep,
                    "field": field,
                    "value": value,
                    "is_computed": field in computed_fields,
                }
            )
        needs_decision = lift in unavailable_lifts
        needs_decision_reason = ""
        if needs_decision:
            needs_decision_reason = (
                gettext(
                    "Recent history for this lift is entirely machine-assisted, "
                    "which can't be converted into a target."
                )
                if lift in assisted_only_lifts
                else gettext(
                    "No recent history or published standard for this lift yet."
                )
            )
        lifts.append(
            {
                "name": lift,
                "is_bodyweight_added": is_bodyweight_added_lift(lift),
                "cells": cells,
                "needs_decision": needs_decision,
                "needs_decision_reason": needs_decision_reason,
            }
        )
    return {
        "challenge": challenge,
        "method": method,
        "is_json_method": method == CustomGoal.SourceMethod.JSON,
        "show_calculator": method == CustomGoal.SourceMethod.CUSTOM,
        "display_unit": unit,
        "rep_range": [
            {"reps": n, "points": points_for_rep_count(n)} for n in range(10, 0, -1)
        ],
        "lifts": lifts,
        "targets_json": targets_json,
        "llm_prompt": _custom_goal_llm_prompt(challenge, lifts, unit),
        "errors": errors or [],
        "unavailable_lifts": sorted(unavailable_lifts),
        "assisted_only_lifts": sorted(assisted_only_lifts),
        "source_note": source_note,
        "unknown_lifts": unknown_lifts or [],
        "acknowledge_unknown_lifts": acknowledge_unknown_lifts,
    }


def build_rep_target_goal_context(
    user,
    challenge,
    *,
    targets=None,
    field_values=None,
    suggested_fields=None,
    errors=None,
    source_note="",
) -> dict:
    """Build the render context for the Rep Target goal-setup form.

    ``targets`` is a ``{lift: (target_weight_kg, target_reps)}`` table used to
    prefill the grid; ``field_values`` (``{field_name: display_value}``, from
    :func:`challenges.rep_target_goals.merge_suggested_fields`) takes
    precedence when given, so a re-render can echo the participant's raw
    per-field input instead of only fully-parsed rows. ``suggested_fields``
    marks the fields the "Suggest targets" convenience (not the participant)
    filled, for the grid's suggested-cell styling and the hidden input that
    persists that set across re-renders. A lift the suggester
    couldn't prefill (:func:`challenges.goal_builders.suggest_rep_targets_from_history`)
    is surfaced via a toast in the view, not a per-row flag here -- an
    inline grid row broke the grid's spacing (UAT feedback).

    Rows start empty unless ``targets`` fills them, with one exception: a
    bodyweight-added lift's weight opens at 0. An earlier attempt at this
    defaulted 0 weight and 10 reps for *every* row, so a challenge mixing
    those lifts with barbell ones opened showing "0" against a bench press --
    a suggestion nobody asked for. Scoped to the lifts where 0 is the
    meaningful default, and read beside the row's "BW +" affix, it says
    "unweighted" rather than "target: nothing". Reps are still left blank
    (10 was a guess), and the value is deliberately NOT added to
    ``suggested_fields``: that set means "the history suggester filled this",
    round-trips through the hidden input, and decides whether the saved goal
    records HISTORY or CUSTOM provenance.
    """
    unit = user.unit_preference
    targets = targets or {}
    suggested_fields = suggested_fields or set()
    configured = covered_lift_names(challenge)
    bw_added_lifts = _bodyweight_added_lift_names(set(configured))
    lifts = []
    for lift_index, lift in enumerate(sorted(configured)):
        weight_field, reps_field = rep_target_field_names(lift_index)
        if field_values is not None:
            weight_value = field_values.get(weight_field, "")
            reps_value = field_values.get(reps_field, "")
        else:
            weight_kg, reps = targets.get(lift, (None, None))
            # Bodyweight-added lifts open at 0 -- the added weight for an
            # unweighted set, and by far the common case for them. It is the
            # one value that can be filled in without guessing at anything
            # about the participant, and beside the row's "BW +" affix it
            # reads as "BW + 0", not as a target of zero.
            weight_value = "0" if lift in bw_added_lifts else ""
            if weight_kg is not None:
                weight_value, _ = to_display_weight(weight_kg, unit)
            reps_value = reps if reps is not None else ""
        lifts.append(
            {
                "name": lift,
                "is_bodyweight_added": lift in bw_added_lifts,
                "weight_field": weight_field,
                "weight_value": weight_value,
                "weight_suggested": weight_field in suggested_fields,
                "reps_field": reps_field,
                "reps_value": reps_value,
                "reps_suggested": reps_field in suggested_fields,
            }
        )
    return {
        "challenge": challenge,
        "display_unit": unit,
        "lifts": lifts,
        "suggested_fields": ",".join(sorted(suggested_fields)),
        "errors": errors or [],
        "source_note": source_note,
    }


def _custom_goal_llm_prompt(challenge, lifts, unit) -> str:
    """Build a copy-paste prompt that guides an LLM to produce the targets JSON.

    Names every lift configured on the challenge so the assistant knows
    exactly what to ask the participant for, and embeds the expected schema.
    """
    names = [lift["name"] for lift in lifts]
    skeleton = {
        "unit": unit,
        "targets": {name: {str(rep): 0 for rep in range(1, 11)} for name in names},
    }
    lift_lines = []
    any_bodyweight_added = False
    for lift in lifts:
        if lift["is_bodyweight_added"]:
            any_bodyweight_added = True
            suffix = (
                " (enter the ADDED weight relative to my bodyweight: 0 for "
                "bodyweight-only, negative if band-assisted — a leverage-machine "
                "assisted set can't score on this lift at all, so don't count it)"
            )
        else:
            suffix = ""
        lift_lines.append(f"- {lift['name']}{suffix}")
    return "\n".join(
        [
            "I want to set my strength targets for a lifting challenge "
            f'called "{challenge.name}".',
            "",
            "The challenge uses these lifts:",
            *lift_lines,
            "",
            "Please ask me for my target weight at each rep count from 1 to 10 for "
            "every lift above (the most weight I expect to lift for that number of "
            f"reps). All weights are in {unit}.",
            "",
            "Once you have my numbers, output a single JSON object in exactly this "
            "schema and nothing else — no commentary — so I can paste it straight in:",
            "",
            json.dumps(skeleton, indent=2),
            "",
            "Requirements:",
            "- Include every lift listed above, spelled exactly as shown.",
            '- Include all ten rep counts ("1" through "10") for each lift; missing '
            "entries are rejected.",
            '- "unit" must be "kg" or "lb".',
            (
                "- Every weight is a positive number (decimals allowed), except "
                "the added-weight lifts noted above, where 0 (bodyweight-only) "
                "and negative (band-assisted) values are allowed."
                if any_bodyweight_added
                else "- Every weight is a positive number (decimals allowed)."
            ),
        ]
    )


def delete_draft_challenge(challenge) -> None:
    """Soft-delete a draft challenge its creator no longer wants (#1).

    Reuses the existing CANCELLED status rather than inventing a "deleted"
    one: cancelled challenges are already excluded from find_challenges_view,
    the dashboard, and get_co_participants, which is exactly the
    "gone from my view" behaviour a deleted draft needs, and it keeps a
    single terminal-void status instead of two that mean almost the same
    thing. Nothing is hard-deleted -- the Challenge row, its ChallengeLift
    rows, the creator's ChallengeParticipant row and its invite link all
    survive for audit, matching this app's no-hard-delete policy. The
    caller (the view) is responsible for validating challenge.status ==
    DRAFT before calling this -- it is not re-checked here.
    """
    challenge.status = Challenge.Status.CANCELLED
    challenge.save(update_fields=["status"])
    logger.info("Draft challenge %s deleted (cancelled) by its creator", challenge.pk)


def activate_draft_for_creator(challenge, user) -> bool:
    """Flip a draft challenge to active when its creator finishes goal setup.

    Returns True when the transition happened, False otherwise (already active,
    or the acting user is not the creator).
    """
    if challenge.status == Challenge.Status.DRAFT and challenge.creator_id == user.id:
        challenge.status = Challenge.Status.ACTIVE
        challenge.save(update_fields=["status"])
        logger.info(
            "Challenge %s flipped draft->active by creator %s",
            challenge.pk,
            user.id,
        )
        return True
    return False
