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
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from accounts.units import LB_TO_KG
from liftosaur.models import Lift, LiftSource
from workout_imports.importers.base import ParsedSet, decode_csv_text
from workout_imports.models import StrongLiftAlias

logger = logging.getLogger(__name__)

# Matches a trailing "(Equipment)" qualifier Strong appends to its exercise
# names, e.g. "Pendlay Row (Barbell)" -> base "Pendlay Row", qualifier
# "Barbell". Only a single, non-nested parenthetical at the very end counts;
# this deliberately isn't used to strip parens anywhere else in a name.
_TRAILING_QUALIFIER_RE = re.compile(r"^(?P<base>.+?)\s*\((?P<qualifier>[^()]+)\)$")

# Equipment qualifiers safe to strip and match against the bare canonical
# lift name. Every existing StrongLiftAlias entry that pairs a raw
# "X (Barbell)" name with a canonical name maps it to the unqualified form
# (e.g. "Bench Press (Barbell)" -> "Bench Press"), so that convention is
# trusted algorithmically here too. No other equipment is treated this way:
# the canonical catalogue distinguishes some equipment variants under
# different names (Bench Press vs Chest Press, Overhead Press vs Shoulder
# Press), so blindly stripping "(Dumbbell)"/"(Machine)"/etc. and matching the
# bare name risks silently attributing a set to the wrong variant -- an
# unmapped/verbatim import is the safer failure mode than a wrong one.
_SAFE_TO_STRIP_QUALIFIERS = frozenset({"barbell"})

_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def _normalize_lift_name(name: str) -> str:
    """Fold a lift name for case/punctuation-insensitive comparison.

    Lowercases and collapses any run of non-alphanumeric characters (spaces,
    hyphens, apostrophes) to a single space, so "Chin Up", "Chin-up", and
    "chin  up" all normalize identically. Deliberately does not drop
    parenthesized content -- normalizing "Bench Press (Dumbbell)" keeps the
    "dumbbell" token, so it never accidentally collapses onto the bare
    "Bench Press" canonical name.
    """
    return _NON_ALNUM_RE.sub(" ", name.lower()).strip()


