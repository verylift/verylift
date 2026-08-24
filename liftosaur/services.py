"""Service functions for Liftosaur integration."""

import json
import logging
import re
import threading
import time
import urllib.error
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.db import OperationalError, transaction
from django.db.models import Max
from django.utils import timezone

from liftosaur.client import LiftosaurAPIError, LiftosaurClient
from liftosaur.models import (
    LB_TO_KG,
    Lift,
    LiftAlias,
    LiftHistory,
    LiftosaurSyncLog,
    LiftSource,
)

logger = logging.getLogger(__name__)

# How far back the one-time onboarding backfill reaches when a user has no
# stored history yet. Subsequent syncs use the delta watermark, not this.
HISTORY_BACKFILL_DAYS = 365

# Rows per INSERT when writing a page of pooled sets. Set explicitly because
# Django's automatic batch size is unbounded on PostgreSQL, and a 200-record
# API page can expand to more placeholders than the wire protocol allows.
POOL_WRITE_BATCH_SIZE = 500

# Backoff schedule for a pool write that loses a race for the write lock.
# Fixed (no jitter) so a test can patch time.sleep and assert the attempt count
# deterministically; the last delay being exhausted is what re-raises.
POOL_WRITE_RETRY_DELAYS = (0.1, 0.3, 0.9)


def canonical_lift_name(name: str) -> str:
    """Return the canonical standard lift name for a raw Liftosaur exercise name.

    Maps known Liftosaur aliases (e.g. "Squat" -> "Back Squat") to their
    canonical standard name via the seeded LiftAlias table; unknown names pass
    through unchanged.

    The lookup is case-insensitive: Liftosaur emits exercise names with casing
    that can differ from what the fixture author assumed (it emits "Behind The
    Neck Press" while the seeded alias reads "Behind the Neck Press"). An exact
    match would silently miss, pooling the set under its raw name so scoring's
    canonical-name filter never counts it.
    """
    alias = (
        LiftAlias.objects.filter(from_name__iexact=name)
        .values_list("to_name", flat=True)
        .first()
    )
    return alias if alias is not None else name


def _alias_map() -> dict[str, str]:
    """Return ``{from_name.lower(): to_name}`` for every alias in one query.

    ``canonical_lift_name`` costs one LiftAlias SELECT per call, which inside
    the pull loop meant one query per parsed set — the read-side twin of the
    per-set write this module used to perform. Keying on the lowercased name
    reproduces that function's ``from_name__iexact`` semantics for the seeded
    data, so a caller can resolve a whole page of sets against a single fetch.
    """
    return {
        from_name.lower(): to_name
        for from_name, to_name in LiftAlias.objects.values_list("from_name", "to_name")
    }


def liftosaur_builtin_lift_names() -> frozenset[str]:
    """Return the lift names Liftosaur ships natively, from the seeded Lift table.

    Lifts absent from this set need a custom exercise provisioned in the
    user's Liftosaur account before they can be logged.
    """
    return Lift.builtin_names()


def validate_liftosaur_key(api_key: str) -> bool:
    """Validate a Liftosaur API key by calling the measurements/weight endpoint.

    This is purely a key-validation probe (it happens to hit the
    weight-measurements endpoint, but nothing about bodyweight survives past
    this call — TASK-248 removed bodyweight from the product entirely). Keep
    the call: it is what confirms the key actually works against the live API
    before an account is created or a key is saved.
    """
    client = LiftosaurClient(api_key)
    try:
        client.get_weight_measurements()
        return True
    except LiftosaurAPIError as exc:
        logger.warning("Liftosaur key validation rejected by API: %s", exc)
        return False
    except (urllib.error.URLError, OSError) as exc:
        logger.warning("Liftosaur key validation failed due to network error: %s", exc)
        return False
    except Exception:
        logger.exception("Liftosaur key validation failed unexpectedly")
        return False


@dataclass(frozen=True)
class ParsedSet:
    """A single completed set parsed from Liftohistory text.

    ``equipment`` is the equipment/variant suffix from the exercise section
    (e.g. "Leverage Machine" in "Pull Up, Leverage Machine / ..."), or "" when
    the line names no equipment. It distinguishes an assisted-machine set —
    whose recorded weight is already the net total load — from a free
    bodyweight or added-weight set of the same lift.
    """

    exercise: str
    performed_at: datetime
    reps: int
    weight_kg: Decimal
    equipment: str = ""


