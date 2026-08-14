"""Service functions for challenge lifecycle operations."""

import json
import logging
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.db.models import Case, CharField, F, When
from django.db.models.functions import Lower
from django.utils import timezone
from django.utils.translation import gettext

from accounts.timezones import is_valid_timezone
from accounts.units import to_display_weight
from challenges.custom_goals import grid_field_name
from challenges.models import (
    Challenge,
    ChallengeInviteLink,
    ChallengeLift,
    ChallengeParticipant,
    CustomGoal,
    CustomGoalTarget,
)
from challenges.standards import covered_lift_names
from liftosaur.models import LiftHistory, LiftSource
from liftosaur.services import sync_user_lifts
from notifications.models import Notification
from scoring.domain.calculator import (
    best_score_for_set,
    format_added_weight,
    is_assisted_equipment,
    is_bodyweight_added_lift,
)
from scoring.models import PointEarnEvent
from scoring.services import score_pooled_history

# Default rep-count the self-report carousel opens on for a lift with no
# PointEarnEvent ever logged -- 10RM, the easiest target (TASK-25 design
# review). A lift with any history opens on the most recently logged rep
# count instead; see _default_manual_rep_count.
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
    """
    if sync:
        sync_user_lifts(user)
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
    """
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
    """The challenge's single live (non-revoked, non-expired) invite link, if any."""
    return ChallengeInviteLink.objects.filter(
        challenge=challenge,
        revoked_at__isnull=True,
        expires_at__gt=timezone.now(),
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
    creator = challenge.creator
    for tz_name in (creator.timezone, creator.detected_timezone):
        if tz_name and is_valid_timezone(tz_name):
            return ZoneInfo(tz_name)
    return ZoneInfo("UTC")


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
    """End-of-day expiry for ``challenge.end_date``, with a safety-net fallback.

    CHALLENGES_INVITE_LINK_TTL_DAYS used to be the *only* expiry rule; now it
    only backstops the rare case where end_date is bad data (e.g. already in
    the past), so a fresh link is never minted pre-expired. Kept as a setting
    deliberately for that reason rather than dropped.
    """
    end_of_day = challenge_display_end_of_day(challenge, challenge.end_date)
    if end_of_day <= timezone.now():
        return timezone.now() + timedelta(days=settings.CHALLENGES_INVITE_LINK_TTL_DAYS)
    return end_of_day


def regenerate_invite_link(
    challenge, by_user, *, expires_at=None, max_uses=None
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
    ``_default_invite_link_expiry``. ``max_uses`` is None for unlimited uses;
    the two overrides are independent of each other and combinable. A fresh
    row's use_count always starts at 0, never carried over from the revoked
    incumbent.

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
            expires_at=expires_at or _default_invite_link_expiry(challenge),
            max_uses=max_uses,
        )
    logger.info(
        "Regenerated invite link %s for challenge %s by user %s (expires_at=%s, "
        "max_uses=%s)",
        link.pk,
        challenge.pk,
        by_user.id,
        link.expires_at.isoformat(),
        max_uses,
    )
    return link


def update_invite_link(link, *, expires_at=None, max_uses=None) -> ChallengeInviteLink:
    """Adjust ``link``'s expiry/max-uses in place, without touching its token.

    Unlike ``regenerate_invite_link``, this keeps the same row (and therefore
    the same shareable URL and use_count) live -- it's for an owner tweaking
    an already-shared link's limits, not invalidating it. ``expires_at``
    missing/blank falls back to the challenge's default (its own end_date),
    same as a fresh link would get; ``max_uses`` missing/blank means
    unlimited. Both are independent, matching ``regenerate_invite_link``.
    """
    link.expires_at = expires_at or _default_invite_link_expiry(link.challenge)
    link.max_uses = max_uses
    link.save(update_fields=["expires_at", "max_uses"])
    logger.info(
        "Updated invite link %s for challenge %s (expires_at=%s, max_uses=%s)",
        link.pk,
        link.challenge_id,
        link.expires_at.isoformat(),
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
        ChallengeInviteLink.objects.select_related("challenge")
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


def remove_participant(participant) -> None:
    """Remove a participant from a challenge (creator/staff moderation action).

    Removal is bail-plus-flag: is_bailed/bailed_at carry the scoring freeze so
    every existing bail filter applies unchanged, while removed_by_creator marks
    this as a creator-initiated removal (blocks self-rejoin, drives the REMOVED
    notification). Removal is permanent for V1 — nothing clears the flag, and no
    invite link readmits a removed user. The removed user's PointEarnEvent
    history is left untouched, so their scored entries stay on the leaderboard
    exactly like a voluntarily-bailed lifter.

    State validation (ACCEPTED, not already bailed, challenge not locked) is
    the caller's responsibility — the view owns those guards, matching bail_view.
    """
    with transaction.atomic():
        participant.is_bailed = True
        participant.bailed_at = datetime.now(tz=UTC)
        participant.removed_by_creator = True
        participant.save(update_fields=["is_bailed", "bailed_at", "removed_by_creator"])
        Notification.objects.create(
            user=participant.user,
            event_type=Notification.EventType.REMOVED_FROM_CHALLENGE,
            challenge=participant.challenge,
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
    logger.info(
        "Challenge %s ownership transferred from %s to %s",
        challenge.pk,
        old_creator_id,
        new_owner.id,
    )


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
       including bailed ones — they were part of the challenge.

    Idempotent: returns immediately if the challenge is already completed.
    """
    if challenge.status == Challenge.Status.COMPLETED:
        logger.info("close_challenge: %s already completed; no-op", challenge.id)
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
    logger.info("Challenge %s closed", challenge.id)

    accepted_participants = ChallengeParticipant.objects.filter(
        challenge=challenge,
        invite_status=ChallengeParticipant.InviteStatus.ACCEPTED,
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


def _default_manual_rep_count(most_recent_event):
    """Starting rep count for a summary card's self-report carousel (TASK-25).

    No scoring history for this lift -> 10RM, the easiest target. Otherwise ->
    the rep count the most recently logged event satisfied (``11 -
    points_earned``, same derivation as everywhere else that maps a
    PointEarnEvent back to a rep count) -- deliberately the *most recent*
    entry, not the current best: a lifter whose last session was worse than
    their all-time best should open on what they just did, not get steered
    toward re-confirming an old best.

    "No scoring history" has to include a zero-point event, not just a missing
    one: :func:`scoring.services._persist_audit_row` records sub-threshold
    sets as real zero-point audit rows, so a lifter who has logged sets but
    never earned a point does have a most-recent event. Running the usual
    derivation on it gives ``11 - 0 == 11``, a rep count no carousel entry has
    (they run 10RM..1RM), which lands on the client's not-found fallback
    instead of any deliberate choice.
    """
    if most_recent_event is None or most_recent_event.points_earned == 0:
        return MANUAL_DEFAULT_REP_COUNT_NO_HISTORY
    return 11 - most_recent_event.points_earned


def _standards_row_for_lift(lift, params, *, threshold_at, current_best, rep_columns):
    """Build one lift's standards-table row (10RM..1RM cells)."""
    cells = []
    for reps in rep_columns:
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
    """
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

    # Window-independent like current_best_by_lift above, and for the same
    # reason (a bail/rejoin resetting joined_at shouldn't make the carousel
    # forget history that still stands on the leaderboard). setdefault (not a
    # dict comprehension) because this is genuinely one-per-lift out of many
    # events per lift, not an already-unique is_current_best=True row.
    most_recent_event_by_lift = {}
    for event in PointEarnEvent.objects.filter(user=user, challenge=challenge).order_by(
        "-performed_at", "-synced_at"
    ):
        most_recent_event_by_lift.setdefault(event.lift, event)

    rep_columns = list(range(10, 0, -1))

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
            most_recent_event_by_lift.get(card["lift"])
        )

    return {
        "summary_cards": summary_cards,
        "standards_rows": standards_rows,
        "rep_columns": rep_columns,
        "display_unit": display_unit,
        "goal_label": goal_label,
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
    """
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

    rep_columns = list(range(10, 0, -1))

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
    goal_name="",
    targets_json="",
    targets=None,
    errors=None,
    unavailable_lifts=None,
    assisted_only_lifts=None,
    source_note="",
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

    The JSON-paste path (``allow_json``) is offered only for the CUSTOM
    method: pasting JSON over a standards/history-prefilled grid would let
    the saved targets diverge from what ``source_detail`` claims produced
    them, silently mislabelling the goal's provenance. ``CustomGoalForm``
    enforces this server-side too (§ its ``clean``), so this is belt only.
    """
    unit = user.unit_preference
    targets = targets or {}
    unavailable_lifts = unavailable_lifts or set()
    assisted_only_lifts = assisted_only_lifts or set()
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
            cells.append(
                {
                    "rep": rep,
                    "field": grid_field_name(lift_index, rep),
                    "value": value,
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
    allow_json = method == CustomGoal.SourceMethod.CUSTOM
    return {
        "challenge": challenge,
        "display_unit": unit,
        "rep_range": list(range(10, 0, -1)),
        "lifts": lifts,
        "goal_name": goal_name,
        "targets_json": targets_json,
        "json_active": allow_json and bool(targets_json.strip()),
        "allow_json": allow_json,
        "llm_prompt": _custom_goal_llm_prompt(challenge, lifts, unit),
        "errors": errors or [],
        "unavailable_lifts": sorted(unavailable_lifts),
        "assisted_only_lifts": sorted(assisted_only_lifts),
        "source_note": source_note,
    }


def _custom_goal_llm_prompt(challenge, lifts, unit) -> str:
    """Build a copy-paste prompt that guides an LLM to produce the targets JSON.

    Names every lift configured on the challenge so the assistant knows
    exactly what to ask the participant for, and embeds the expected schema.
    """
    names = [lift["name"] for lift in lifts]
    skeleton = {
        "name": "Spring targets",
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
            'Please ask me for a short name/label for this goal (e.g. "Spring '
            'targets") and my target weight at each rep count from 1 to 10 for '
            "every lift above (the most weight I expect to lift for that number of "
            f"reps). All weights are in {unit}.",
            "",
            "Once you have my numbers, output a single JSON object in exactly this "
            "schema and nothing else — no commentary — so I can paste it straight in:",
            "",
            json.dumps(skeleton, indent=2),
            "",
            "Requirements:",
            '- Include a "name" key with the goal\'s short name/label.',
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
