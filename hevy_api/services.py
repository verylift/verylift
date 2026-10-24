"""Service functions for the Hevy API live-sync integration (TASK-312, #8).

Mirrors liftosaur.services' shape (validate/sync/watermark/cooldown), but the
per-workout parsing is far simpler than Liftosaur's Liftohistory-text parser:
Hevy's API already returns structured JSON (verified against its published
OpenAPI spec), so there is no exercise-line grammar to parse -- only field
lookups and the same warmup-set exclusion the existing Hevy CSV importer
(workout_imports.importers.hevy) already applies.

Exercise names are canonicalized via the shared core.lift_resolution chain
against core.models.LiftAlias with source="hevy", never source="liftosaur"
-- Hevy's own exercise titles (e.g. "Bench Press (Barbell)") don't match
Liftosaur's alias vocabulary, and the Hevy-sourced aliases already exist precisely
to bridge Hevy's naming to the canonical standard-lift names. This is the
same alias data the CSV importer resolves against, since both are the same
underlying Hevy exercise catalogue.

Known limitation, documented rather than silently worked around: Hevy's
workout-events endpoint reports both "updated" and "deleted" events, but
LiftHistory has no per-row link back to a source workout id (its identity is
user+lift+performed_at+reps+weight_kg, same as Liftosaur's), so a workout
deleted in Hevy after being synced is not retracted from the pool. This
mirrors an already-accepted class of eventual-consistency gap in the
Liftosaur integration (edit-after-pull) rather than introducing a new kind of
inconsistency.

TASK-325 -- /v1/workouts/events ordering, and why the resume logic here does
not depend on it: Hevy's own OpenAPI spec (served at
https://api.hevyapp.com/docs/, title "Hevy API Docs", server
api.hevyapp.com; cross-checked via the mirrored spec at
https://github.com/chrisdoc/hevy-mcp/blob/main/openapi-spec.json since the
Swagger UI page renders client-side and has no stable raw-JSON URL) states
the endpoint returns events newest-first: "Events are ordered from newest to
oldest." This matters because a naive resume that advances the delta
watermark to the newest pooled row after a truncated walk would jump straight
past everything older that the walk never reached -- permanently, since
nothing later re-requests that window. See pull_events_into_pool and
sync_user_lifts: instead of trusting ordering, a truncated walk is recorded
as such (HevySyncLog.walk_complete=False) and the *next* sync resumes from
the exact `since` this run used (HevySyncLog.since_used), not from a
watermark computed off partial results, so no event can be skipped regardless
of what order the API happens to return them in.
"""

import json
import logging
import threading
import time
import urllib.error
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db import OperationalError, transaction
from django.db.models import Max
from django.utils import timezone

from accounts.timezones import local_day, user_zoneinfo
from core.lift_resolution import LiftNameResolver, build_lift_alias_maps
from core.models import Lift, LiftAliasSource, LiftHistory, LiftSource
from core.sync_status import latest_sync_failure as shared_latest_sync_failure
from hevy_api.client import HevyAPIError, HevyClient
from hevy_api.models import HevySyncLog

logger = logging.getLogger(__name__)

# How far back the one-time onboarding backfill reaches when a user has no
# stored history yet. Subsequent syncs use the delta watermark instead.
# Matches liftosaur.services.HISTORY_BACKFILL_DAYS.
HISTORY_BACKFILL_DAYS = 365

# Hevy excludes warmup sets from working-set totals; everything else (normal,
# dropset, failure, and any future set type Hevy adds) counts. Mirrors
# workout_imports.importers.hevy._EXCLUDED_SET_TYPES.
_EXCLUDED_SET_TYPES = frozenset({"warmup"})

POOL_WRITE_BATCH_SIZE = 500
POOL_WRITE_RETRY_DELAYS = (0.1, 0.3, 0.9)

