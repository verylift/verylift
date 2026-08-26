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
import time
import urllib.error
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from django.conf import settings
from django.db import OperationalError, transaction
from django.utils import timezone

from hevy_api.client import HevyAPIError, HevyClient
from hevy_api.models import HevySyncLog
from liftosaur.models import LiftHistory, LiftSource
from liftosaur.services import history_watermark
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

# Safety valve for the events walk: Hevy caps pageSize at 10, so a lifter with
# years of history could in principle page for a very long time on their first
# sync. Bounding it means one sync run can't hang indefinitely; a truncated
# first backfill is completed by the next scheduled/forced sync since it
# re-requests from the same (unmoved) watermark.
MAX_EVENT_PAGES_PER_RUN = 500

POOL_WRITE_BATCH_SIZE = 500
POOL_WRITE_RETRY_DELAYS = (0.1, 0.3, 0.9)


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


def pull_events_into_pool(client, user, *, since: str, synced_at) -> int:
    """Walk paginated Hevy workout events, upserting completed sets into LiftHistory.

    ``since`` drives both the initial backfill (a date far in the past) and
    every subsequent delta sync (the shared pool's watermark) -- Hevy's events
    endpoint returns full workout payloads for "updated" events regardless, so
    there is no separate backfill endpoint/code path to maintain. "deleted"
    events are not applied (see module docstring).

    Returns the number of completed sets persisted to the shared pool.
    """
    pooled = 0
    alias_map = _alias_map()
    page = 1
    page_count = 1

    while page <= page_count and page <= MAX_EVENT_PAGES_PER_RUN:
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

    if page_count > MAX_EVENT_PAGES_PER_RUN:
        logger.warning(
            "Hevy event pagination truncated for user %s: %s pages available, "
            "stopped after %s. The next sync resumes from the same watermark.",
            user.id,
            page_count,
            MAX_EVENT_PAGES_PER_RUN,
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


def sync_user_lifts(user, force: bool = False) -> int:
    """Pure per-user pull that keeps the shared LiftHistory pool fresh from Hevy.

    No-op (returns 0) when the user has no Hevy API key. Delta-aware: reuses
    the same shared-pool watermark liftosaur.services.sync_user_lifts does
    (LiftHistory is a single pool across every source, TASK-25/#11/#8), so a
    lifter who has ever synced Liftosaur or imported a Hevy CSV still gets a
    bounded first Hevy-API pull rather than always re-walking
    HISTORY_BACKFILL_DAYS. Unless ``force`` is True, the run is skipped when a
    successful Hevy pull completed within HEVY_SYNC_COOLDOWN_MINUTES.

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

        watermark = history_watermark(user)
        if watermark is not None:
            since = f"{watermark.isoformat()}T00:00:00Z"
        else:
            since = (
                timezone.now() - timedelta(days=HISTORY_BACKFILL_DAYS)
            ).date().isoformat() + "T00:00:00Z"

        synced_at = datetime.now(tz=UTC)
        pooled = pull_events_into_pool(client, user, since=since, synced_at=synced_at)
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