def _normalize_lift_name_strict(name: str) -> str:
    """Fold a lift name for the separator-free catch-all match (stage 4).

    Lowercases and removes non-alphanumerics entirely rather than collapsing
    them to a space, so "Chinup", "Chin-up", and "Chin Up" all normalize to
    "chinup". Strictly looser than ``_normalize_lift_name`` -- every name it
    equates, that function already equates too -- so it's only ever tried
    after that one has already failed. Still operates on the whole raw
    string with no equipment-qualifier stripping, so "Bench Press
    (Dumbbell)" normalizes to "benchpressdumbbell", not "benchpress" --
    the equipment-collapse guard holds here too.
    """
    return _NON_ALNUM_RE.sub("", name.lower())


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
        value = Decimal(cleaned)
    except InvalidOperation:
        return None
    # Decimal("nan")/Decimal("Infinity") parse without raising InvalidOperation
    # here, but a non-finite weight blows up quantize() downstream (Infinity)
    # or silently produces a NaN weight_kg (nan) -- reject both as unparseable
    # like any other malformed cell, rather than letting either reach the
    # arithmetic below.
    if not value.is_finite():
        return None
    return value


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
            # dropping the row. float() itself never raises on something
            # like "1e400" (it returns inf), so int() on that result raises
            # OverflowError, not ValueError -- both must be caught here or
            # a single malformed Reps cell crashes the whole import.
            return int(float(cleaned))
        except (ValueError, OverflowError):
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
        Exercise names are resolved against StrongLiftAlias and the canonical
        Liftosaur lift catalogue before being returned, so callers rarely see
        Strong's raw naming. A name that cannot be resolved is imported
        verbatim rather than skipped -- an unmapped lift name is inert
        (doesn't pool or count toward goals) but doesn't lose the user's
        logged sets outright, and is surfaced via a warning log instead.
        """
        text = decode_csv_text(file_obj)
        reader = csv.DictReader(io.StringIO(text))
        alias_map = self._alias_map()
        canonical_map = self._canonical_map()
        alias_map_strict = self._alias_map_strict()
        canonical_map_strict = self._canonical_map_strict()
        warned_exercises: set[str] = set()
        fuzzy_matched_exercises: set[str] = set()

        parsed: list[ParsedSet] = []
        for row in reader:
            exercise = (row.get("Exercise Name") or "").strip()
            if not exercise:
                continue

            weight_lbs = _parse_decimal(row.get("Weight"))
            reps = _parse_reps(row.get("Reps"))
            if weight_lbs is None or reps is None or reps <= 0:
                # Cardio/rest-day rows land here -- either Weight/Reps are
                # genuinely blank in favor of Distance/Seconds, or (observed
                # in a real export) explicitly "0" rather than blank, e.g. a
                # Swimming entry logged as Weight=0, Reps=0, Distance=1.0.
                # Zero reps is never a completed set regardless of which
                # form the row takes, so it's checked directly rather than
                # relying on the cell being unparseable. weight_lbs == 0 is
                # left alone -- a legitimate bodyweight-only set (e.g. Push
                # Up) reports zero added weight with real reps.
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
            lift, status = self._resolve_lift(
                exercise,
                alias_map,
                canonical_map,
                alias_map_strict,
                canonical_map_strict,
            )
            if status == "fuzzy" and exercise not in fuzzy_matched_exercises:
                fuzzy_matched_exercises.add(exercise)
                logger.warning(
                    "Strong CSV import: exercise %r only resolved to "
                    "canonical lift %r via separator-insensitive fallback "
                    "matching (stage 4); consider adding an explicit "
                    "StrongLiftAlias or double-checking this correspondence",
                    exercise,
                    lift,
                )
            elif status == "unmapped" and exercise not in warned_exercises:
                warned_exercises.add(exercise)
                logger.warning(
                    "Strong CSV import: exercise %r did not match any known "
                    "alias or canonical lift; importing sets under this name "
                    "verbatim",
                    exercise,
                )
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
    def _resolve_lift(
        exercise: str,
        alias_map: dict[str, str],
        canonical_map: dict[str, str],
        alias_map_strict: dict[str, str],
        canonical_map_strict: dict[str, str],
    ) -> tuple[str, str]:
        """Resolve a raw Strong exercise name to a canonical lift name.

        Tries, in order:
        1. An explicit ``StrongLiftAlias`` entry (case-insensitive) --
           always wins when present, since it's a human-confirmed mapping.
        2. If the name ends in a "(Barbell)" qualifier, strip it and retry
           both the alias map and the canonical catalogue against the base
           name -- this is what resolves "Pendlay Row (Barbell)" to the
           seeded "Pendlay Row" lift without needing a dedicated alias row.
           Only "(Barbell)" is stripped this way; see
           ``_SAFE_TO_STRIP_QUALIFIERS`` for why other equipment isn't.
        3. A case-insensitive, punctuation-tolerant match of the whole raw
           name against the canonical catalogue (catches "Chin Up" vs
           "Chin-up").
        4. A separator-free catch-all: lowercase with every non-alphanumeric
           character removed entirely (not just collapsed to a space),
           matched against both the canonical catalogue and the alias-map
           keys under the same folding (catches "Chinup", "TBar Row").
           Strictly looser than stage 3, so it only ever fires once that one
           has already missed. Still operates on the whole raw name --
           equipment qualifiers are never stripped here, so the
           dumbbell/machine collapse guard from stage 2 still holds.

        Returns ``(lift_name, status)``, where ``status`` is ``"matched"``
        for stages 1-3, ``"fuzzy"`` when only stage 4 found it (a hit the
        caller should surface -- loose matching stood in for a proper alias
        or canonical entry), or ``"unmapped"`` when nothing did, in which
        case ``lift_name`` is the original ``exercise`` string, unchanged.
        """
        hit = alias_map.get(exercise.lower())
        if hit:
            return hit, "matched"

        qualifier_match = _TRAILING_QUALIFIER_RE.match(exercise)
        if qualifier_match:
            qualifier = qualifier_match.group("qualifier").strip().lower()
            if qualifier in _SAFE_TO_STRIP_QUALIFIERS:
                base = qualifier_match.group("base").strip()
                hit = alias_map.get(base.lower())
                if hit:
                    return hit, "matched"
                hit = canonical_map.get(_normalize_lift_name(base))
                if hit:
                    return hit, "matched"

        hit = canonical_map.get(_normalize_lift_name(exercise))
        if hit:
            return hit, "matched"

        strict_key = _normalize_lift_name_strict(exercise)
        hit = canonical_map_strict.get(strict_key)
        if hit:
            return hit, "fuzzy"
        hit = alias_map_strict.get(strict_key)
        if hit:
            return hit, "fuzzy"

        return exercise, "unmapped"

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

    @staticmethod
    def _canonical_map() -> dict[str, str]:
        """Return ``{normalized_name: name}`` for every seeded canonical lift.

        One query for the whole file, same reasoning as ``_alias_map``.
        """
        return {
            _normalize_lift_name(name): name
            for name in Lift.objects.values_list("name", flat=True)
        }

    @staticmethod
    def _alias_map_strict() -> dict[str, str]:
        """Return ``{strict_normalized(from_name): to_name}`` for stage 4.

        Same seeded StrongLiftAlias data as ``_alias_map``, just keyed under
        the separator-free fold instead of a plain ``.lower()`` -- stage 4
        needs this to catch a Strong export naming an exercise with no
        separators at all against an alias whose ``from_name`` has them (or
        vice versa).
        """
        return {
            _normalize_lift_name_strict(from_name): to_name
            for from_name, to_name in StrongLiftAlias.objects.values_list(
                "from_name", "to_name"
            )
        }

    @staticmethod
    def _canonical_map_strict() -> dict[str, str]:
        """Return ``{strict_normalized_name: name}`` for stage 4.

        Same seeded Lift data as ``_canonical_map``, just keyed under the
        separator-free fold. See ``_normalize_lift_name_strict`` for why
        this is safe against the seeded catalogue today, and the guard test
        in test_seed_strong_lift_aliases.py that keeps it that way.
        """
        return {
            _normalize_lift_name_strict(name): name
            for name in Lift.objects.values_list("name", flat=True)
        }