# Safety valve for the events walk: Hevy caps pageSize at 10, so a lifter with
# years of history could in principle page for a very long time on their first
# sync. Bounding it means one sync run can't hang indefinitely.
#
# A truncated run does NOT move the delta watermark to what it managed to
# pool (TASK-325): /v1/workouts/events is confirmed newest-first (see the
# module docstring), so the newest-pooled-row watermark a truncated walk
# would produce is close to "now" while everything older than the walk's
# reach is still unpooled -- moving the watermark there would skip that older
# history permanently, since nothing ever re-requests it. Instead
# pull_events_into_pool reports whether it walked every page, and
# sync_user_lifts records that on HevySyncLog (walk_complete/since_used): a
# truncated run's own `since` is reused verbatim next time, so the walk keeps
# making the same request until it completes rather than advancing past
# unpooled history. Pages are still committed as they're written within one
# call (idempotent upserts), so a truncated run keeps whatever it already
# wrote -- only the *watermark used by the next call* is gated on completion.
#
# Two different callers need two different bounds, because only one of them
# runs inside a request/response cycle bounded by gunicorn.conf.py's
# `timeout = 60`:
#
# - The one-time onboarding backfill (trigger_hevy_lift_history_backfill)
#   runs off-thread (see below), so it isn't racing the worker timeout at
#   all. It keeps the original generous cap -- just a ceiling against
#   literally-unbounded pagination, e.g. a pathological account or an API
#   bug that never advances page_count.
# - Every other caller (challenges.services.sync_and_score,
#   accounts.views.hevy_sync_now_view) runs synchronously inside a request,
#   and must leave that request nowhere near the 60s worker timeout even in
#   the worst case.
#
# Worst case per page is HEVY_API_TIMEOUT (10s: the HTTP round-trip can hang
# right up to the client's own timeout without erroring) plus the full
# POOL_WRITE_RETRY_DELAYS sleep budget (0.1 + 0.3 + 0.9 = 1.3s, if the write
# loses the DB lock race on every attempt) = 11.3s/page. Capping the
# in-request walk at 3 pages bounds that portion of the request to
# 3 * 11.3 = 33.9s -- under 60% of the 60s worker timeout, leaving the
# remainder for the Liftosaur pull sync_and_score also runs, DB writes,
# scoring, and template rendering. In practice a routine delta sync (the
# only kind that runs synchronously once the one-time backfill is off the
# request path) needs at most a page or two; this cap only bites for a
# lifter who has been away long enough to accumulate a large delta, and a
# truncated run there still makes real forward progress each time it's
# triggered again.
MAX_EVENT_PAGES_PER_BACKGROUND_RUN = 500
MAX_EVENT_PAGES_PER_INLINE_RUN = 3


# validate_hevy_key_status outcomes. VALID/INVALID are a confirmed answer from
# Hevy; UNKNOWN means the probe couldn't complete (network hiccup, a Hevy
# 5xx/429, an unexpected local failure) and says nothing about the key itself.
# Settings' save path (accounts.views.settings_view) treats UNKNOWN
# differently from a bool caller like onboarding: rejecting a key outright
# because Hevy happened to be unreachable for one request would be its own
# regression, so an UNKNOWN key gets saved with a caveat instead -- if it
# turns out to actually be bad, the next sync attempt fails and that failure
# is surfaced via HevySyncLog (see latest_sync_failure).
HEVY_KEY_VALID = "valid"
HEVY_KEY_INVALID = "invalid"
HEVY_KEY_UNKNOWN = "unknown"

# HTTP statuses Hevy uses to mean "this key is not accepted", as opposed to a
# transient/server-side problem (5xx, 429) that says nothing about the key.
_HEVY_AUTH_FAILURE_STATUSES = frozenset({401, 403})


