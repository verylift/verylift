"""Service layer for FitnessVolt strength standards (TASK-104, doc-1).

Two kinds of function live here:

- ``refresh_cache()`` — the only code path that ever talks to FitnessVolt,
  run out-of-band by the ``refresh_fitnessvolt_cache`` management command.
- Pure cache reads (``get_fitnessvolt_threshold``, ``get_standards_bulk``,
  ``current_snapshot_version``, ``covered_lift_names``) used by scoring and
  the standards chart. These never call FitnessVoltClient — a missing row
  means "no standard for this cell", never a live fallback fetch.

FitnessVolt publishes raw weight-class percentile tables, not tier-labelled
multipliers, so reads resolve a threshold in two pure interpolation steps
(doc-1 §3): interpolate each percentile column linearly between the two
weight classes bracketing the lifter's bodyweight, then resolve the tier's
target percentile against the known columns (exact / linear interpolation /
below-p10 linear extrapolation for Beginner).
"""

import itertools
import logging
import urllib.error
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from core.lift_resolution import LiftNameMaps, build_lift_alias_maps, resolve_lift_name
from core.models import LiftAlias, LiftAliasSource
from fitnessvolt.client import FitnessVoltAPIError, FitnessVoltClient
from fitnessvolt.models import FitnessVoltStandardCache

logger = logging.getLogger(__name__)

POPULATIONS = (
    FitnessVoltStandardCache.Population.VERIFIED,
    FitnessVoltStandardCache.Population.GYM,
)

# FitnessVolt's own published tier definitions — each tier is a fixed
# percentile of the lift's distribution, per their methodology page
# (https://fitnessvolt.com/strength-standards/methodology/). This mapping is
# FitnessVolt's stated definition of its own tiers, not invented here.
TIER_TARGET_PERCENTILE = {
    "Beginner": 5,
    "Novice": 20,
    "Intermediate": 50,
    "Advanced": 80,
    "Elite": 95,
}

# Percentile columns /standards/{lift}?format=table actually returns, mapped
# to their numeric percentile for interpolation.
PERCENTILE_BY_COLUMN = {
    "p10": Decimal("10"),
    "p25": Decimal("25"),
    "p50": Decimal("50"),
    "p75": Decimal("75"),
    "p90": Decimal("90"),
    "p95": Decimal("95"),
    "p99": Decimal("99"),
}

# Model sex value -> API-side sex query param.
API_SEX_BY_MODEL_SEX = {
    FitnessVoltStandardCache.Sex.MALE.value: "male",
    FitnessVoltStandardCache.Sex.FEMALE.value: "female",
}

_TWO_PLACES = Decimal("0.01")

_CLIENT_ERRORS = (FitnessVoltAPIError, urllib.error.URLError, OSError)


def _build_maps() -> LiftNameMaps:
    """Build this call's alias + canonical-catalogue lookup maps in two queries.

    Mirrors liftosaur.services._build_maps / wger.services._build_maps.
    FitnessVolt is a live external API whose published slugs can drift (a
    rename, a punctuation change) independently of when we last reviewed its
    capability doc, so it shares the same six-stage tracker-agnostic chain as
    every other source rather than the single-stage exact lookup this used to
    be — see core.lift_resolution for the chain itself.
    """
    from core.models import Lift

    return build_lift_alias_maps(
        LiftAliasSource.FITNESSVOLT, Lift.objects.values_list("name", flat=True)
    )


