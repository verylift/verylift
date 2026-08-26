"""Service functions for the Hevy API live-sync integration (TASK-312, #8).

Mirrors liftosaur.services' shape (validate/sync/watermark/cooldown), but the
per-workout parsing is far simpler than Liftosaur's Liftohistory-text parser:
Hevy's API already returns structured JSON (verified against its published
OpenAPI spec), so there is no exercise-line grammar to parse -- only field
lookups and the same warmup-set exclusion the existing Hevy CSV importer
(workout_imports.importers.hevy) already applies.

Exercise names are canonicalized via workout_imports.models.HevyLiftAlias, not
liftosaur.models.LiftAlias -- Hevy's own exercise titles (e.g. "Bench Press
(Barbell)") don't match Liftosaur's alias table, and HevyLiftAlias already
exists precisely to bridge Hevy's naming to the canonical standard-lift names.
This is the same alias table the CSV importer uses, since both are the same
underlying Hevy exercise catalogue.

Known limitation, documented rather than silently worked around: Hevy's
workout-events endpoint reports both "updated" and "deleted" events, but
LiftHistory has no per-row link back to a source workout id (its identity is
user+lift+performed_at+reps+weight_kg, same as Liftosaur's), so a workout
deleted in Hevy after being synced is not retracted from the pool. This
mirrors an already-accepted class of eventual-consistency gap in the
Liftosaur integration (edit-after-pull) rather than introducing a new kind of
inconsistency.
"""

import json
import logging
import threading
import time
import urllib.error
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from django.conf import settings
from django.db import OperationalError, transaction
from django.db.models import Max
from django.utils import timezone

from hevy_api.client import HevyAPIError, HevyClient
from hevy_api.models import HevySyncLog
from liftosaur.models import LiftHistory, LiftSource
from workout_imports.models import HevyLiftAlias

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
# sync. Bounding it means one sync run can't hang indefinitely. A truncated
# run is *not* stuck at a fixed watermark: pages are committed as they are
# written (see pull_events_into_pool), so the watermark moves forward with
# every completed page and the next sync resumes past what was already
# pooled, rather than re-requesting the same window (whether resuming from a
# moved watermark is *safe* -- i.e. can't skip an event -- depends on
# /v1/workouts/events ordering guarantees, tracked separately as TASK-325).
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


def validate_hevy_key(api_key: str) -> bool:
    """Validate a Hevy API key by calling the workouts endpoint.

    A minimal page_size=1 request is enough to confirm the key is accepted;
    nothing about the returned workout is used.
    """
    client = HevyClient(api_key)
    try:
        client.get_workouts(page=1, page_size=1)
        return True
    except HevyAPIError as exc:
        logger.warning("Hevy key validation rejected by API: %s", exc)
        return False
    except (urllib.error.URLError, OSError) as exc:
        logger.warning("Hevy key validation failed due to network error: %s", exc)
        return False
    except Exception:
        logger.exception("Hevy key validation failed unexpectedly")
        return False


@dataclass(frozen=True)
class ParsedSet:
    """A single completed set parsed from a Hevy API workout payload."""

    lift: str
    performed_at: date
    reps: int
    weight_kg: Decimal


def _alias_map() -> dict[str, str]:
    """Return ``{from_name.lower(): to_name}`` for every seeded Hevy alias.

    Mirrors workout_imports.importers.hevy.HevyImporter._alias_map: one query
    for the whole sync instead of one HevyLiftAlias SELECT per set.
    """
    return {
        from_name.lower(): to_name
        for from_name, to_name in HevyLiftAlias.objects.values_list(
            "from_name", "to_name"
        )
    }


def _parse_start_date(raw: str) -> date | None:
    """Parse a Hevy ``start_time`` ISO timestamp into a date."""
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
    except ValueError:
        logger.warning("Skipping Hevy workout: unparseable start_time %r", raw)
        return None