def validate_hevy_key_status(api_key: str) -> str:
    """Validate a Hevy API key, distinguishing a confirmed rejection from an
    inconclusive probe.

    A minimal page_size=1 request is enough to confirm the key is accepted;
    nothing about the returned workout is used. Returns one of
    HEVY_KEY_VALID / HEVY_KEY_INVALID / HEVY_KEY_UNKNOWN -- see those
    constants' comment for what each means and why the distinction matters.
    """
    client = HevyClient(api_key)
    try:
        client.get_workouts(page=1, page_size=1)
        return HEVY_KEY_VALID
    except HevyAPIError as exc:
        if exc.status_code in _HEVY_AUTH_FAILURE_STATUSES:
            logger.warning("Hevy key validation rejected by API: %s", exc)
            return HEVY_KEY_INVALID
        logger.warning(
            "Hevy key validation got a non-auth API error, treating as "
            "inconclusive rather than rejecting the key: %s",
            exc,
        )
        return HEVY_KEY_UNKNOWN
    except (urllib.error.URLError, OSError) as exc:
        logger.warning("Hevy key validation failed due to network error: %s", exc)
        return HEVY_KEY_UNKNOWN
    except Exception:
        logger.exception("Hevy key validation failed unexpectedly")
        return HEVY_KEY_UNKNOWN


def validate_hevy_key(api_key: str) -> bool:
    """Validate a Hevy API key by calling the workouts endpoint.

    Strict bool wrapper around validate_hevy_key_status for callers that only
    want a definite yes/no (onboarding's connect-tracker step, the Settings
    "Test Connection" AJAX probe) -- both treat anything short of a confirmed
    HEVY_KEY_VALID as a failure, unlike the Settings save path.
    """
    return validate_hevy_key_status(api_key) == HEVY_KEY_VALID


@dataclass(frozen=True)
class ParsedSet:
    """A single completed set parsed from a Hevy API workout payload."""

    lift: str
    performed_at: date
    reps: int
    weight_kg: Decimal


def _build_resolver() -> LiftNameResolver:
    """Build the shared tracker-agnostic resolver for Hevy's alias data.

    Mirrors workout_imports.importers.hevy.HevyImporter's use of the same
    core.lift_resolution chain: one query for the whole sync instead of one
    LiftAlias SELECT per set.
    """
    return LiftNameResolver(
        build_lift_alias_maps(
            LiftAliasSource.HEVY, Lift.objects.values_list("name", flat=True)
        ),
        source_label="Hevy live sync",
        logger=logger,
    )


def _parse_start_date(raw: str, tz: ZoneInfo) -> date | None:
    """Parse a Hevy ``start_time`` ISO timestamp into the lifter's local date.

    Hevy reports ``start_time`` as a UTC instant ("2026-08-25T02:00:00Z"), so
    the conversion into ``tz`` is what keeps a late-evening session filed under
    the day the lifter actually trained -- see accounts.timezones.local_day.

    Contrast the Hevy CSV importer (workout_imports.importers.hevy), which
    needs no conversion: that export's start_time cells are already naive
    local wall-clock. Liftosaur's export carries a UTC offset per timestamp,
    so its parse keeps the local date too.
    """
    if not raw:
        return None
    try:
        return local_day(datetime.fromisoformat(raw.replace("Z", "+00:00")), tz)
    except ValueError:
        logger.warning("Skipping Hevy workout: unparseable start_time %r", raw)
        return None