def canonical_lift_name(slug: str) -> str | None:
    """Map a FitnessVolt lift slug to our canonical lift name via the shared
    tracker-agnostic resolution chain (core.lift_resolution).

    Returns None for slugs with no known mapping (new/renamed FitnessVolt
    lifts we haven't reviewed yet) rather than passing the slug through
    unchanged — unlike liftosaur's canonical_lift_name(), an
    unrecognized FitnessVolt slug must not silently become a fake "lift name"
    since it would never match anything and its shape (kebab-case slug) would
    look wrong if it leaked into the UI. A fuzzy/reordered match (drift the
    chain resolved through rather than an exact alias/canonical hit) still
    returns the resolved name, but logs a warning naming "fitnessvolt" as the
    source so the drift is visible instead of silent.
    """
    lift, status = resolve_lift_name(slug, _build_maps())
    if status == "unmapped":
        return None
    if status == "reordered":
        logger.warning(
            "fitnessvolt: slug %r only resolved to canonical lift %r by "
            "reordering its equipment qualifier to the front (stage 4); "
            "consider adding an explicit alias or double-checking this "
            "correspondence",
            slug,
            lift,
        )
    elif status == "fuzzy":
        logger.warning(
            "fitnessvolt: slug %r only resolved to canonical lift %r via "
            "separator-insensitive fallback matching (stage 5); consider "
            "adding an explicit alias or double-checking this correspondence",
            slug,
            lift,
        )
    return lift


def slugs_for_lift_name(name: str) -> list[str]:
    """Reverse-map a canonical lift name to its FitnessVolt slug candidates.

    Used by scoring, which speaks canonical lift names (PointEarnEvent.lift,
    LiftHistory.lift) and needs the FitnessVolt-side key to read the cache.
    Returns a list because the two populations slug the same lift differently
    (verified says ``squat``, gym says ``back_squat`` — both map to
    "Back Squat"); the caller tries each candidate against the pinned
    snapshot's rows. An empty list means no alias maps to the name — the
    same "no standard for this cell" outcome as an uncovered lift.

    Deliberately stays an explicit-alias-only reverse lookup (not run through
    the fuzzy chain): the chain resolves a raw slug forward to a canonical
    name, and has no well-defined reverse direction to invert.
    """
    return list(
        LiftAlias.objects.filter(
            source=LiftAliasSource.FITNESSVOLT, to_name=name
        ).values_list("from_name", flat=True)
    )


def current_snapshot_version(population: str) -> str | None:
    """Return the latest source_snapshot_version with cache rows for a population.

    None means the population has never been warmed — the creation picker
    must not offer it.
    """
    row = (
        FitnessVoltStandardCache.objects.filter(population=population)
        .order_by("-fetched_at")
        .first()
    )
    return row.source_snapshot_version if row else None


def standards_method_available() -> bool:
    """Whether the goal-setup wizard should offer the "strength standards" method.

    Requires both the FITNESSVOLT_ENABLED rollout gate and at least one
    population with a warmed snapshot -- offering the method without either
    leads to a population picker with nothing in it and no way to finish.
    """
    return settings.FITNESSVOLT_ENABLED and any(
        current_snapshot_version(population) for population in POPULATIONS
    )


def _parse_weight_class_kg(value) -> Decimal | None:
    """Normalize a response's ``weight_class`` value to kg, or None to skip it.

    ``verified`` returns bare numbers (already kg since unit=kg is
    requested); ``gym`` returns strings like ``"47kg"``, the open-ended
    ``"84+kg"``, and ``"all"`` (an all-bodyweights aggregate). The "+" class
    gets a small epsilon added to its numeric anchor so it sorts directly
    above the same-numbered closed class and heavier bodyweights clamp to
    it (the verbatim ``weight_class_label`` keeps the display string).
    ``"all"`` — and anything else unparseable — returns None: an aggregate
    across bodyweights is not a weight class and would corrupt the
    by-weight interpolation.
    """
    if isinstance(value, int | float | Decimal):
        return Decimal(str(value))
    if isinstance(value, str):
        text = value.strip().lower().removesuffix("kg").strip()
        is_open_ended = text.endswith("+")
        if is_open_ended:
            text = text[:-1]
        try:
            parsed = Decimal(text)
        except InvalidOperation:
            return None
        return parsed + Decimal("0.01") if is_open_ended else parsed
    return None


