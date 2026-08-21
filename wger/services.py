"""Service functions for the Wger integration.

Mirrors liftosaur.services (sync/watermark/sync-log/cooldown pattern), adapted
for two real differences in Wger's API:

- Wger is self-hostable: there's no fixed base URL, so every client call needs
  the user's own instance URL alongside their token.
- Wger's exercise database is normalized -- a workout log entry names its
  exercise by a numeric ID, not a raw string. Each pull resolves unique
  exercise IDs to names via a second API call (cached per-pull in-memory; not
  persisted across syncs -- see the PR description for why that scope was cut)
  before running them through WgerLiftAlias, exactly like Liftosaur's raw
  exercise-name strings are run through LiftAlias.

Weight and repetition units are resolved live from Wger's
setting-weightunit/setting-repetitionunit endpoints each sync, rather than
assumed by ID -- a self-hosted instance's fixture data could in principle be
re-numbered.
"""

import json
import logging
import threading
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
from django.conf import settings
from django.db import OperationalError, transaction
from django.db.models import Max
from django.utils import timezone
from wger_api_client.models.repetition_unit import RepetitionUnit
from wger_api_client.types import Unset

from accounts.units import LB_TO_KG
from liftosaur.models import LiftHistory, LiftSource
from wger.client import WgerAPIError, WgerClient
from wger.models import WgerLiftAlias, WgerSyncLog

logger = logging.getLogger(__name__)

# How far back the one-time backfill reaches when a user has no stored Wger
# history yet. Subsequent syncs use the delta watermark, not this.
HISTORY_BACKFILL_DAYS = 365

# Rows per INSERT when writing a page of pooled sets.
POOL_WRITE_BATCH_SIZE = 500

# Backoff schedule for a pool write that loses a race for the write lock.
POOL_WRITE_RETRY_DELAYS = (0.1, 0.3, 0.9)


def _is_unset(value) -> bool:
    """True for None or Wger's ``Unset`` sentinel (a field absent server-side)."""
    return value is None or isinstance(value, Unset)


def canonical_wger_lift_name(name: str) -> str:
    """Return the canonical standard lift name for a raw Wger exercise name.

    Mirrors liftosaur.services.canonical_lift_name. Case-insensitive lookup
    against the seeded WgerLiftAlias table; unknown names pass through
    unchanged.
    """
    alias = (
        WgerLiftAlias.objects.filter(from_name__iexact=name)
        .values_list("to_name", flat=True)
        .first()
    )
    return alias if alias is not None else name


def _alias_map() -> dict[str, str]:
    """Return ``{from_name.lower(): to_name}`` for every alias in one query."""
    return {
        from_name.lower(): to_name
        for from_name, to_name in WgerLiftAlias.objects.values_list(
            "from_name", "to_name"
        )
    }


def validate_wger_credentials(base_url: str, api_token: str) -> bool:
    """Validate a Wger instance URL + API token by calling the workoutlog endpoint.

    Purely a credential-validation probe -- nothing about the returned page
    survives past this call.
    """
    client = WgerClient(base_url, api_token, timeout=settings.WGER_API_TIMEOUT)
    try:
        client.get_workout_logs(limit=1)
        return True
    except WgerAPIError as exc:
        logger.warning("Wger credential validation rejected by API: %s", exc)
        return False
    except (httpx.HTTPError, OSError) as exc:
        logger.warning(
            "Wger credential validation failed due to network error: %s", exc
        )
        return False
    except Exception:
        logger.exception("Wger credential validation failed unexpectedly")
        return False


def _weight_kg(
    raw_weight, weight_unit_id, weight_units: dict[int, str]
) -> Decimal | None:
    """Convert a raw WorkoutLog weight + unit ID into kg.

    The unit ID is resolved against ``weight_units`` (this sync's live
    id->name map from Wger's setting-weightunit endpoint) by name,
    case-insensitively. An unrecognized name, or an ID missing from the map,
    falls back to treating the value as already kg.
    """
    if raw_weight in (None, ""):
        return None
    try:
        amount = Decimal(str(raw_weight))
    except Exception:
        return None
    unit_name = weight_units.get(weight_unit_id, "")
    multiplier = {"kg": Decimal(1), "lb": LB_TO_KG}.get(unit_name.lower(), Decimal(1))
    return amount * multiplier