def _parse_workout(
    workout: dict, resolver: LiftNameResolver, tz: ZoneInfo
) -> list[ParsedSet]:
    """Expand one Hevy workout payload into its completed working sets.

    Skips warmup sets and sets with no reps recorded (cardio-style sets that
    track distance_meters/duration_seconds instead) -- not an error, just not
    a set this sync scores. One malformed exercise/set is skipped, never
    aborts the whole workout.
    """
    performed_at = _parse_start_date(workout.get("start_time", ""), tz)
    if performed_at is None:
        return []

    parsed: list[ParsedSet] = []
    for exercise in workout.get("exercises") or []:
        title = (exercise.get("title") or "").strip()
        if not title:
            continue
        lift = resolver.resolve(title)
        for raw_set in exercise.get("sets") or []:
            if (raw_set.get("type") or "").strip().lower() in _EXCLUDED_SET_TYPES:
                continue
            reps = raw_set.get("reps")
            weight_kg = raw_set.get("weight_kg")
            if reps is None or weight_kg is None:
                continue
            try:
                weight = Decimal(str(weight_kg)).quantize(Decimal("0.01"))
            except (ArithmeticError, ValueError):
                continue
            parsed.append(
                ParsedSet(
                    lift=lift,
                    performed_at=performed_at,
                    reps=int(reps),
                    weight_kg=weight,
                )
            )
    return parsed


def _history_rows(
    parsed_sets: list[ParsedSet], *, user, synced_at
) -> list[LiftHistory]:
    """Build the unsaved LiftHistory rows for a batch of parsed sets.

    Keyed on (user, lift, performed_at, reps, weight_kg), same identity
    liftosaur.services uses -- Hevy exposes no stable per-set id either.
    Collapsing intra-batch duplicates onto that key before they reach the
    database avoids the same "ON CONFLICT DO UPDATE...cannot affect row a
    second time" failure liftosaur.services._history_rows_for_page guards
    against.
    """
    rows: dict[tuple, LiftHistory] = {}
    for parsed_set in parsed_sets:
        key = (
            parsed_set.lift,
            parsed_set.performed_at,
            parsed_set.reps,
            parsed_set.weight_kg,
        )
        rows[key] = LiftHistory(
            user=user,
            lift=parsed_set.lift,
            performed_at=parsed_set.performed_at,
            reps=parsed_set.reps,
            weight_kg=parsed_set.weight_kg,
            synced_at=synced_at,
            source=LiftSource.HEVY,
        )
    return list(rows.values())


def _write_batch(rows: list[LiftHistory]) -> None:
    if not rows:
        return
    with transaction.atomic():
        LiftHistory.objects.bulk_create(
            rows,
            batch_size=POOL_WRITE_BATCH_SIZE,
            update_conflicts=True,
            unique_fields=["user", "lift", "performed_at", "reps", "weight_kg"],
            update_fields=["synced_at", "source"],
        )


def _write_batch_with_retry(rows: list[LiftHistory], *, user) -> None:
    """Write one page's worth of pooled rows, retrying transient DB contention.

    Mirrors liftosaur.services._write_history_batch_with_retry.
    """
    for attempt, delay in enumerate(POOL_WRITE_RETRY_DELAYS, start=1):
        try:
            _write_batch(rows)
            return
        except OperationalError as exc:
            logger.warning(
                "Hevy pool write hit DB contention for user %s "
                "(attempt %s/%s), retrying in %ss: %s",
                user.id,
                attempt,
                len(POOL_WRITE_RETRY_DELAYS) + 1,
                delay,
                exc,
            )
            time.sleep(delay)
    _write_batch(rows)


