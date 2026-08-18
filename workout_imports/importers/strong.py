"""Strong CSV export importer (#10).

Strong's CSV export is free-tier and column-for-column the de-facto standard
other tracker apps (including Hevy) import natively. One row is one
completed set; a workout's sets share a Date. Verified against a real
user-exported sample (github.com/AlexandrosKyriakakis/StrongAppAnalytics,
Data/strong.csv) and cross-checked against an independent open-source
Strong CSV adapter's documented column list, both agreeing on:
Date, Workout Name, Duration, Exercise Name, Set Order, Weight, Reps,
Distance, Seconds, Notes, Workout Notes, RPE.

Known limitation: unlike Hevy's export, Strong's CSV carries no weight-unit
column -- the Weight cell's unit matches whatever the exporting user had set
in the Strong app and isn't recorded in the file itself. This importer
assumes lbs (Strong's common default, and consistent with how this codebase
already treats unit-less imperial weights in the Hevy importer); a kg-locale
Strong user's weights will be silently over-converted. There's no reliable
per-file signal to disambiguate, so this is a judgment call rather than a
detectable case -- flagged here for a future fix if it proves wrong in
practice (e.g. a per-upload unit selector).
"""

import csv
import io
import logging
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from accounts.units import LB_TO_KG
from liftosaur.models import LiftSource
from workout_imports.importers.base import ParsedSet, decode_csv_text
from workout_imports.models import StrongLiftAlias

logger = logging.getLogger(__name__)

# Columns this importer actually reads. Strong's real export also carries
# Duration, Distance, Seconds, Notes, Workout Notes, and RPE, but those
# aren't needed to pool a strength set, so they aren't required here. This
# set doubles as the detection signature: it's distinctive enough that an
# unrelated CSV won't accidentally match it.
REQUIRED_HEADERS = frozenset(
    {"Date", "Workout Name", "Exercise Name", "Set Order", "Weight", "Reps"}
)

_DATE_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d",
)


def _parse_date(raw: str) -> date | None:
    """Parse a Strong Date cell into a date, or None if unparseable."""
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
            # Strong has been observed emitting reps as "8.0" for some
            # export variants; fall back to float parsing rather than
            # dropping the row.
            return int(float(cleaned))
        except ValueError:
            return None


class StrongImporter:
    """Recognizes and parses a Strong CSV workout export."""

    source = LiftSource.STRONG

    def detect(self, header: list[str]) -> bool:
        return set(header) >= REQUIRED_HEADERS

    def parse(self, file_obj) -> list[ParsedSet]:
        """Parse an uploaded Strong CSV export into its completed working sets.

        Skips rows with a missing/non-numeric Weight or Reps (Strong CSVs mix
        strength sets with cardio/rest-day rows that leave Weight and Reps
        blank in favor of Distance/Seconds, or leave the whole row blank on a
        rest day, which this importer doesn't score) or an unparseable Date --
        one bad row is logged and skipped, it never aborts the whole import.
        Exercise names are resolved against StrongLiftAlias before being
        returned, so callers never see Strong's raw naming.
        """
        text = decode_csv_text(file_obj)
        reader = csv.DictReader(io.StringIO(text))
        alias_map = self._alias_map()

        parsed: list[ParsedSet] = []
        for row in reader:
            exercise = (row.get("Exercise Name") or "").strip()
            if not exercise:
                continue

            weight_lbs = _parse_decimal(row.get("Weight"))
            reps = _parse_reps(row.get("Reps"))
            if weight_lbs is None or reps is None:
                # Cardio/rest-day rows (Distance/Seconds instead of
                # Weight/Reps, or entirely blank) land here -- not an error,
                # just not a set this importer scores.
                continue

            performed_at = _parse_date(row.get("Date"))
            if performed_at is None:
                # The raw value is workout data from the user's file and is
                # deliberately not logged -- which known format it failed to
                # match is enough to diagnose a new Strong date variant.
                logger.warning(
                    "Skipping Strong CSV row: Date did not match any known format %s",
                    _DATE_FORMATS,
                )
                continue

            weight_kg = (weight_lbs * LB_TO_KG).quantize(Decimal("0.01"))
            lift = alias_map.get(exercise.lower(), exercise)
            parsed.append(
                ParsedSet(
                    lift=lift,
                    performed_at=performed_at,
                    reps=reps,
                    weight_kg=weight_kg,
                )
            )

        return parsed

    @staticmethod
    def _alias_map() -> dict[str, str]:
        """Return ``{from_name.lower(): to_name}`` for every seeded Strong alias.

        Mirrors liftosaur.services._alias_map: one query for the whole file
        instead of one StrongLiftAlias SELECT per row.
        """
        return {
            from_name.lower(): to_name
            for from_name, to_name in StrongLiftAlias.objects.values_list(
                "from_name", "to_name"
            )
        }