_WEIGHT_RE = re.compile(r"^\s*([\d.]+)\s*(kg|lb)\s*$", re.IGNORECASE)
# Weight allows a leading minus sign: assisted equipment or custom Liftoscript
# progressions can in principle report a negative (assistance) value, which
# must parse rather than be silently dropped or misread as positive.
_SET_GROUP_RE = re.compile(
    r"(?P<sets>\d+)x(?P<reps>[\d|+]+)\s+(?P<weight>-?[\d.]+)(?P<unit>lb|kg)",
    re.IGNORECASE,
)
_DATE_FORMATS = (
    "%Y-%m-%d",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%dT%H:%M:%S.%f%z",
    "%Y-%m-%d %H:%M:%S %z",
)


def _parse_weight_value(raw: str) -> tuple[Decimal, str] | None:
    """Parse an embedded-unit weight string like '80kg' into (Decimal, unit)."""
    match = _WEIGHT_RE.match(raw or "")
    if match is None:
        return None
    try:
        amount = Decimal(match.group(1))
    except InvalidOperation:
        return None
    return amount, match.group(2).lower()


def _parse_date(raw: str) -> datetime | None:
    """Parse a Liftosaur date/timestamp string into a timezone-aware datetime."""
    for fmt in _DATE_FORMATS:
        try:
            parsed = datetime.strptime(raw, fmt)
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed
    return None


def _parse_reps(token: str) -> int | None:
    """Parse the reps token of a set group (e.g. ``3+``, ``8|7``) into an int.

    AMRAP markers (``+``) are stripped; unilateral notation (``8|7``) takes the
    first (right-side) count.
    """
    cleaned = token.split("|")[0].replace("+", "").strip()
    return int(cleaned) if cleaned.isdigit() else None


def _parse_completed_sets(
    exercise: str, equipment: str, completed: str, performed_at: datetime
) -> list[ParsedSet]:
    """Expand a completed-sets section into individual ParsedSet entries.

    ``completed`` is a comma-separated list of ``NxM weight[unit][@rpe][+]``
    groups; each group yields N entries of M reps at the given weight (lb
    converted to kg). RPE suffixes are ignored.
    """
    sets: list[ParsedSet] = []
    for match in _SET_GROUP_RE.finditer(completed):
        reps = _parse_reps(match.group("reps"))
        if reps is None:
            continue
        try:
            weight = Decimal(match.group("weight"))
        except InvalidOperation:
            continue
        if match.group("unit").lower() == "lb":
            weight = weight * LB_TO_KG
        sets.extend(
            ParsedSet(
                exercise=exercise,
                performed_at=performed_at,
                reps=reps,
                weight_kg=weight,
                equipment=equipment,
            )
            for _ in range(int(match.group("sets")))
        )
    return sets


def _parse_exercise_line(line: str, performed_at: datetime) -> list[ParsedSet]:
    """Parse one exercise line inside the ``exercises`` block.

    Format: ``ExerciseName[, Equipment] / completed sets [/ warmup: ...] [/
    target: ...]``. The equipment suffix is split off the exercise name and
    retained on each ParsedSet (it changes what the recorded weight *means*
    for assisted-machine sets), and only the completed-sets section (the
    first one) is scored — warmup and target sections are skipped.
    """
    sections = [section.strip() for section in line.split(" / ")]
    if len(sections) < 2:
        return []
    name_parts = sections[0].split(", ", 1)
    name = name_parts[0].strip()
    equipment = name_parts[1].strip() if len(name_parts) > 1 else ""
    completed = sections[1]
    if completed.startswith(("warmup:", "target:")):
        return []
    return _parse_completed_sets(name, equipment, completed, performed_at)


def _parse_history_record(text: str) -> list[ParsedSet]:
    """Parse one Liftohistory record string into its completed sets.

    The record opens with a header line whose first ``/``-separated token is the
    workout date, followed by an ``exercises: { ... }`` block. Each line inside
    the block is one exercise; only its completed sets (not warmup/target) are
    scored. ``//`` note lines are ignored.
    """
    performed_at: datetime | None = None
    in_block = False
    sets: list[ParsedSet] = []

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("//"):
            continue
        if not in_block:
            if performed_at is None:
                performed_at = _parse_date(line.split("/")[0].strip())
            if "{" in line:
                in_block = True
            continue
        if line.startswith("}"):
            break
        if performed_at is not None:
            sets.extend(_parse_exercise_line(line, performed_at))

    return sets