def history_watermark(user):
    """Return the latest performed_at among the user's pooled Hevy-sourced sets,
    or None.

    Scoped to source=HEVY (unlike liftosaur.services.history_watermark, which
    aggregates across the whole shared LiftHistory pool). LiftHistory is a single
    pool across every source, but TASK-319 showed that pooling the watermark
    itself is the wrong move for a *live-sync* connector: a user who already has
    Liftosaur, CSV, or manual history gets ``since=<today>`` the moment they
    connect Hevy for the first time, because the pool's newest row belongs to
    another source entirely -- zero backfill, no error shown. Sync order in
    challenges.services.sync_and_score (Liftosaur before Hevy) makes it worse:
    Liftosaur's own pull can move the shared watermark before this function ever
    reads it, so even a freshly-onboarded dual-connected user gets poisoned.
    Scoping to HEVY sidesteps both: this connector's watermark can only move
    by this connector's own writes (deliberately excluding HEVY_CSV -- a CSV
    upload's dates aren't this live connector's own writes either), so sync
    order and other-source history are both irrelevant. It's also strictly
    more accurate on an ongoing basis -- the shared watermark can park on
    another source's newer date and skip legitimately-new Hevy workouts that
    are still older than that date (see wger.services.history_watermark, which
    made the same scoping call for the same reason).

    One caveat worth naming: Hevy's ``/v1/workouts/events?since=`` filters on
    *event* time (when the workout was created/updated in Hevy), not
    performed_at (the workout date stored here). A workout backdated in Hevy to
    a date already at or behind this watermark, then edited later, is still
    picked up correctly (its event time is the edit), so this mismatch doesn't
    reintroduce a backfill gap -- it only means "since" is a performed_at value
    doing double duty as an event-time filter, same tolerance the unscoped
    version already accepted.
    """
    return LiftHistory.objects.filter(user=user, source=LiftSource.HEVY).aggregate(
        latest=Max("performed_at")
    )["latest"]


def _backfill_since() -> str:
    return (
        timezone.now() - timedelta(days=HISTORY_BACKFILL_DAYS)
    ).date().isoformat() + "T00:00:00Z"


def recent_pull_exists(user) -> bool:
    """True if a successful Hevy pull for the user completed within cooldown."""
    cutoff = timezone.now() - timedelta(minutes=settings.HEVY_SYNC_COOLDOWN_MINUTES)
    return HevySyncLog.objects.filter(
        user=user, success=True, started_at__gte=cutoff
    ).exists()


def last_synced_at(user):
    """Return the started_at of the user's most recent successful Hevy pull."""
    return (
        HevySyncLog.objects.filter(user=user, success=True)
        .order_by("-started_at")
        .values_list("started_at", flat=True)
        .first()
    )


def latest_sync_failure(user) -> HevySyncLog | None:
    """Return the user's most recent Hevy sync log, if that attempt failed.

    Thin wrapper over core.sync_status.latest_sync_failure, which carries the
    semantics; see there for why the log and not sync_user_lifts' return value
    is the failure signal.
    """
    return shared_latest_sync_failure(HevySyncLog, user)


def pull_events_into_pool(
    client, user, *, since: str, synced_at, max_pages: int
) -> tuple[int, bool]:
    """Walk paginated Hevy workout events, upserting completed sets into LiftHistory.

    ``since`` drives both the initial backfill (a date far in the past) and
    every subsequent delta sync (this connector's own HEVY-scoped
    watermark, see ``history_watermark``) -- Hevy's events
    endpoint returns full workout payloads for "updated" events regardless, so
    there is no separate backfill endpoint/code path to maintain. "deleted"
    events are not applied (see module docstring).

    ``max_pages`` bounds how far this single call will walk -- see
    MAX_EVENT_PAGES_PER_BACKGROUND_RUN / MAX_EVENT_PAGES_PER_INLINE_RUN for
    which one callers should pass and why they differ.

    Returns ``(sets_pooled, walk_complete)``: the number of completed sets
    persisted to the shared pool, and whether every available page was
    walked (False if the walk stopped at ``max_pages`` with pages still
    remaining). Callers must not treat a truncated walk's newest-pooled-row
    watermark as safe to resume from -- see this module's docstring for why.
    """
    pooled = 0
    resolver = _build_resolver()
    tz = user_zoneinfo(user)
    page = 1
    page_count = 1

    while page <= page_count and page <= max_pages:
        data = client.get_workout_events(since=since, page=page)
        page_count = data.get("page_count", 1)

        page_sets: list[ParsedSet] = []
        for event in data.get("events") or []:
            if event.get("type") != "updated":
                continue
            workout = event.get("workout") or {}
            page_sets.extend(_parse_workout(workout, resolver, tz))

        pooled += len(page_sets)
        _write_batch_with_retry(
            _history_rows(page_sets, user=user, synced_at=synced_at), user=user
        )
        page += 1

    walk_complete = page_count <= max_pages
    if not walk_complete:
        logger.warning(
            "Hevy event pagination truncated for user %s: %s pages available, "
            "stopped after %s. The next sync will resume from this same "
            "`since` (%s) rather than a watermark derived from this partial "
            "walk, since /v1/workouts/events is newest-first and a moved "
            "watermark could skip older unpooled events.",
            user.id,
            page_count,
            max_pages,
            since,
        )

    return pooled, walk_complete