def _row_percentiles(row: FitnessVoltStandardCache) -> dict[str, Decimal]:
    """Parse a cache row's verbatim percentile JSON into Decimals.

    Unknown columns and unparseable values are dropped with a warning so bad
    data surfaces as an unavailable cell rather than a wrong threshold.
    """
    parsed: dict[str, Decimal] = {}
    for column, value in row.percentiles.items():
        if column not in PERCENTILE_BY_COLUMN or value is None:
            continue
        try:
            parsed[column] = Decimal(str(value))
        except InvalidOperation:
            logger.warning(
                "Unparseable FitnessVolt percentile %s=%r for %s / %s / %s",
                column,
                value,
                row.lift_slug,
                row.sex,
                row.weight_class_label,
            )
    return parsed


def _percentiles_at_bodyweight(
    rows: list[FitnessVoltStandardCache], bodyweight_kg: Decimal
) -> dict[str, Decimal]:
    """Interpolate each percentile column linearly by weight class (doc-1 §3).

    ``rows`` must be non-empty and sorted ascending by weight_class_kg. A
    bodyweight outside the observed weight-class range uses the single
    nearest row as-is — no extrapolation across weight classes, since
    FitnessVolt's own table is already sparse/bucketed at the extremes.
    """
    if bodyweight_kg <= rows[0].weight_class_kg:
        return _row_percentiles(rows[0])
    if bodyweight_kg >= rows[-1].weight_class_kg:
        return _row_percentiles(rows[-1])

    lower = upper = rows[0]
    for lower, upper in itertools.pairwise(rows):
        if lower.weight_class_kg <= bodyweight_kg <= upper.weight_class_kg:
            break
    lower_percentiles = _row_percentiles(lower)
    upper_percentiles = _row_percentiles(upper)
    span = upper.weight_class_kg - lower.weight_class_kg
    offset = bodyweight_kg - lower.weight_class_kg
    return {
        column: value + (upper_percentiles[column] - value) * offset / span
        for column, value in lower_percentiles.items()
        if column in upper_percentiles
    }


def _threshold_at_percentile(
    percentiles: dict[str, Decimal],
    tier_label: str,
    *,
    lift_slug: str,
) -> Decimal | None:
    """Resolve one tier's target percentile against the known columns (doc-1 §3).

    Exact column match (Intermediate=p50, Elite=p95) is returned as-is; a
    target between two known columns (Novice=20th, Advanced=80th) is linearly
    interpolated; Beginner (5th) sits below the lowest column (p10) and is
    linearly extrapolated from the two lowest columns' slope. Returns None —
    the existing "no standard for this cell" outcome — when the columns
    present can't resolve the target.
    """
    target = Decimal(TIER_TARGET_PERCENTILE[tier_label])
    known = sorted(
        (PERCENTILE_BY_COLUMN[column], value) for column, value in percentiles.items()
    )
    for percentile, value in known:
        if percentile == target:
            return value.quantize(_TWO_PLACES)
    if len(known) < 2:
        logger.warning(
            "Cannot resolve FitnessVolt tier %s for lift %s: only %d usable "
            "percentile column(s)",
            tier_label,
            lift_slug,
            len(known),
        )
        return None
    if target > known[-1][0]:
        logger.warning(
            "Cannot resolve FitnessVolt tier %s (%sth percentile) for lift %s: "
            "above the highest cached column (p%s)",
            tier_label,
            target,
            lift_slug,
            known[-1][0],
        )
        return None
    if target < known[0][0]:
        # Beginner (5th) is below p10: extrapolate using the two lowest
        # columns' slope (p10 -> p25 with a full table).
        (low_pct, low_val), (next_pct, next_val) = known[0], known[1]
        logger.debug(
            "Extrapolating FitnessVolt tier %s (%sth percentile) for lift %s "
            "below the lowest cached column p%s using the p%s->p%s slope",
            tier_label,
            target,
            lift_slug,
            low_pct,
            low_pct,
            next_pct,
        )
        value = low_val + (next_val - low_val) * (target - low_pct) / (
            next_pct - low_pct
        )
    else:
        value = None
        for (low_pct, low_val), (high_pct, high_val) in itertools.pairwise(known):
            if low_pct < target < high_pct:
                value = low_val + (high_val - low_val) * (target - low_pct) / (
                    high_pct - low_pct
                )
                break
    if value is None or value <= 0:
        logger.warning(
            "FitnessVolt tier %s for lift %s resolved to %s; treating as unavailable",
            tier_label,
            lift_slug,
            value,
        )
        return None
    return value.quantize(_TWO_PLACES)