def _history_rows_for_page(
    user, parsed_sets, *, synced_at, alias_map
) -> list[LiftHistory]:
    """Build the unsaved LiftHistory rows for one page of parsed sets.

    Keyed on (user, lift, performed_at, reps, weight_kg) — the identity of a set,
    since Liftosaur exposes no stable set ID. Re-syncing the same workout upserts
    rather than duplicates, while two genuinely different sets on the same day
    with the same rep count (a bodyweight Pull-up recorded as 0 vs an assisted
    Pull-up recorded as its net load, or a top set vs a lighter back-off set)
    land in separate rows instead of the second silently overwriting the first.

    equipment is deliberately not part of that key: rows pooled before equipment
    was captured get restamped in place by a full re-sync instead of being
    orphaned as stale blank-equipment duplicates.

    weight_kg is quantized to the column's two decimal places before it is used
    as a key: the parsed value can carry more precision (lb→kg conversion), and
    matching the stored, rounded value is what keeps a re-sync an upsert rather
    than a unique-constraint violation.

    Rows are accumulated into a dict on that key so intra-page duplicates
    collapse last-wins before they reach the database. This is not an
    optimization — ``_parse_completed_sets`` expands "3x5 100kg" into three
    identical ParsedSets, and a single ``INSERT ... ON CONFLICT DO UPDATE``
    naming the same conflict target three times makes PostgreSQL raise
    "ON CONFLICT DO UPDATE command cannot affect row a second time" (SQLite
    silently tolerates it). Last-wins is also exactly what the per-set
    ``update_or_create`` this replaced did when two sets shared the key but
    differed in equipment, so collapsing this way preserves the old behavior.
    """
    rows: dict[tuple, LiftHistory] = {}
    for parsed_set in parsed_sets:
        lift = alias_map.get(parsed_set.exercise.lower(), parsed_set.exercise)
        performed_at = parsed_set.performed_at.date()
        weight_kg = parsed_set.weight_kg.quantize(Decimal("0.01"))
        rows[(lift, performed_at, parsed_set.reps, weight_kg)] = LiftHistory(
            user=user,
            lift=lift,
            performed_at=performed_at,
            reps=parsed_set.reps,
            weight_kg=weight_kg,
            equipment=parsed_set.equipment,
            synced_at=synced_at,
            source=LiftSource.LIFTOSAUR,
        )
    return list(rows.values())


def _write_history_batch(rows) -> None:
    """Upsert a page's worth of LiftHistory rows in one transaction.

    One commit for the whole page instead of one auto-committed write per set:
    that is the point of this function. Under SQLite it collapses N chances to
    collide with a concurrent writer into one; under PostgreSQL it turns N
    round-trips into one and makes the page all-or-nothing.

    ``bulk_create``'s return value must not be used here: with
    ``update_conflicts`` the returned objects for conflicting rows carry the
    client-generated UUID this process invented, not the pk of the row that
    actually survived in the table.
    """
    if not rows:
        return
    with transaction.atomic():
        LiftHistory.objects.bulk_create(
            rows,
            batch_size=POOL_WRITE_BATCH_SIZE,
            update_conflicts=True,
            unique_fields=["user", "lift", "performed_at", "reps", "weight_kg"],
            update_fields=["equipment", "synced_at"],
        )


def _write_history_batch_with_retry(rows, *, user) -> None:
    """Write one page of pooled rows, retrying transient write contention.

    The retry loop sits *outside* ``transaction.atomic()`` on purpose: each
    attempt needs its own transaction. Retrying a statement inside a
    transaction the database has already aborted cannot succeed, and on
    PostgreSQL every subsequent query in that transaction fails too.

    Only OperationalError is retried — that is the transient
    "database is locked" / lock-timeout family. An IntegrityError means the data
    is wrong, not the timing, and must keep propagating.
    """
    for attempt, delay in enumerate(POOL_WRITE_RETRY_DELAYS, start=1):
        try:
            _write_history_batch(rows)
            return
        except OperationalError as exc:
            logger.warning(
                "Liftosaur pool write hit DB contention for user %s "
                "(attempt %s/%s), retrying in %ss: %s",
                user.id,
                attempt,
                len(POOL_WRITE_RETRY_DELAYS) + 1,
                delay,
                exc,
            )
            time.sleep(delay)
    _write_history_batch(rows)


def history_watermark(user):
    """Return the latest performed_at in the user's LiftHistory pool, or None.

    Drives delta sync: subsequent fetches request only records newer than this.
    """
    return LiftHistory.objects.filter(user=user).aggregate(latest=Max("performed_at"))[
        "latest"
    ]