def _resolve_exercise_name(client, exercise_id, name_cache: dict) -> str | None:
    """Resolve exercise_id to a name, caching within this pull only."""
    if exercise_id in name_cache:
        return name_cache[exercise_id]
    name = client.get_exercise_name(exercise_id)
    name_cache[exercise_id] = name
    return name


def _history_rows_for_page(
    user,
    entries,
    *,
    client,
    synced_at,
    alias_map,
    name_cache,
    weight_units: dict[int, str],
    repetition_units: dict[int, RepetitionUnit],
) -> list[LiftHistory]:
    """Build the unsaved LiftHistory rows for one page of WorkoutLog entries.

    Keyed on (user, lift, performed_at, reps, weight_kg), mirroring
    liftosaur.services._history_rows_for_page -- Wger exposes no stable
    cross-sync set identity either, so the same "who/which lift/which day/reps/
    load" key is what keeps a re-sync an upsert.

    Entries whose repetitions_unit isn't plain "Repetitions" (e.g. "Until
    Failure") are skipped: the pooled reps column is a bare integer and can't
    represent those units meaningfully. Which unit id counts as "Repetitions"
    is resolved live via ``repetition_units`` (unit_type == "REPETITIONS"),
    not assumed by id.
    """
    rows: dict[tuple, LiftHistory] = {}
    for entry in entries:
        repetitions_unit_id = entry.repetitions_unit
        if not _is_unset(repetitions_unit_id):
            unit = repetition_units.get(repetitions_unit_id)
            if unit is None or unit.unit_type != "REPETITIONS":
                continue

        reps_raw = entry.repetitions
        weight_kg = _weight_kg(entry.weight, entry.weight_unit, weight_units)
        entry_date = entry.date
        exercise_id = entry.exercise
        if (
            _is_unset(reps_raw)
            or weight_kg is None
            or _is_unset(entry_date)
            or _is_unset(exercise_id)
        ):
            continue

        try:
            reps_decimal = Decimal(str(reps_raw))
        except Exception:
            continue
        if reps_decimal != reps_decimal.to_integral_value():
            continue
        reps = int(reps_decimal)

        raw_name = _resolve_exercise_name(client, exercise_id, name_cache)
        if not raw_name:
            continue

        lift = alias_map.get(raw_name.lower(), raw_name)
        performed_at = entry_date.date()
        weight_kg = weight_kg.quantize(Decimal("0.01"))
        rows[(lift, performed_at, reps, weight_kg)] = LiftHistory(
            user=user,
            lift=lift,
            performed_at=performed_at,
            reps=reps,
            weight_kg=weight_kg,
            equipment="",
            synced_at=synced_at,
            source=LiftSource.WGER,
        )
    return list(rows.values())


