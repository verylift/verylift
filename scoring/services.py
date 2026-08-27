"""Scoring service layer — ORM reads/writes that wrap the pure domain functions."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from django.db import transaction

from accounts.units import to_display_weight
from challenges.models import Challenge, ChallengeParticipant
from challenges.standards import covered_lift_names
from liftosaur.models import Lift, LiftHistory, LiftSource
from notifications.models import Notification
from scoring.domain.calculator import (
    best_score_for_rep_target,
    best_score_for_set,
    is_assisted_equipment,
    is_bodyweight_added_lift,
)
from scoring.models import PointEarnEvent

logger = logging.getLogger(__name__)


class _GoalTargets:
    """A participant's flat per-lift, per-rep target table (kg).

    Every challenge is CUSTOM (TASK-248 plan §3): there is no bodyweight-scaled
    standard left in scoring, so this is a thin, deliberately dumb wrapper
    around a single prefetch of the participant's CustomGoal — not a dispatch
    across standards sources. Prefetching once per resolver (rather than once
    per set) keeps a full-history backfill at one query for the whole run.
    """

    def __init__(self, *, custom_goal=None):
        self._targets: dict[str, dict[int, Decimal]] = {}
        if custom_goal is not None:
            for lift, rep_count, target_weight in custom_goal.targets.values_list(
                "lift", "rep_count", "target_weight"
            ):
                self._targets.setdefault(lift, {})[rep_count] = target_weight

    def targets_for(self, lift: str) -> dict[int, Decimal] | None:
        """Return the ``{rep_count: kg}`` target table for a lift, or None.

        Targets are absolute weight for every lift: for bodyweight-added lifts
        (Chin-up/Pull-up/Dip) the target IS the added weight and the recorded
        LiftHistory weight IS the added weight, so this is returned verbatim —
        no bodyweight arithmetic anywhere in scoring. None means the
        participant's goal does not cover this lift, which callers treat as a
        scoring no-op.
        """
        return self._targets.get(lift) or None


class _RepTargetGoalTargets:
    """A participant's flat per-lift ``{lift: (target_weight_kg, target_reps)}``
    table for a REP_TARGET challenge -- the sibling of _GoalTargets above,
    same "one prefetch per resolver" shape.
    """

    def __init__(self, *, rep_target_goal=None):
        self._targets: dict[str, tuple[Decimal, int]] = {}
        if rep_target_goal is not None:
            for lift, target_weight, target_reps in rep_target_goal.targets.values_list(
                "lift", "target_weight", "target_reps"
            ):
                self._targets[lift] = (target_weight, target_reps)

    def targets_for(self, lift: str) -> tuple[Decimal, int] | None:
        return self._targets.get(lift)


@dataclass
class ScoringSummary:
    """Outcome counts for a single score_pooled_history run."""

    sets_evaluated: int = 0
    new_point_events: int = 0


def process_scored_set(
    user,
    challenge,
    lift: str,
    performed_at: date,
    reps: int,
    weight: Decimal,
    synced_at: datetime,
    equipment: str = "",
    notify: bool = True,
    *,
    participant: ChallengeParticipant | None = None,
    resolver: _GoalTargets | None = None,
    is_bodyweight_added: bool | None = None,
    source: str = LiftSource.LIFTOSAUR,
) -> PointEarnEvent | None:
    """Evaluate a performed set and persist a PointEarnEvent if appropriate.

    Orchestrates: ledger-lock and eligibility guards, threshold resolution
    (delegated to _GoalTargets — a flat per-lift, per-rep table, no bodyweight
    anywhere), domain scoring, then persistence (delegated to _persist_audit_row
    for sub-threshold sets or _persist_best for threshold-meeting ones).

    ``weight`` is compared and persisted exactly as recorded: for
    bodyweight-added lifts (Chin-up/Pull-up/Dip) it IS the added weight, and
    the participant's target for that lift IS also added weight, so no
    conversion happens here at all (TASK-248 — the whole bodyweight round-trip
    this function used to do is gone).

    Assisted-machine sets (leverage machine) on a bodyweight-added lift are
    excluded from scoring entirely: their recorded weight is net total load,
    not added weight, and is not comparable to an added-weight target without
    a bodyweight to convert with — one no longer exists in this product. This
    is a silent-wrong-answer guard, not a defensive nice-to-have: without it,
    an assisted set's net-load number would satisfy nearly every target (see
    TASK-248 plan §1b and scoring/tests/test_static_threshold_scoring.py).

    When ``notify`` is False the leaderboard is not snapshotted and no overtaken
    notifications are fired for this set. The bulk backfill path
    (score_pooled_history) passes notify=False and diffs the leaderboard once for
    the whole run rather than per set, so a first-time history import does not
    re-rank the board on each of the lifter's own progressive PRs. The default
    (True) preserves the incremental single-set behaviour.

    ``participant``, ``resolver`` and ``is_bodyweight_added`` let the bulk
    backfill resolve once per run and pass them down, avoiding a participant
    query, a targets lookup, and a Lift ``exists()`` query per set. All three
    default to None, in which case the single-set path resolves them itself.

    Returns the newly-created PointEarnEvent, or None if nothing was written.
    """
    # Ledger lock: a completed or cancelled challenge takes no further writes.
    if challenge.status in (
        Challenge.Status.COMPLETED,
        Challenge.Status.CANCELLED,
    ):
        return None

    if participant is None:
        try:
            participant = ChallengeParticipant.objects.get(
                user=user, challenge=challenge
            )
        except ChallengeParticipant.DoesNotExist:
            return None

    # Eligibility guards. A frozen (bailed) ledger blocks any source. A
    # participant with no goal yet configured for their challenge's mode —
    # including, deliberately, any participant row that predates this task and
    # so has no goal at all (there is no legacy backfill, TASK-248 revision 5)
    # — is a clean scoring no-op, indistinguishable from someone who joined
    # and abandoned goal setup: listed, scoring nothing, no exception.
    if participant.is_bailed:
        return None
    is_rep_target = challenge.mode == Challenge.Mode.REP_TARGET
    goal_id = (
        participant.rep_target_goal_id if is_rep_target else participant.custom_goal_id
    )
    if goal_id is None:
        return None

    if is_bodyweight_added is None:
        is_bodyweight_added = is_bodyweight_added_lift(lift)
    if is_bodyweight_added and is_assisted_equipment(equipment):
        return None

    if resolver is None:
        resolver = (
            _RepTargetGoalTargets(rep_target_goal=participant.rep_target_goal)
            if is_rep_target
            else _GoalTargets(custom_goal=participant.custom_goal)
        )
    target = resolver.targets_for(lift)
    if target is None:
        return None

    # The weight comparison is exact — no fuzz band. Every target is a static,
    # entered-once weight; there is no bodyweight drift to absorb.
    if is_rep_target:
        target_weight, target_reps = target
        points_earned = best_score_for_rep_target(
            reps, weight, target_reps, target_weight
        )
    else:
        result = best_score_for_set(reps, weight, target)
        points_earned = result[0] if result is not None else None

    if points_earned is None:
        return _persist_audit_row(
            user=user,
            challenge=challenge,
            lift=lift,
            performed_at=performed_at,
            synced_at=synced_at,
            reps=reps,
            weight=weight,
            equipment=equipment,
            source=source,
        )

    return _persist_best(
        user=user,
        challenge=challenge,
        lift=lift,
        performed_at=performed_at,
        synced_at=synced_at,
        reps=reps,
        weight=weight,
        points_earned=points_earned,
        equipment=equipment,
        notify=notify,
        source=source,
    )


def _persist_audit_row(
    *,
    user,
    challenge,
    lift,
    performed_at,
    synced_at,
    reps,
    weight,
    equipment,
    source=LiftSource.LIFTOSAUR,
) -> PointEarnEvent:
    """Persist a sub-threshold set as a zero-point, non-current-best audit row.

    The lift still appears in the user's history (powering the no_points summary
    card), but the row never becomes current best and never touches the
    leaderboard or notifications.
    """
    return PointEarnEvent.objects.create(
        user=user,
        challenge=challenge,
        lift=lift,
        performed_at=performed_at,
        synced_at=synced_at,
        reps=reps,
        weight=weight,
        points_earned=0,
        is_current_best=False,
        equipment=equipment,
        source=source,
    )


def _persist_best(
    *,
    user,
    challenge,
    lift,
    performed_at,
    synced_at,
    reps,
    weight,
    points_earned,
    equipment,
    notify,
    source=LiftSource.LIFTOSAUR,
) -> PointEarnEvent:
    """Persist a threshold-meeting set, promoting it to current best if it beats
    the current high-watermark for (user, challenge, lift).

    Wrapped in a transaction; current best is fetched with select_for_update to
    serialise concurrent writes for the same slot. When ``notify`` the leaderboard
    is snapshotted before and after and overtaken notifications fire on the net
    change; the bulk backfill passes notify=False and diffs once for the run.
    """
    with transaction.atomic():
        current_best = (
            PointEarnEvent.objects.select_for_update()
            .filter(
                user=user,
                challenge=challenge,
                lift=lift,
                is_current_best=True,
            )
            .first()
        )

        is_new_best = current_best is None or points_earned > current_best.points_earned

        should_diff = is_new_best and notify
        leaderboard_before = rank_participants(challenge) if should_diff else None

        if is_new_best and current_best is not None:
            current_best.is_current_best = False
            current_best.save(update_fields=["is_current_best"])

        new_event = PointEarnEvent.objects.create(
            user=user,
            challenge=challenge,
            lift=lift,
            performed_at=performed_at,
            synced_at=synced_at,
            reps=reps,
            weight=weight,
            points_earned=points_earned,
            is_current_best=is_new_best,
            equipment=equipment,
            source=source,
        )

        if should_diff:
            leaderboard_after = rank_participants(challenge)
            notify_ranking_changes(
                challenge, compute_ranking_deltas(leaderboard_before, leaderboard_after)
            )

    return new_event


def score_pooled_history(*, user, challenge) -> ScoringSummary:
    """Score a participant's pooled LiftHistory for a challenge, idempotently.

    Operates purely on the local DB: no Liftosaur API calls, no LiftosaurSyncLog.
    Evaluates every LiftHistory row for the user that falls within the
    challenge window and matches the participant's configured lifts, feeding
    it through process_scored_set. Scoring is decoupled from any delta fetch:
    the whole pool is considered every run, and rows already carrying a
    PointEarnEvent are skipped, so re-scoring never duplicates PointEarnEvents
    or double-counts.

    Overtaken notifications are diffed once for the whole run: the leaderboard is
    snapshotted before scoring begins and compared to a single post-run snapshot,
    so participants are notified only on the net standings change. Per-set diffing
    is suppressed (notify=False on process_scored_set) because a first-time
    backfill scores many of the lifter's own progressive PRs in one pass, and
    re-ranking on each would fire spurious 'overtaken' notifications for transient
    intermediate standings that never existed as a final state (TASK-125).

    Returns a ScoringSummary of how many pooled sets were evaluated and how many
    new PointEarnEvents were created.
    """
    summary = ScoringSummary()

    participant = ChallengeParticipant.objects.filter(
        user=user, challenge=challenge
    ).first()
    if participant is None:
        logger.warning(
            "Cannot score pooled history: user %s is not a participant in challenge %s",
            user.id,
            challenge.id,
        )
        return summary

    standard_lifts = covered_lift_names(challenge)
    window_start = challenge.window_start_for(participant)
    synced_at = datetime.now(tz=UTC)

    # Resolve once per run, not per set (avoids redundant queries on a large
    # backfill): the participant is already in hand; build one target-table
    # lookup and read the bodyweight-added lift set in a single query so the
    # assisted-equipment skip rule never hits the Lift table per set.
    resolver = (
        _RepTargetGoalTargets(rep_target_goal=participant.rep_target_goal)
        if challenge.mode == Challenge.Mode.REP_TARGET
        else _GoalTargets(custom_goal=participant.custom_goal)
    )
    bodyweight_added_lifts = set(
        Lift.objects.filter(
            name__in=standard_lifts, is_bodyweight_added=True
        ).values_list("name", flat=True)
    )

    rows = LiftHistory.objects.filter(user=user, lift__in=standard_lifts)
    if window_start is not None:
        rows = rows.filter(performed_at__gte=window_start.date())

    # A pooled LiftHistory row is unique on (user, lift, performed_at, reps,
    # weight_kg, equipment), so a PointEarnEvent already recorded for that same
    # set is the marker that it was scored on a prior run. Skipping those keeps
    # re-scoring the whole pool idempotent: no duplicate rows, no
    # double-counting, current best preserved. Equipment is part of the key so
    # an ordinary set and an assisted set of the same lift, day and rep count
    # are never collapsed into one — assisted rows never score at all on a
    # bodyweight-added lift (see the skip rule below), so the two must stay
    # distinguishable even when their recorded weight happens to coincide.
    already_scored = set(
        PointEarnEvent.objects.filter(
            user=user, challenge=challenge, lift__in=standard_lifts
        ).values_list("lift", "performed_at", "reps", "weight", "equipment")
    )

    # Snapshotted lazily just before the first set is scored so a fully-idempotent
    # re-run (every row already scored) issues zero leaderboard queries.
    leaderboard_before: list[dict] | None = None

    for row in rows.iterator():
        is_bodyweight_added = row.lift in bodyweight_added_lifts
        if is_bodyweight_added and is_assisted_equipment(row.equipment):
            # Silent-wrong-answer guard (TASK-248 plan §1b): an assisted set's
            # recorded weight is net total load, not added weight, and cannot
            # be compared to an added-weight target without a bodyweight to
            # convert with. Skip before the idempotency lookup too, so an
            # assisted row is never mistaken for an already-scored one.
            continue

        # weight_kg is compared and persisted exactly as recorded (no
        # bodyweight arithmetic anywhere) — see process_scored_set.
        key = (row.lift, row.performed_at, row.reps, row.weight_kg, row.equipment)
        if key in already_scored:
            continue

        if leaderboard_before is None:
            leaderboard_before = rank_participants(challenge)

        summary.sets_evaluated += 1
        event = process_scored_set(
            user=user,
            challenge=challenge,
            lift=row.lift,
            performed_at=row.performed_at,
            reps=row.reps,
            weight=row.weight_kg,
            synced_at=synced_at,
            equipment=row.equipment,
            notify=False,
            participant=participant,
            resolver=resolver,
            is_bodyweight_added=is_bodyweight_added,
            source=row.source,
        )
        if event is not None:
            summary.new_point_events += 1

    # Diff the leaderboard once for the whole run: notify only on the net change
    # in standings, not on each intermediate PR scored during the backfill.
    if summary.new_point_events:
        leaderboard_after = rank_participants(challenge)
        notify_ranking_changes(
            challenge, compute_ranking_deltas(leaderboard_before, leaderboard_after)
        )

    return summary


def build_points_over_time(challenge, *, top_n: int | None = None) -> dict:
    """Build Chart.js line-chart data of cumulative points per participant.

    For each accepted, non-bailed participant, the value at any date is the
    running high-watermark total *as of that date*: for each lift, the best
    points they had earned in it from any set performed on or before that date,
    summed across lifts.

    This is deliberately computed from *every* scoring event, superseded ones
    included, rather than from the is_current_best=True rows alone. Filtering to
    current-best would make the chart a projection of today's leaderboard onto
    the calendar rather than a history: when a lifter re-earns a lift, the
    beaten row flips to is_current_best=False (see _persist_best) and would
    vanish from the past, retroactively dropping the date it was set to zero and
    re-attributing its points to the newer date. Traces would rewrite themselves
    as the challenge progressed. Reading the high-watermark per lift per date
    off the full event set instead means a point on the line never changes once
    drawn, and each trace is monotonically non-decreasing.

    A superseded row still contributes only the *difference* it was worth at the
    time, never a second helping: taking the per-lift maximum (not a sum) is
    what keeps a 2-point squat followed by a 6-point squat reading 2 then 6
    rather than 2 then 8. That also keeps each trace's final value equal to the
    lifter's leaderboard total, since the current-best row for a lift is by
    construction its highest-scoring event.

    A set entered after the fact -- backdating a session in the source app to
    capture a lift someone forgot to record -- correctly raises the curve at the
    date it was *performed*, not the date it was synced. That is a change to an
    already-drawn trace, but it is the honest one: the lifter really did hold
    those points then, and performed_at is what every other scoring path
    (window filtering, Recent Activity) already keys on.

    Zero-point rows (sub-threshold sets, persisted as an audit trail by
    _persist_no_points) are excluded outright. They can never raise a per-lift
    maximum, so they cannot move a trace; including them would only add label
    dates to the shared x-axis where every line is flat.

    The returned dict has a shared sorted list of unique date labels (ISO strings)
    and one dataset per participant whose ``data`` is the cumulative total at each
    label date. Each scoring participant contributes a leading zero-baseline label
    at their observation-window start so their first scored event renders as a
    rising slope from zero. Participants with no events produce a flat zero series.

    Deactivated (self-serve-deleted) users show under their generated
    pseudonym with a "(deleted)" suffix (User.effective_display_name) --
    anonymize_account already replaced their real username/display_name
    with the pseudonym, and the suffix marks that clearly instead of
    leaving a departed member looking like an unexplained stranger next
    to real names on the same chart.

    ``top_n``, when given, keeps only the ``top_n`` datasets with the highest
    final cumulative value (the shared label axis is unaffected) -- used by the
    invite accept/decline preview (TASK-303) to fit a small card; the full
    challenge detail page always calls this with ``top_n=None``.

    Shape: {"labels": [...], "datasets": [{"label": str, "data": [int, ...]}, ...]}
    """
    participants = (
        ChallengeParticipant.objects.filter(
            challenge=challenge,
            invite_status=ChallengeParticipant.InviteStatus.ACCEPTED,
            is_bailed=False,
        )
        .select_related("user")
        .order_by("joined_at", "created_at")
    )

    events = list(
        PointEarnEvent.objects.filter(challenge=challenge)
        .exclude(points_earned=0)
        .values("user", "lift", "performed_at", "points_earned")
        .order_by("performed_at")
    )

    points_by_user: dict[int, list[tuple]] = {}
    for e in events:
        points_by_user.setdefault(e["user"], []).append(
            (e["performed_at"], e["lift"], e["points_earned"])
        )

    # Seed the shared date axis with a leading zero-baseline date at each
    # scoring participant's observation-window start, so their first scored
    # event renders as a rising slope from zero rather than an immediate flat
    # value (beginAtZero only anchors the y-scale, it does not add a point).
    # score_pooled_history filters events to performed_at >= window_start, so a
    # concrete window start is on or before the first event; we require the
    # baseline strictly before that first event, falling back to the day before
    # it when the window start is absent (FROM_JOIN with no join date) or lands
    # on/after the first event (e.g. a rejoin whose joined_at was reset).
    label_date_set = {e["performed_at"] for e in events}
    for participant in participants:
        user_events = points_by_user.get(participant.user_id)
        if not user_events:
            continue
        first_event_date = user_events[0][0]
        window_start = challenge.window_start_for(participant)
        window_start_date = window_start.date() if window_start is not None else None
        if window_start_date is None or window_start_date >= first_event_date:
            baseline = first_event_date - timedelta(days=1)
        else:
            baseline = window_start_date
        label_date_set.add(baseline)

    label_dates = sorted(label_date_set)
    labels = [d.isoformat() for d in label_dates]

    datasets = []
    for participant in participants:
        user = participant.user
        label = user.effective_display_name
        user_events = points_by_user.get(user.pk, [])
        data = []
        cumulative = 0
        # Highest points this lifter had earned in each lift so far. Events
        # arrive in performed_at order, so replaying them while walking the
        # label axis yields each date's total without re-scanning history.
        best_by_lift: dict[str, int] = {}
        idx = 0
        for label_date in label_dates:
            while idx < len(user_events) and user_events[idx][0] <= label_date:
                _, lift, points = user_events[idx]
                previous_best = best_by_lift.get(lift, 0)
                if points > previous_best:
                    cumulative += points - previous_best
                    best_by_lift[lift] = points
                idx += 1
            data.append(cumulative)
        datasets.append({"label": label, "data": data})

    if top_n is not None and len(datasets) > top_n:
        datasets = sorted(
            datasets, key=lambda ds: ds["data"][-1] if ds["data"] else 0, reverse=True
        )[:top_n]

    return {"labels": labels, "datasets": datasets}


def build_points_by_lift(challenge) -> dict:
    """Build Chart.js grouped-bar data of current points-per-lift per participant.

    The x-axis categories are the lifts the challenge's standards source covers
    (sorted for a stable order). Each accepted, non-bailed participant becomes one
    bar series whose ``data`` is that lifter's current points earned in each lift —
    the sum of their ``is_current_best=True`` PointEarnEvent rows for that lift, or
    0 for a lift they have not scored. Because the same current-best rows back the
    leaderboard total, a lifter's per-lift bars sum to their leaderboard total.

    Deactivated (self-serve-deleted) users show under their generated
    pseudonym with a "(deleted)" suffix (User.effective_display_name),
    matching build_points_over_time and the detail-page leaderboard.

    Shape: {"labels": [lift, ...], "datasets": [{"label": str, "data": [int]}, ...]}
    """
    lifts = sorted(covered_lift_names(challenge))

    participants = (
        ChallengeParticipant.objects.filter(
            challenge=challenge,
            invite_status=ChallengeParticipant.InviteStatus.ACCEPTED,
            is_bailed=False,
        )
        .select_related("user")
        .order_by("joined_at", "created_at")
    )

    from django.db.models import Sum

    rows = (
        PointEarnEvent.objects.filter(
            challenge=challenge,
            is_current_best=True,
        )
        .values("user", "lift")
        .annotate(total_points=Sum("points_earned"))
    )
    points_by_user_lift: dict[int, dict[str, int]] = {}
    for row in rows:
        points_by_user_lift.setdefault(row["user"], {})[row["lift"]] = row[
            "total_points"
        ]

    datasets = []
    for participant in participants:
        user = participant.user
        label = user.effective_display_name
        user_points = points_by_user_lift.get(user.pk, {})
        data = [user_points.get(lift, 0) for lift in lifts]
        datasets.append({"label": label, "data": data})

    return {"labels": lifts, "datasets": datasets}


def build_recent_scoring_activity(challenge, viewing_user, limit: int = 5) -> list:
    """Build a bounded, most-recent-first feed of the most significant scoring events.

    Returns up to ``limit`` display-ready rows, newest first (by performed date,
    then sync time). Bailed participants are excluded, mirroring the leaderboard
    and Points Over Time chart. Weights are converted to ``viewing_user``'s unit
    preference so the feed reads consistently for whoever is looking at it.

    Zero-point events are dropped entirely -- they are not meaningful activity.
    Superseded events (is_current_best=False) are dropped too, matching
    rank_participants/get_leader -- a later set on a lift that doesn't beat the
    existing PR still earns points_earned equal to whatever tier it clears, but
    it isn't a new personal best, so it shouldn't read as fresh activity here.
    Events are also collapsed per (lifter, lift, performed_at day): a lifting
    session often has several sets on the same lift the same day, each earning
    progressively more points as the lifter works up, so only the best-scoring
    set from each session is kept rather than showing every intermediate set.

    Deactivated (self-serve-deleted) lifters show under their generated
    pseudonym with a "(deleted)" suffix (User.effective_display_name), same
    as everywhere else this shows up. An empty list means the challenge has
    no scoring activity yet, which the template renders as an explicit empty
    state.

    Dict shape: {'name': str, 'lift': str, 'weight': Decimal, 'unit': str,
    'reps': int, 'points_earned': int, 'date': date}.
    """
    bailed_user_ids = ChallengeParticipant.objects.filter(
        challenge=challenge, is_bailed=True
    ).values_list("user_id", flat=True)

    events = (
        PointEarnEvent.objects.filter(challenge=challenge, is_current_best=True)
        .exclude(user__in=bailed_user_ids)
        .exclude(points_earned=0)
        .select_related("user")
        .order_by("-performed_at", "-synced_at")
    )

    best_by_session: dict[tuple, PointEarnEvent] = {}
    session_order: list[tuple] = []
    for event in events:
        session_key = (event.user_id, event.lift, event.performed_at)
        best = best_by_session.get(session_key)
        if best is None:
            best_by_session[session_key] = event
            session_order.append(session_key)
        elif event.points_earned > best.points_earned:
            best_by_session[session_key] = event

    unit = viewing_user.unit_preference
    activity = []
    for session_key in session_order[:limit]:
        event = best_by_session[session_key]
        user = event.user
        name = user.effective_display_name
        weight, _ = to_display_weight(event.weight, unit)
        activity.append(
            {
                "name": name,
                "lift": event.lift,
                "weight": weight,
                "unit": unit,
                "reps": event.reps,
                "points_earned": event.points_earned,
                "date": event.performed_at,
            }
        )
    return activity


def rank_participants(challenge, *, include_unscored=False) -> list[dict]:
    """Return a dense-ranked leaderboard for a challenge.

    Queries PointEarnEvent where is_current_best=True, groups by user, sums
    points_earned, and returns a list of dicts ordered by total_points descending.
    Tied users share the same rank (dense ranking).

    Bailed participants (voluntarily left or creator-removed) are excluded so a
    frozen ledger no longer occupies a ranked row, matching build_points_over_time.

    By default (``include_unscored=False``) only participants who have earned
    at least one point event appear -- this is the exact historical behavior
    that notification/standing code relies on: an unscored participant must
    stay absent from the ranked set, or every zero-scorer would appear to
    shift rank the moment anyone else scores, firing spurious "overtaken"
    notifications. Pass ``include_unscored=True`` for display purposes (a
    leaderboard UI) to additionally include every accepted, non-bailed
    ChallengeParticipant missing from the scored set, defaulted to
    total_points=0 and ranked via the same dense-rank pass across the
    combined set.

    Dict shape: {'user': <User>, 'total_points': <int>, 'rank': <int>}
    """
    from django.db.models import Sum

    bailed_user_ids = ChallengeParticipant.objects.filter(
        challenge=challenge, is_bailed=True
    ).values_list("user_id", flat=True)

    rows = (
        PointEarnEvent.objects.filter(
            challenge=challenge,
            is_current_best=True,
        )
        .exclude(user__in=bailed_user_ids)
        .values("user")
        .annotate(total_points=Sum("points_earned"))
        .order_by("-total_points")
    )

    from django.contrib.auth import get_user_model

    User = get_user_model()
    scored_totals = {row["user"]: row["total_points"] for row in rows}

    if include_unscored:
        unscored_user_ids = (
            ChallengeParticipant.objects.filter(
                challenge=challenge,
                invite_status=ChallengeParticipant.InviteStatus.ACCEPTED,
                is_bailed=False,
            )
            .exclude(user_id__in=scored_totals)
            .values_list("user_id", flat=True)
        )
        totals = {**scored_totals, **dict.fromkeys(unscored_user_ids, 0)}
    else:
        totals = scored_totals

    user_map = {u.pk: u for u in User.objects.filter(pk__in=totals)}

    ordered = sorted(totals.items(), key=lambda item: item[1], reverse=True)

    # Assign dense ranks in Python
    leaderboard = []
    current_rank = 0
    previous_points: int | None = None
    for user_id, total_points in ordered:
        if total_points != previous_points:
            current_rank += 1
            previous_points = total_points
        leaderboard.append(
            {
                "user": user_map[user_id],
                "total_points": total_points,
                "rank": current_rank,
            }
        )

    return leaderboard


def get_leader(challenge) -> dict | None:
    """Return only the current leader of a challenge, or None if unscored.

    A single top-1 aggregate plus one user fetch — cheaper than building the
    whole leaderboard when a caller (the find-challenges list) needs nothing
    but the leader's name and points for each row.

    Bailed participants are excluded so a left user is never advertised as the
    headline leader on the find-challenges page.

    Dict shape: {'user': <User>, 'total_points': <int>}.
    """
    from django.db.models import Sum

    bailed_user_ids = ChallengeParticipant.objects.filter(
        challenge=challenge, is_bailed=True
    ).values_list("user_id", flat=True)

    top = (
        PointEarnEvent.objects.filter(
            challenge=challenge,
            is_current_best=True,
        )
        .exclude(user__in=bailed_user_ids)
        .values("user")
        .annotate(total_points=Sum("points_earned"))
        .order_by("-total_points")
        .first()
    )
    if top is None:
        return None

    from django.contrib.auth import get_user_model

    user = get_user_model().objects.get(pk=top["user"])
    return {"user": user, "total_points": top["total_points"]}


def get_user_standing(challenge, user) -> dict:
    """Return a user's total points and dense rank in a challenge.

    Derives both from a single leaderboard computation rather than a separate
    per-user aggregate. ``rank`` is None when the user has earned no points and
    therefore does not appear on the leaderboard; ``total_points`` is 0 then.

    Dict shape: {'total_points': <int>, 'rank': <int | None>}.
    """
    for entry in rank_participants(challenge):
        if entry["user"].pk == user.pk:
            return {"total_points": entry["total_points"], "rank": entry["rank"]}
    return {"total_points": 0, "rank": None}


def compute_ranking_deltas(
    leaderboard_before: list[dict], leaderboard_after: list[dict]
) -> list[dict]:
    """Diff two ordered rank_participants() snapshots into overtake events.

    Pure function: no DB access, no side effects. For every participant whose
    rank is numerically higher (worse) in ``leaderboard_after`` than in
    ``leaderboard_before``, returns one delta recording the drop and whichever
    participant now occupies their old rank -- the same "who got overtaken"
    computation ``create_overtaken_notifications`` used to do inline, split out
    so it can be unit-tested with plain in-memory data.

    Dict shape: {'user': <User>, 'from_rank': <int>, 'to_rank': <int>,
    'overtaken_by': <User>}.
    """
    # Keyed on the user object itself, not user.pk: Django model instances
    # compare and hash by pk (so two separately-fetched rows for the same
    # user still collide correctly here), and keeping it identity/equality
    # based -- rather than reaching for .pk -- is what lets this stay a pure
    # function callable with plain in-memory test doubles, no ORM required.
    before_rank_by_user = {entry["user"]: entry["rank"] for entry in leaderboard_before}
    after_by_rank = {entry["rank"]: entry["user"] for entry in leaderboard_after}

    deltas = []
    for entry in leaderboard_after:
        user = entry["user"]
        new_rank = entry["rank"]
        old_rank = before_rank_by_user.get(user)

        if old_rank is None or new_rank <= old_rank:
            continue

        overtaker = after_by_rank.get(old_rank)
        if overtaker is None or overtaker == user:
            continue

        deltas.append(
            {
                "user": user,
                "from_rank": old_rank,
                "to_rank": new_rank,
                "overtaken_by": overtaker,
            }
        )

    return deltas


def notify_ranking_changes(challenge, deltas: list[dict]) -> None:
    """Create overtaken Notification rows for the given ranking deltas.

    Bailed participants are excluded — their ledger is frozen and notifying
    them while bailed is not meaningful.
    """
    bailed_user_ids = set(
        ChallengeParticipant.objects.filter(
            challenge=challenge, is_bailed=True
        ).values_list("user_id", flat=True)
    )

    for delta in deltas:
        user = delta["user"]
        if user.pk in bailed_user_ids:
            continue

        overtaker = delta["overtaken_by"]
        old_rank = delta["from_rank"]
        new_rank = delta["to_rank"]

        Notification.objects.create(
            user=user,
            event_type=Notification.EventType.OVERTAKEN,
            challenge=challenge,
            metadata={
                "overtaken_by_id": str(overtaker.pk),
                "overtaken_by_name": overtaker.display_name or overtaker.username,
                "from_rank": old_rank,
                "to_rank": new_rank,
            },
        )
        logger.info(
            "Overtaken notification: user %s dropped %s->%s in challenge %s",
            user.pk,
            old_rank,
            new_rank,
            challenge.pk,
        )


def build_career_stats(user) -> dict:
    """Cross-challenge career stats for the dashboard hero card.

    Aggregates the user's whole scoring history across every challenge they
    have accepted into (drafts excluded — a draft has no scoring yet):

    - ``challenges_played``: accepted participations in non-draft challenges.
      Bailed participations still count — the user did play.
    - ``wins``: completed challenges where the user holds dense rank #1
      (bailed participants are already excluded by rank_participants).
    - ``total_points``: sum of the user's current-best point events everywhere.
    - ``points_per_week``: total_points spread over the span from their first
      to their most recent scoring event, with a one-week floor so a brand-new
      scorer is not shown an absurd rate. None until they have scored.
    - ``avg_points``: total_points / challenges_played. None until they play.
    - ``lifts``: per lift, the first set that ever earned points vs the most
      recent one, weights converted to the user's display unit. Ordered by
      lift name.

    ``has_history`` is True once the user has played at least one challenge;
    the hero card shows a stats preview plus a start-a-challenge CTA until then.
    """
    from django.db.models import Sum

    participations = (
        ChallengeParticipant.objects.filter(
            user=user,
            invite_status=ChallengeParticipant.InviteStatus.ACCEPTED,
        )
        .exclude(challenge__status=Challenge.Status.DRAFT)
        .select_related("challenge")
    )
    challenges_played = len(participations)

    wins = 0
    for participation in participations:
        challenge = participation.challenge
        if challenge.status != Challenge.Status.COMPLETED:
            continue
        if get_user_standing(challenge, user)["rank"] == 1:
            wins += 1

    total_points = (
        PointEarnEvent.objects.filter(user=user, is_current_best=True).aggregate(
            total=Sum("points_earned")
        )["total"]
        or 0
    )

    scoring_events = list(
        PointEarnEvent.objects.filter(user=user, points_earned__gt=0).order_by(
            "performed_at", "synced_at"
        )
    )

    points_per_week = None
    if scoring_events:
        span_days = (
            scoring_events[-1].performed_at - scoring_events[0].performed_at
        ).days
        weeks = Decimal(max(span_days, 7)) / Decimal(7)
        points_per_week = (Decimal(total_points) / weeks).quantize(Decimal("0.1"))

    avg_points = None
    if challenges_played:
        avg_points = (Decimal(total_points) / Decimal(challenges_played)).quantize(
            Decimal("0.1")
        )

    unit = user.unit_preference
    first_by_lift: dict[str, PointEarnEvent] = {}
    latest_by_lift: dict[str, PointEarnEvent] = {}
    for event in scoring_events:
        first_by_lift.setdefault(event.lift, event)
        latest_by_lift[event.lift] = event

    lifts = []
    for lift in sorted(first_by_lift):
        first_event = first_by_lift[lift]
        latest_event = latest_by_lift[lift]
        first_weight, _ = to_display_weight(first_event.weight, unit)
        latest_weight, _ = to_display_weight(latest_event.weight, unit)
        lifts.append(
            {
                "lift": lift,
                "first_weight": first_weight,
                "first_date": first_event.performed_at,
                "latest_weight": latest_weight,
                "latest_date": latest_event.performed_at,
                "unit": unit,
            }
        )

    return {
        "challenges_played": challenges_played,
        "wins": wins,
        "total_points": total_points,
        "points_per_week": points_per_week,
        "avg_points": avg_points,
        "lifts": lifts,
        "has_history": challenges_played > 0,
    }