def get_fitnessvolt_threshold(
    population: str,
    snapshot_version: str,
    lift_slug: str,
    sex: str,
    tier_label: str,
    bodyweight_kg: Decimal,
) -> Decimal | None:
    """Resolve one 1RM threshold (kg) from the pinned snapshot's cached tables.

    Pure DB read plus pure interpolation — never a live FitnessVolt call.
    Returns None when the cell has no cached standard (a lift the population
    doesn't cover, an omitted small cohort, or an unknown tier), which
    callers treat exactly like the built-in "no matching standard" no-op
    path.
    """
    if tier_label not in TIER_TARGET_PERCENTILE:
        logger.warning(
            "Unknown FitnessVolt tier label %r requested for lift %s",
            tier_label,
            lift_slug,
        )
        return None
    rows = list(
        FitnessVoltStandardCache.objects.filter(
            population=population,
            source_snapshot_version=snapshot_version,
            lift_slug=lift_slug,
            sex=sex,
        ).order_by("weight_class_kg")
    )
    if not rows:
        return None
    percentiles = _percentiles_at_bodyweight(rows, Decimal(bodyweight_kg))
    return _threshold_at_percentile(percentiles, tier_label, lift_slug=lift_slug)


def get_standards_bulk(
    population: str,
    snapshot_version: str,
    sex: str,
    bodyweight_kg: Decimal | None,
) -> list[dict]:
    """Interpolate every covered lift × all five tiers at one bodyweight.

    Used by the standards chart, which needs all lift × tier cells at once.
    Each cell dict carries the canonical lift name (``lift``), the raw
    ``lift_slug``, the ``tier_label`` (FitnessVolt's fixed 5-tier
    vocabulary), and ``multiplier`` — the threshold expressed as an
    effective bodyweight multiplier (threshold / bodyweight) so consumers
    keep the exact shape the built-in multiplier rows give them. Cells whose
    slug has no alias are skipped with a log line; a tier whose percentile
    target can't be resolved is omitted (rendered "unavailable"). When
    ``bodyweight_kg`` is None every covered cell is emitted with
    ``multiplier`` None so the tier vocabulary still renders. Sorted by
    (lift, tier percentile), i.e. ascending threshold within a lift.
    """
    maps = _build_maps()
    rows = FitnessVoltStandardCache.objects.filter(
        population=population,
        source_snapshot_version=snapshot_version,
        sex=sex,
    ).order_by("lift_slug", "weight_class_kg")

    cells = []
    for lift_slug, group in itertools.groupby(rows, key=lambda r: r.lift_slug):
        lift, status = resolve_lift_name(lift_slug, maps)
        if status == "unmapped":
            lift = None
        if lift is None:
            logger.warning(
                "Skipping cached FitnessVolt rows with unmapped slug %r", lift_slug
            )
            continue
        lift_rows = list(group)
        percentiles = (
            _percentiles_at_bodyweight(lift_rows, Decimal(bodyweight_kg))
            if bodyweight_kg is not None
            else None
        )
        for tier_label in TIER_TARGET_PERCENTILE:
            multiplier = None
            if percentiles is not None:
                threshold = _threshold_at_percentile(
                    percentiles, tier_label, lift_slug=lift_slug
                )
                if threshold is None:
                    continue
                multiplier = threshold / Decimal(bodyweight_kg)
            cells.append(
                {
                    "lift": lift,
                    "lift_slug": lift_slug,
                    "tier_label": tier_label,
                    "multiplier": multiplier,
                }
            )
    cells.sort(key=lambda c: (c["lift"], TIER_TARGET_PERCENTILE[c["tier_label"]]))
    return cells