def recent_pull_exists(user) -> bool:
    """Return True if a successful Liftosaur pull for the user is within cooldown.

    The LiftHistory pool is shared and challenge-independent, so any recent
    successful pull — backfill or challenge sync — means the pool was just
    refreshed and an immediate re-pull would be redundant. Callers pass
    ``force=True`` to bypass this.
    """
    cutoff = timezone.now() - timedelta(
        minutes=settings.LIFTOSAUR_SYNC_COOLDOWN_MINUTES
    )
    return LiftosaurSyncLog.objects.filter(
        user=user,
        success=True,
        started_at__gte=cutoff,
    ).exists()


def last_synced_at(user):
    """Return the started_at of the user's most recent successful Liftosaur pull.

    Returns ``None`` if the user has no successful sync yet. Failed syncs are
    ignored so the stamp reflects when data was actually last refreshed. Uses the
    same queryset shape as ``recent_pull_exists``.
    """
    latest = (
        LiftosaurSyncLog.objects.filter(user=user, success=True)
        .order_by("-started_at")
        .values_list("started_at", flat=True)
        .first()
    )
    return latest


def pull_history_into_pool(
    client, user, *, start_date, end_date=None, synced_at
) -> int:
    """Walk paginated Liftosaur history, upserting each parsed set into LiftHistory.

    Returns the number of completed sets persisted to the shared pool.

    ``end_date`` is an optional, open-ended upper bound retained for deliberate
    bounded/chunked historical backfills. Liftosaur normalizes endDate to
    midnight UTC (00:00:00.000Z) and filters records with a string date-range
    comparison — passing today's date as endDate silently excludes any workout
    completed earlier that same UTC day. Omit end_date for routine/delta syncs;
    only pass it deliberately for a bounded historical window.

    Liftosaur ignores the pagination ``cursor`` whenever ``startDate`` is set and
    just returns the first page repeatedly, so a stall guard breaks out if the
    server hands back the same cursor with ``has_more`` still set.

    Each API page is written in exactly one transaction — not the whole pull.
    That is deliberate: wrapping the loop would hold a write transaction open
    across every HTTP round-trip, which under SQLite means holding the write
    lock across network latency and makes lock contention worse rather than
    better. In practice a real sync is one page anyway (``sync_user_lifts``
    always sends ``start_date``, and Liftosaur then ignores the cursor, so the
    stall guard ends the loop after the second fetch), so per-page is
    all-or-nothing for every real sync. Where a genuinely multi-page pull does
    fail partway, earlier pages stay committed — the same "truncated window
    persists" semantics the stall guard above already accepts.
    """
    pooled = 0
    cursor: str | None = None
    alias_map = _alias_map()
    while True:
        records, has_more, next_cursor = client.get_history(
            start_date=start_date, end_date=end_date, cursor=cursor, limit=200
        )
        page_sets: list[ParsedSet] = []
        for record in records:
            page_sets.extend(_parse_history_record(record))
        # pooled counts parsed sets, not rows: "3x5" contributes 3 even though
        # it collapses to one row.
        pooled += len(page_sets)
        _write_history_batch_with_retry(
            _history_rows_for_page(
                user, page_sets, synced_at=synced_at, alias_map=alias_map
            ),
            user=user,
        )

        if not has_more or not next_cursor:
            break
        if next_cursor == cursor:
            logger.warning(
                "Liftosaur history pagination stalled for user %s: cursor %s "
                "repeated with has_more set (server ignores the cursor when "
                "startDate is present); stopping to avoid an infinite loop. "
                "Records beyond this page are truncated for this window.",
                user.id,
                next_cursor,
            )
            break
        cursor = next_cursor

    return pooled


def _mark_sync_log_failed(sync_log, detail: str, *, user) -> None:
    """Stamp a sync log as failed without letting the bookkeeping raise in turn.

    The write that records a lock failure can itself lose the write lock. If it
    does there is nothing useful left to do — logging it is the whole remedy —
    and it must not resurrect the very 500 the caller is degrading away from.
    """
    sync_log.success = False
    sync_log.completed_at = datetime.now(tz=UTC)
    sync_log.error_detail = detail
    try:
        sync_log.save(update_fields=["success", "completed_at", "error_detail"])
    except OperationalError:
        logger.exception(
            "Could not record the failed Liftosaur sync log for user %s: the "
            "database is still refusing writes",
            user.id,
        )


