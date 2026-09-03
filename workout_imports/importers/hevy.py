"""Hevy CSV export importer (#11).

Hevy's CSV export is available to free-tier users (its API is Pro-gated).
One row in the export is one completed set; a workout's sets share a
start_time.
"""

import csv
import io
import logging
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from accounts.units import LB_TO_KG
from core.lift_resolution import LiftNameResolver, build_lift_alias_maps
from core.models import Lift, LiftAliasSource, LiftSource
from workout_imports.importers.base import ParsedSet, decode_csv_text

logger = logging.getLogger(__name__)

# Columns this importer actually reads. Hevy's real export carries more
# columns (title, end_time, description, superset_id, exercise_notes,
# set_index, distance_km, duration_seconds, rpe) but those aren't needed to
# pool a strength set, so they aren't required here. This set doubles as the
# detection signature: it's distinctive enough that an unrelated CSV won't
# accidentally match it.
REQUIRED_HEADERS = frozenset(
    {"exercise_title", "start_time", "set_type", "weight_lbs", "reps"}
)

# Hevy excludes warmup sets from working-set totals; everything else (normal,
# dropset, failure, and any future set_type Hevy adds) counts, mirroring how
# liftosaur/services.py skips warmup:/target: sections rather than allow-listing.
_EXCLUDED_SET_TYPES = frozenset({"warmup"})

_DATE_FORMATS = (
    "%d %b %Y, %H:%M",
    "%d %b %Y, %H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
)


def _parse_date(raw: str) -> date | None:
    """Parse a Hevy start_time cell into a date, or None if unparseable."""
    cleaned = (raw or "").strip()
    if not cleaned:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(cleaned, fmt).date()
        except ValueError:
            continue
    return None


def _parse_decimal(raw: str) -> Decimal | None:
    cleaned = (raw or "").strip()
    if not cleaned:
        return None
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def _parse_reps(raw: str) -> int | None:
    cleaned = (raw or "").strip()
    if not cleaned:
        return None
    try:
        return int(cleaned)
    except ValueError:
        try:
            # Hevy has been observed emitting reps as "8.0" for some export
            # variants; fall back to float parsing rather than dropping the row.
            return int(float(cleaned))
        except ValueError:
            return None


class HevyImporter:
    """Recognizes and parses a Hevy CSV workout export."""

    source = LiftSource.HEVY_CSV

    def detect(self, header: list[str]) -> bool:
        return set(header) >= REQUIRED_HEADERS

    def parse(self, file_obj) -> list[ParsedSet]:
        """Parse an uploaded Hevy CSV export into its completed working sets.

        Skips warmup sets and rows with a missing/non-numeric weight_lbs or
        reps (Hevy CSVs mix strength sets with cardio rows that use
        distance_km / duration_seconds instead, which this importer doesn't
        score) or an unparseable start_time -- one bad row is logged and
        skipped, it never aborts the whole import. Exercise names are
        resolved through the shared tracker-agnostic resolution chain
        (core.lift_resolution) against Hevy's aliases and the canonical
        Liftosaur lift catalogue before being returned, so callers rarely see
        Hevy's raw naming -- including equipment-qualifier and fuzzy-matching
        fallback stages this importer didn't have before (e.g. a
        "Pendlay Row (Barbell)"-shaped raw name now resolves the same way it
        does for a Strong import).
        """
        text = decode_csv_text(file_obj)
        reader = csv.DictReader(io.StringIO(text))
        resolver = LiftNameResolver(
            build_lift_alias_maps(
                LiftAliasSource.HEVY, Lift.objects.values_list("name", flat=True)
            ),
            source_label="Hevy CSV import",
            logger=logger,
        )

        parsed: list[ParsedSet] = []
        for row in reader:
            set_type = (row.get("set_type") or "").strip().lower()
            if set_type in _EXCLUDED_SET_TYPES:
                continue

            exercise = (row.get("exercise_title") or "").strip()
            if not exercise:
                continue

            weight_lbs = _parse_decimal(row.get("weight_lbs"))
            reps = _parse_reps(row.get("reps"))
            if weight_lbs is None or reps is None:
                # Cardio/distance-only rows (distance_km/duration_seconds
                # instead of weight_lbs/reps) land here -- not an error, just
                # not a set this importer scores.
                continue

            performed_at = _parse_date(row.get("start_time"))
            if performed_at is None:
                # The raw value is workout data from the user's file and is
                # deliberately not logged -- which known format it failed to
                # match is enough to diagnose a new Hevy date variant.
                logger.warning(
                    "Skipping Hevy CSV row: start_time did not match any "
                    "known format %s",
                    _DATE_FORMATS,
                )
                continue

            weight_kg = (weight_lbs * LB_TO_KG).quantize(Decimal("0.01"))
            lift = resolver.resolve(exercise)
            parsed.append(
                ParsedSet(
                    lift=lift,
                    performed_at=performed_at,
                    reps=reps,
                    weight_kg=weight_kg,
                )
            )

        return parsed