def covered_lift_names(population: str, snapshot_version: str) -> set[str]:
    """Canonical lift names a population's pinned snapshot covers (either sex)."""
    slugs = set(
        FitnessVoltStandardCache.objects.filter(
            population=population,
            source_snapshot_version=snapshot_version,
        ).values_list("lift_slug", flat=True)
    )
    maps = _build_maps()
    names = set()
    for slug in slugs:
        lift, status = resolve_lift_name(slug, maps)
        if status != "unmapped":
            names.add(lift)
    return names


def refresh_cache() -> dict[str, str]:
    """Full pull of both populations into the versioned cache, idempotently.

    Fetches the capability doc, then ``/standards/{lift}?sex=...&source=...
    &format=table&unit=kg`` for every (lift, sex) pair each population lists
    under ``sources.<population>.lifts``, inserting one row per
    ``weight_classes`` entry under the response's verbatim ``data_version``.
    A population whose current ``data_version`` is already cached is a no-op.
    When a genuinely new snapshot is inserted for a population, stale old
    snapshots for that population (unreferenced by any challenge and past
    the retention window) are swept in the same pass — garbage collection
    rides along with a successful new-snapshot pull, never a separate job.

    Any FitnessVolt error (including 429 rate limits) aborts that
    population's pull with a warning and leaves its existing snapshot
    current; nothing is retried inline.

    Returns a summary mapping population -> "noop" | "inserted:<version>".
    """
    summary: dict[str, str] = {}
    client = FitnessVoltClient()

    try:
        capabilities = client.get_capabilities()
    except _CLIENT_ERRORS:
        logger.exception(
            "FitnessVolt capability fetch failed; keeping existing snapshots current"
        )
        return summary

    data_version = capabilities.get("data_version")
    if not data_version:
        logger.error(
            "FitnessVolt capability doc carried no data_version; aborting refresh"
        )
        return summary

    sources_doc = capabilities.get("sources", {})
    for population in POPULATIONS:
        lift_entries = sources_doc.get(population.value, {}).get("lifts") or []
        lift_slugs = [
            entry["lift"]
            for entry in lift_entries
            if isinstance(entry, dict) and entry.get("lift")
        ]
        if not lift_slugs:
            logger.warning(
                "FitnessVolt capability doc lists no lifts for population %s; skipping",
                population,
            )
            continue

        if FitnessVoltStandardCache.objects.filter(
            population=population, source_snapshot_version=data_version
        ).exists():
            logger.info(
                "FitnessVolt snapshot %s already cached for population %s; no-op",
                data_version,
                population,
            )
            summary[population.value] = "noop"
            continue

        try:
            inserted = _pull_population_snapshot(
                client, population, lift_slugs, data_version
            )
        except _CLIENT_ERRORS:
            logger.warning(
                "FitnessVolt snapshot refresh failed for population %s, "
                "keeping snapshot %s current",
                population,
                current_snapshot_version(population),
                exc_info=True,
            )
            continue

        if inserted:
            _sweep_stale_snapshots(population, keep_version=data_version)
            summary[population.value] = f"inserted:{data_version}"
        else:
            logger.warning(
                "FitnessVolt refresh inserted no rows for population %s "
                "(all slugs unmapped or empty responses)",
                population,
            )

    return summary


