"""Liftosaur CSV export importer (#67 / TASK-313).

Liftosaur's REST API requires a premium subscription (liftosaur.com/doc/api:
"Requires a premium subscription"), so free-tier Liftosaur users have no way
to connect liftosaur.services.sync_user_lifts at all. Liftosaur's CSV export
("Settings -> Export history to CSV file") is free-tier and gives those users
a proof-of-lift path through the same generic workout_imports registry Hevy
uses. One row in the export is one set (warmup or working); a workout's rows
share a Workout DateTime.
"""

import csv
import io
import logging
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from accounts.units import LB_TO_KG
from liftosaur.models import LiftAlias, LiftSource
from workout_imports.importers.base import ParsedSet, decode_csv_text

logger = logging.getLogger(__name__)

# Columns this importer actually reads. Liftosaur's real export carries more
# columns (Program, Day Name, Is AMRAP?, Required RPE/Completed RPE, Log RPE?,
# Ask Weight?, Target Muscles, Synergist Muscles, Notes) but those aren't
# needed to pool a strength set, so they aren't required here. This subset
# doubles as the detection signature: it's distinctive enough that an
# unrelated CSV won't accidentally match it.
REQUIRED_HEADERS = frozenset(
    {
        "Workout DateTime",
        "Exercise",
        "Is Warmup Set?",
        "Completed Reps",
        "Completed Weight Value",
        "Completed Weight Unit",
    }
)

_DATE_FORMATS = (
    "%Y-%m-%dT%H:%M:%S.%f%z",
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d",
)


def _parse_date(raw: str) -> date | None:
    """Parse a Liftosaur ``Workout DateTime`` cell into a date, or None."""
    cleaned = (raw or "").strip()
    if not cleaned:
        return None
    # Liftosaur emits UTC ISO-8601 timestamps suffixed with "Z"
    # (e.g. "2026-03-01T10:00:00.000Z"); strptime's %z doesn't accept "Z"
    # itself, so normalize it to an explicit offset first.
    normalized = cleaned[:-1] + "+0000" if cleaned.endswith("Z") else cleaned
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(normalized, fmt).date()
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
        return None


def _split_exercise_name(raw: str) -> str:
    """Strip a CSV ``Exercise`` cell's equipment suffix, if any.

    Liftosaur renders a non-default-equipment exercise as ``"Name, Equipment"``
    (Exercise_fullName in the Liftosaur app) -- the same convention
    liftosaur.services._parse_exercise_line splits on for API history. Only
    the bare name is looked up against LiftAlias, since the seeded aliases
    key on Liftosaur's exercise names, not its equipment suffixes.
    """
    return raw.split(", ", 1)[0].strip()


class LiftosaurImporter:
    """Recognizes and parses a Liftosaur CSV history export."""

    source = LiftSource.LIFTOSAUR

    def detect(self, header: list[str]) -> bool:
        return set(header) >= REQUIRED_HEADERS

    def parse(self, file_obj) -> list[ParsedSet]:
        """Parse an uploaded Liftosaur CSV export into its completed sets.

        Includes warmup sets (unlike Hevy: Liftosaur's own API-sync path pools
        every completed set, warmup and working alike -- only the *target*
        section of a Liftoscript exercise line is skipped, and the CSV export
        has no equivalent of a target row). Rows with a missing/non-numeric
        Completed Reps or Completed Weight Value, an unrecognized Completed
        Weight Unit, an unparseable Workout DateTime, or a blank Exercise are
        skipped, not fatal.
        """
        text = decode_csv_text(file_obj)
        reader = csv.DictReader(io.StringIO(text))
        alias_map = self._alias_map()

        parsed: list[ParsedSet] = []
        for row in reader:
            exercise = _split_exercise_name(row.get("Exercise") or "")
            if not exercise:
                continue

            reps = _parse_reps(row.get("Completed Reps"))
            weight_value = _parse_decimal(row.get("Completed Weight Value"))
            unit = (row.get("Completed Weight Unit") or "").strip().lower()
            if reps is None or weight_value is None or unit not in ("kg", "lb"):
                continue

            performed_at = _parse_date(row.get("Workout DateTime"))
            if performed_at is None:
                # The raw value is workout data from the user's file and is
                # deliberately not logged -- which known format it failed to
                # match is enough to diagnose a new Liftosaur date variant.
                logger.warning(
                    "Skipping Liftosaur CSV row: Workout DateTime did not "
                    "match any known format %s",
                    _DATE_FORMATS,
                )
                continue

            weight_kg = weight_value * LB_TO_KG if unit == "lb" else weight_value
            weight_kg = weight_kg.quantize(Decimal("0.01"))
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
        """Return ``{from_name.lower(): to_name}`` for every seeded alias.

        Reuses liftosaur.models.LiftAlias -- the same table
        liftosaur.services._alias_map draws from for the API-sync path -- since
        a Liftosaur CSV export uses the same exercise names the API returns.
        """
        return {
            from_name.lower(): to_name
            for from_name, to_name in LiftAlias.objects.values_list(
                "from_name", "to_name"
            )
        }