def _parse_workout(workout: dict, alias_map: dict[str, str]) -> list[ParsedSet]:
    """Expand one Hevy workout payload into its completed working sets.

    Skips warmup sets and sets with no reps recorded (cardio-style sets that
    track distance_meters/duration_seconds instead) -- not an error, just not
    a set this sync scores. One malformed exercise/set is skipped, never
    aborts the whole workout.
    """
    performed_at = _parse_start_date(workout.get("start_time", ""))
    if performed_at is None:
        return []

    parsed: list[ParsedSet] = []
    for exercise in workout.get("exercises") or []:
        title = (exercise.get("title") or "").strip()
        if not title:
            continue
        lift = alias_map.get(title.lower(), title)
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
            source=LiftSource.HEVY_API,
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

    Scoped to source=HEVY_API (unlike liftosaur.services.history_watermark, which
    aggregates across the whole shared LiftHistory pool). LiftHistory is a single
    pool across every source, but TASK-319 showed that pooling the watermark
    itself is the wrong move for a *live-sync* connector: a user who already has
    Liftosaur, CSV, or manual history gets ``since=<today>`` the moment they
    connect Hevy for the first time, because the pool's newest row belongs to
    another source entirely -- zero backfill, no error shown. Sync order in
    challenges.services.sync_and_score (Liftosaur before Hevy) makes it worse:
    Liftosaur's own pull can move the shared watermark before this function ever
    reads it, so even a freshly-onboarded dual-connected user gets poisoned.
    Scoping to HEVY_API sidesteps both: this connector's watermark can only move
    by this connector's own writes, so sync order and other-source history are
    both irrelevant. It's also strictly more accurate on an ongoing basis -- the
    shared watermark can park on another source's newer date and skip
    legitimately-new Hevy workouts that are still older than that date (see
    wger.services.history_watermark, which made the same scoping call for the
    same reason).

    One caveat worth naming: Hevy's ``/v1/workouts/events?since=`` filters on
    *event* time (when the workout was created/updated in Hevy), not
    performed_at (the workout date stored here). A workout backdated in Hevy to
    a date already at or behind this watermark, then edited later, is still
    picked up correctly (its event time is the edit), so this mismatch doesn't
    reintroduce a backfill gap -- it only means "since" is a performed_at value
    doing double duty as an event-time filter, same tolerance the unscoped
    version already accepted.
    """
    return LiftHistory.objects.filter(user=user, source=LiftSource.HEVY_API).aggregate(
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


def pull_events_into_pool(
    client, user, *, since: str, synced_at, max_pages: int
) -> int:
    """Walk paginated Hevy workout events, upserting completed sets into LiftHistory.

    ``since`` drives both the initial backfill (a date far in the past) and
    every subsequent delta sync (this connector's own HEVY_API-scoped
    watermark, see ``history_watermark``) -- Hevy's events
    endpoint returns full workout payloads for "updated" events regardless, so
    there is no separate backfill endpoint/code path to maintain. "deleted"
    events are not applied (see module docstring).

    ``max_pages`` bounds how far this single call will walk -- see
    MAX_EVENT_PAGES_PER_BACKGROUND_RUN / MAX_EVENT_PAGES_PER_INLINE_RUN for
    which one callers should pass and why they differ.

    Returns the number of completed sets persisted to the shared pool.
    """
    pooled = 0
    alias_map = _alias_map()
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
            page_sets.extend(_parse_workout(workout, alias_map))

        pooled += len(page_sets)
        _write_batch_with_retry(
            _history_rows(page_sets, user=user, synced_at=synced_at), user=user
        )
        page += 1

    if page_count > max_pages:
        logger.warning(
            "Hevy event pagination truncated for user %s: %s pages available, "
            "stopped after %s. The watermark has moved past what was pooled "
            "here, so the next sync resumes further along rather than "
            "re-walking this same window.",
            user.id,
            page_count,
            max_pages,
        )

    return pooled


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
    runs start from ``history_watermark`` (this module's own, HEVY_API-scoped
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

        has_synced_before = HevySyncLog.objects.filter(user=user, success=True).exists()
        if has_synced_before:
            watermark = history_watermark(user)
            since = (
                f"{watermark.isoformat()}T00:00:00Z"
                if watermark is not None
                else _backfill_since()
            )
        else:
            since = _backfill_since()

        synced_at = datetime.now(tz=UTC)
        pooled = pull_events_into_pool(
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
    sync_log.save(update_fields=["success", "completed_at", "result_summary"])
    logger.info("Hevy lift sync complete for user %s: %s sets pooled", user.id, pooled)
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


def _run_backfill_in_thread(user) -> None:
    """Thread entry point: sync lifts, then release this thread's DB connection."""
    from django.db import connection

    try:
        sync_user_lifts(user, max_pages=MAX_EVENT_PAGES_PER_BACKGROUND_RUN)
    except Exception:
        logger.exception("Hevy backfill thread failed for user %s", user.id)
    finally:
        connection.close()