def sync_user_lifts(user, force: bool = False, full_backfill: bool = False) -> int:
    """Pure per-user pull that keeps the shared LiftHistory pool fresh.

    This is the canonical 'sync user lifts' primitive: it walks Liftosaur
    history and upserts every completed set into the shared per-user
    LiftHistory pool. It performs NO scoring — scoring is a separate, explicit
    concern (scoring.services.score_pooled_history) composed by callers. This
    primitive knows nothing about challenges.

    No-op (returns 0) when the user has no Liftosaur API key.

    Delta-aware: once history exists, the fetch starts at the latest stored
    performed_at, so a re-run never re-pulls the full year; the first-ever pull
    reaches back HISTORY_BACKFILL_DAYS. Passing ``full_backfill=True`` ignores
    the watermark and re-pulls the whole HISTORY_BACKFILL_DAYS window,
    re-upserting existing rows in place — used to restamp fields (e.g.
    equipment) onto history pooled before those fields were captured.
    Unless ``force`` is True the run is
    skipped when any successful pull for this user completed within the
    LIFTOSAUR_SYNC_COOLDOWN_MINUTES window — the cooldown applies only to this
    API pull, and it gates the shared pool once for every challenge.

    Returns the number of sets pooled.
    """
    if not user.liftosaur_api_key:
        logger.info(
            "Skipping lift history backfill for user %s: no Liftosaur API key", user.id
        )
        return 0

    if not force and recent_pull_exists(user):
        logger.debug("Skipping lift sync for user %s: within cooldown window", user.id)
        return 0

    sync_log = LiftosaurSyncLog.objects.create(
        user=user,
        started_at=datetime.now(tz=UTC),
        success=None,
    )

    try:
        client = LiftosaurClient(user.liftosaur_api_key)

        watermark = history_watermark(user)
        if watermark is not None and not full_backfill:
            start_date = watermark.isoformat()
        else:
            start_date = (
                (timezone.now() - timedelta(days=HISTORY_BACKFILL_DAYS))
                .date()
                .isoformat()
            )

        # No end_date: the fetch is open-ended above the watermark ("everything
        # since"). Liftosaur truncates an endDate to midnight UTC and filters by
        # string date range, so passing today's date would silently drop any
        # workout completed earlier the same UTC day (see pull_history_into_pool).
        synced_at = datetime.now(tz=UTC)
        pooled = pull_history_into_pool(
            client,
            user,
            start_date=start_date,
            synced_at=synced_at,
        )
    except LiftosaurAPIError as exc:
        _mark_sync_log_failed(sync_log, str(exc), user=user)
        logger.exception("Liftosaur lift sync failed for user %s", user.id)
        return 0
    except (urllib.error.URLError, OSError) as exc:
        # A slow/unreachable Liftosaur API. OSError also catches the bare
        # TimeoutError a read-phase timeout surfaces as (TimeoutError is an
        # OSError subclass) -- core.http.send_request's docstring only
        # promises urllib.error.URLError for network failures, which a
        # read-phase timeout isn't one of. Degrade exactly like the API-error
        # branch: a 500 reaching the user for a transient network hiccup is
        # strictly worse than showing whatever is already pooled.
        _mark_sync_log_failed(sync_log, f"Network error: {exc}", user=user)
        logger.exception("Liftosaur lift sync network error for user %s", user.id)
        return 0
    except OperationalError as exc:
        # A write-lock loss that outlived the pool write's own retries. Degrade
        # exactly like the API-error branch does — return 0 so callers treat it
        # as "the pool was not refreshed this run" and sync_and_score still
        # scores whatever is already pooled, instead of a 500 reaching the user.
        _mark_sync_log_failed(sync_log, f"DB contention: {exc}", user=user)
        logger.exception(
            "Liftosaur lift sync aborted by DB contention for user %s", user.id
        )
        return 0

    sync_log.success = True
    sync_log.completed_at = datetime.now(tz=UTC)
    sync_log.result_summary = json.dumps({"sets_pooled": pooled})
    sync_log.save(update_fields=["success", "completed_at", "result_summary"])
    logger.info(
        "Liftosaur lift sync complete for user %s: %s sets pooled", user.id, pooled
    )
    return pooled


def trigger_lift_history_backfill(user) -> None:
    """Run sync_user_lifts in a daemon thread so it never blocks the caller.

    Used at registration: the initial 12-month seed runs off the
    request/response cycle.
    """
    thread = threading.Thread(target=_run_backfill_in_thread, args=(user,), daemon=True)
    thread.start()


def _run_backfill_in_thread(user) -> None:
    """Thread entry point: sync lifts, then release this thread's DB connection."""
    from django.db import connection

    try:
        sync_user_lifts(user)
    except Exception:
        logger.exception("Liftosaur backfill thread failed for user %s", user.id)
    finally:
        connection.close()
