"""Service functions for the generic workout-CSV import (#11).

One upload endpoint accepts a CSV export from any supported tracker app; the
importer registry (workout_imports.importers) detects which app produced a
given file and parses it. This is a one-shot manual upload-and-pool action,
not a polled/watermarked sync like liftosaur.services.sync_user_lifts.
"""

import logging
from dataclasses import dataclass

from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from liftosaur.models import LiftHistory, LiftSource
from workout_imports.importers import REGISTRY, detect_importer

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ImportResult:
    """Outcome of one workout-CSV import: which app it came from and how much
    was pooled, so the caller can report both in the success message."""

    source: LiftSource
    pooled_count: int


def _history_rows(user, parsed_sets, *, source, synced_at) -> list[LiftHistory]:
    """Build the unsaved LiftHistory rows for one parsed CSV, deduped by key.

    Keyed on (lift, performed_at, reps, weight_kg) -- a set's identity given
    no tracker export used here carries a stable cross-import set ID.
    Collapsing intra-file duplicates onto one dict entry before the INSERT is
    what keeps a re-upload of the exact same file an idempotent upsert rather
    than a multi-affect ON CONFLICT error.
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
            equipment="",
            synced_at=synced_at,
            source=source,
        )
    return list(rows.values())


def import_workout_csv(user, file_obj) -> ImportResult:
    """Detect an uploaded CSV's tracker format, parse it, and pool its sets.

    One file, one transaction -- no pagination/retry/cooldown machinery,
    unlike liftosaur.services.pull_history_into_pool, since this is a single
    user-initiated upload rather than a polled external API.
    """
    importer = detect_importer(file_obj)
    file_obj.seek(0)
    parsed_sets = importer.parse(file_obj)
    synced_at = timezone.now()
    rows = _history_rows(user, parsed_sets, source=importer.source, synced_at=synced_at)

    if rows:
        with transaction.atomic():
            LiftHistory.objects.bulk_create(
                rows,
                update_conflicts=True,
                unique_fields=["user", "lift", "performed_at", "reps", "weight_kg"],
                update_fields=["equipment", "synced_at"],
            )

    logger.info(
        "Workout CSV import pooled %s sets for user %s (source=%s)",
        len(parsed_sets),
        user.id,
        importer.source,
    )
    return ImportResult(source=importer.source, pooled_count=len(parsed_sets))


def last_imported_at(user):
    """Return the synced_at of the user's most recent workout CSV import.

    Returns None if the user has never imported a workout CSV. Spans every
    registered importer's source (not just one tracker), so the settings
    page's "last imported" stamp stays correct as more importers are added.
    """
    sources = [importer.source for importer in REGISTRY]
    return LiftHistory.objects.filter(user=user, source__in=sources).aggregate(
        latest=Max("synced_at")
    )["latest"]