@transaction.atomic
def _pull_population_snapshot(
    client: FitnessVoltClient,
    population: str,
    lift_slugs: list[str],
    data_version: str,
) -> int:
    """Fetch and insert one population's full grid for one snapshot version.

    One ``/standards/{lift}`` call per (lift, sex) pair — ``sex`` and
    ``source`` are both required query params and each call returns only one
    sex's weight-class table. Atomic: a mid-pull failure rolls the partial
    snapshot back so the older snapshot stays current (a half-inserted
    snapshot must never win current_snapshot_version()). Unmapped slugs are
    skipped with a warning, per doc-1 §5.
    """
    inserted = 0
    fetched_at = timezone.now()
    for slug in lift_slugs:
        if canonical_lift_name(slug) is None:
            logger.warning(
                "Skipping FitnessVolt lift slug %r for population %s: "
                "no alias or canonical lift match",
                slug,
                population,
            )
            continue

        for model_sex, api_sex in API_SEX_BY_MODEL_SEX.items():
            payload = client.get_lift_standards(slug, population, api_sex)
            payload_version = payload.get("data_version", data_version)
            if payload_version != data_version:
                logger.warning(
                    "FitnessVolt /standards/%s reported data_version %s while the "
                    "capability doc reported %s; storing the per-lift value verbatim",
                    slug,
                    payload_version,
                    data_version,
                )

            for entry in payload.get("weight_classes", []):
                weight_class_kg = _parse_weight_class_kg(entry.get("weight_class"))
                if weight_class_kg is None:
                    # Expected for gym's "all" aggregate row on every pull;
                    # it is not a weight class and must not join the
                    # by-weight interpolation table.
                    logger.debug(
                        "Skipping non-weight-class entry %r for %s/%s (%s)",
                        entry.get("weight_class"),
                        population,
                        slug,
                        api_sex,
                    )
                    continue
                _, created = FitnessVoltStandardCache.objects.get_or_create(
                    population=population,
                    lift_slug=slug,
                    sex=model_sex,
                    weight_class_kg=weight_class_kg,
                    source_snapshot_version=payload_version,
                    defaults={
                        "weight_class_label": entry.get("weight_class_label", ""),
                        "percentiles": entry["percentiles"],
                        "sample_size": entry.get("sample_size", 0),
                        "fetched_at": fetched_at,
                    },
                )
                if created:
                    inserted += 1

    logger.info(
        "FitnessVolt refresh inserted %s rows for population %s snapshot %s",
        inserted,
        population,
        data_version,
    )
    return inserted


def _sweep_stale_snapshots(population: str, keep_version: str) -> None:
    """Delete unreferenced, out-of-retention old snapshots for a population.

    A snapshot is swept only when (a) it is not the just-inserted current
    version, (b) no participant's CustomGoal (TASK-248 — the challenge no
    longer prescribes a standard, so this is a per-participant provenance
    record, not a Challenge field) still pins it via ``source_detail``, and
    (c) its rows were fetched more than FITNESSVOLT_SNAPSHOT_RETENTION_MONTHS
    ago.
    """
    from challenges.models import CustomGoal

    cutoff = timezone.now() - timedelta(
        days=30 * settings.FITNESSVOLT_SNAPSHOT_RETENTION_MONTHS
    )
    referenced = set(
        CustomGoal.objects.filter(source_detail__population=population).values_list(
            "source_detail__snapshot_version", flat=True
        )
    )
    referenced.discard(None)
    candidates = (
        FitnessVoltStandardCache.objects.filter(population=population)
        .exclude(source_snapshot_version=keep_version)
        .values("source_snapshot_version")
        .annotate(newest_fetch=Max("fetched_at"))
    )
    for candidate in candidates:
        version = candidate["source_snapshot_version"]
        if version in referenced or candidate["newest_fetch"] >= cutoff:
            continue
        deleted, _ = FitnessVoltStandardCache.objects.filter(
            population=population, source_snapshot_version=version
        ).delete()
        logger.info(
            "Swept %s stale FitnessVolt cache rows for population %s snapshot %s",
            deleted,
            population,
            version,
        )