def _write_history_batch(rows) -> None:
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
    for attempt, delay in enumerate(POOL_WRITE_RETRY_DELAYS, start=1):
        try:
            _write_history_batch(rows)
            return
        except OperationalError as exc:
            logger.warning(
                "Wger pool write hit DB contention for user %s "
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
    """Return the latest performed_at among the user's pooled Wger sets, or None.

    Scoped to source=WGER (unlike liftosaur's history_watermark, which doesn't
    need the filter since it's the only writer into that pool) so an
    independent Liftosaur sync's watermark never leaks into Wger's delta pull.
    """
    return LiftHistory.objects.filter(user=user, source=LiftSource.WGER).aggregate(
        latest=Max("performed_at")
    )["latest"]


def recent_pull_exists(user) -> bool:
    """True if a successful Wger pull for the user is within cooldown."""
    cutoff = timezone.now() - timedelta(minutes=settings.WGER_SYNC_COOLDOWN_MINUTES)
    return WgerSyncLog.objects.filter(
        user=user, success=True, started_at__gte=cutoff
    ).exists()


def last_synced_at(user):
    """Return the started_at of the user's most recent successful Wger pull."""
    return (
        WgerSyncLog.objects.filter(user=user, success=True)
        .order_by("-started_at")
        .values_list("started_at", flat=True)
        .first()
    )


def pull_workout_logs_into_pool(client, user, *, start_date, synced_at) -> int:
    """Walk paginated Wger workout logs, upserting each entry into LiftHistory.

    Returns the number of sets persisted to the shared pool. Each API page is
    written in exactly one transaction, mirroring
    liftosaur.services.pull_history_into_pool.
    """
    pooled = 0
    offset = 0
    limit = 100
    alias_map = _alias_map()
    name_cache: dict = {}
    weight_units = client.get_weight_units()
    repetition_units = client.get_repetition_units()
    while True:
        entries, has_more, next_offset = client.get_workout_logs(
            date_gte=start_date, limit=limit, offset=offset
        )
        rows = _history_rows_for_page(
            user,
            entries,
            client=client,
            synced_at=synced_at,
            alias_map=alias_map,
            name_cache=name_cache,
            weight_units=weight_units,
            repetition_units=repetition_units,
        )
        pooled += len(rows)
        _write_history_batch_with_retry(rows, user=user)

        if not has_more:
            break
        offset = next_offset

    return pooled


def _mark_sync_log_failed(sync_log, detail: str, *, user) -> None:
    sync_log.success = False
    sync_log.completed_at = datetime.now(tz=UTC)
    sync_log.error_detail = detail
    try:
        sync_log.save(update_fields=["success", "completed_at", "error_detail"])
    except OperationalError:
        logger.exception(
            "Could not record the failed Wger sync log for user %s: the "
            "database is still refusing writes",
            user.id,
        )


def sync_wger_lifts(user, force: bool = False, full_backfill: bool = False) -> int:
    """Pure per-user pull that keeps the shared LiftHistory pool fresh from Wger.

    Mirrors liftosaur.services.sync_user_lifts. No-op (returns 0) when the
    user has no Wger instance URL or API token configured.
    """
    if not user.wger_instance_url or not user.wger_api_token:
        logger.info(
            "Skipping Wger lift history sync for user %s: no instance URL/token",
            user.id,
        )
        return 0

    if not force and recent_pull_exists(user):
        logger.debug("Skipping Wger sync for user %s: within cooldown window", user.id)
        return 0

    sync_log = WgerSyncLog.objects.create(
        user=user, started_at=datetime.now(tz=UTC), success=None
    )

    try:
        client = WgerClient(
            user.wger_instance_url,
            user.wger_api_token,
            timeout=settings.WGER_API_TIMEOUT,
        )

        watermark = history_watermark(user)
        if watermark is not None and not full_backfill:
            start_date = watermark.isoformat()
        else:
            start_date = (
                (timezone.now() - timedelta(days=HISTORY_BACKFILL_DAYS))
                .date()
                .isoformat()
            )

        synced_at = datetime.now(tz=UTC)
        pooled = pull_workout_logs_into_pool(
            client, user, start_date=start_date, synced_at=synced_at
        )
    except WgerAPIError as exc:
        _mark_sync_log_failed(sync_log, str(exc), user=user)
        logger.exception("Wger lift sync failed for user %s", user.id)
        return 0
    except OperationalError as exc:
        _mark_sync_log_failed(sync_log, f"DB contention: {exc}", user=user)
        logger.exception("Wger lift sync aborted by DB contention for user %s", user.id)
        return 0

    sync_log.success = True
    sync_log.completed_at = datetime.now(tz=UTC)
    sync_log.result_summary = json.dumps({"sets_pooled": pooled})
    sync_log.save(update_fields=["success", "completed_at", "result_summary"])
    logger.info("Wger lift sync complete for user %s: %s sets pooled", user.id, pooled)
    return pooled


def trigger_wger_lift_history_backfill(user) -> None:
    """Run sync_wger_lifts in a daemon thread so it never blocks the caller.

    Used when a user connects Wger from Settings, mirroring
    liftosaur.services.trigger_lift_history_backfill.
    """
    thread = threading.Thread(target=_run_backfill_in_thread, args=(user,), daemon=True)
    thread.start()


def _run_backfill_in_thread(user) -> None:
    from django.db import connection

    try:
        sync_wger_lifts(user)
    except Exception:
        logger.exception("Wger backfill thread failed for user %s", user.id)
    finally:
        connection.close()