def _mark_sync_log_failed(sync_log, detail: str, *, user) -> None:
    sync_log.success = False
    sync_log.completed_at = datetime.now(tz=UTC)
    sync_log.error_detail = detail
    try:
        sync_log.save(update_fields=["success", "completed_at", "error_detail"])
    except OperationalError:
        logger.exception(
            "Could not record the failed Hevy sync log for user %s: the "
            "database is still refusing writes",
            user.id,
        )


def sync_user_lifts(
    user, force: bool = False, max_pages: int = MAX_EVENT_PAGES_PER_INLINE_RUN
) -> int:
    """Pure per-user pull that keeps the shared LiftHistory pool fresh from Hevy.

    No-op (returns 0) when the user has no Hevy API key. Delta-aware: once this
    connector has ever completed a successful pull for the user, subsequent
    runs start from ``history_watermark`` (this module's own, HEVY-scoped
    watermark -- see its docstring for why the shared pool's watermark is the
    wrong signal here). A user with no prior successful ``HevySyncLog`` always
    gets the full ``HISTORY_BACKFILL_DAYS`` window instead, regardless of what
    is already pooled from another source: that is the one-time onboarding
    backfill, and gating it on the sync log (rather than on
    ``history_watermark`` returning ``None``) keeps it correct even if a first
    sync completes but pools zero sets. Unless ``force`` is True, the run is
    skipped when a successful Hevy pull completed within
    HEVY_SYNC_COOLDOWN_MINUTES.

    ``max_pages`` defaults to the request-cycle-safe cap. Callers that run off
    the request/response cycle (the onboarding backfill thread) pass
    MAX_EVENT_PAGES_PER_BACKGROUND_RUN instead -- see that constant's comment.
    When an inline-capped walk (``max_pages < MAX_EVENT_PAGES_PER_BACKGROUND_RUN``)
    truncates, this triggers a background catch-up pass at the higher cap
    (see trigger_hevy_event_catchup) so a large delta backlog still drains
    even though every inline call keeps resuming from the same `since` --
    without it, a backlog bigger than one inline walk can hold would never
    shrink, since each inline call would just re-fetch the same unfinished
    window forever (TASK-325).

    Returns the number of sets pooled.
    """
    if not user.hevy_api_key:
        logger.info("Skipping Hevy lift sync for user %s: no Hevy API key", user.id)
        return 0

    if not force and recent_pull_exists(user):
        logger.debug("Skipping Hevy sync for user %s: within cooldown window", user.id)
        return 0

    sync_log = HevySyncLog.objects.create(
        user=user, started_at=datetime.now(tz=UTC), success=None
    )

    try:
        client = HevyClient(user.hevy_api_key)

        # TASK-325: resume from the exact `since` a truncated prior walk used,
        # not from a watermark derived from its (necessarily partial, and --
        # per /v1/workouts/events being newest-first -- newest-skewed) pooled
        # rows. Only a *completed* prior walk's watermark is safe to resume
        # from. See the module docstring and MAX_EVENT_PAGES_PER_INLINE_RUN's
        # comment for why.
        last_successful_log = (
            HevySyncLog.objects.filter(user=user, success=True)
            .order_by("-started_at")
            .first()
        )
        if last_successful_log is None:
            since = _backfill_since()
        elif not last_successful_log.walk_complete:
            since = last_successful_log.since_used
        else:
            watermark = history_watermark(user)
            since = (
                f"{watermark.isoformat()}T00:00:00Z"
                if watermark is not None
                else _backfill_since()
            )

        synced_at = datetime.now(tz=UTC)
        pooled, walk_complete = pull_events_into_pool(
            client, user, since=since, synced_at=synced_at, max_pages=max_pages
        )
    except HevyAPIError as exc:
        _mark_sync_log_failed(sync_log, str(exc), user=user)
        logger.exception("Hevy lift sync failed for user %s", user.id)
        return 0
    except (urllib.error.URLError, OSError) as exc:
        # A slow/unreachable Hevy API. OSError also catches the bare
        # TimeoutError a read-phase timeout surfaces as (TimeoutError is an
        # OSError subclass) -- core.http.send_request's docstring only
        # promises urllib.error.URLError for network failures, which a
        # read-phase timeout isn't one of. Degrade exactly like the API-error
        # branch: a 500 reaching the user for a transient network hiccup is
        # strictly worse than showing whatever is already pooled.
        _mark_sync_log_failed(sync_log, f"Network error: {exc}", user=user)
        logger.exception("Hevy lift sync network error for user %s", user.id)
        return 0
    except OperationalError as exc:
        _mark_sync_log_failed(sync_log, f"DB contention: {exc}", user=user)
        logger.exception("Hevy lift sync aborted by DB contention for user %s", user.id)
        return 0

    sync_log.success = True
    sync_log.completed_at = datetime.now(tz=UTC)
    sync_log.result_summary = json.dumps({"sets_pooled": pooled})
    sync_log.walk_complete = walk_complete
    sync_log.since_used = since
    sync_log.save(
        update_fields=[
            "success",
            "completed_at",
            "result_summary",
            "walk_complete",
            "since_used",
        ]
    )
    logger.info("Hevy lift sync complete for user %s: %s sets pooled", user.id, pooled)

    if not walk_complete and max_pages < MAX_EVENT_PAGES_PER_BACKGROUND_RUN:
        logger.info(
            "Hevy events walk truncated inline for user %s; triggering a "
            "background catch-up pass at the higher page cap",
            user.id,
        )
        trigger_hevy_event_catchup(user)

    return pooled


def trigger_hevy_lift_history_backfill(user) -> None:
    """Run sync_user_lifts in a daemon thread so it never blocks the caller.

    Mirrors liftosaur.services.trigger_lift_history_backfill: the one-time
    onboarding backfill (up to HISTORY_BACKFILL_DAYS of history, potentially
    many pages at Hevy's 10-per-page cap) is by far the most expensive Hevy
    pull a user ever triggers, so it must not run inside the request that
    saves their API key. Used when a user connects Hevy from Settings.
    """
    thread = threading.Thread(target=_run_backfill_in_thread, args=(user,), daemon=True)
    thread.start()


def trigger_hevy_event_catchup(user) -> None:
    """Continue a truncated inline events walk in the background (TASK-325).

    Unlike trigger_hevy_lift_history_backfill, this bypasses the sync
    cooldown (``force=True``): the inline call that just truncated already
    recorded a successful-but-incomplete HevySyncLog, so recent_pull_exists
    would otherwise skip this catch-up entirely until the cooldown window
    passes -- defeating the point of triggering it immediately.
    """
    thread = threading.Thread(
        target=_run_backfill_in_thread,
        args=(user,),
        kwargs={"force": True},
        daemon=True,
    )
    thread.start()


def _run_backfill_in_thread(user, *, force: bool = False) -> None:
    """Thread entry point: sync lifts, then release this thread's DB connection."""
    from django.db import connection

    try:
        sync_user_lifts(user, force=force, max_pages=MAX_EVENT_PAGES_PER_BACKGROUND_RUN)
    except Exception:
        logger.exception("Hevy backfill thread failed for user %s", user.id)
    finally:
        connection.close()
