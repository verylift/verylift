"""Scoring calculation domain logic.

Pure calculation functions, with one exception: is_bodyweight_added_lift reads
the seeded Lift reference table (liftosaur.models.Lift) so the
bodyweight-added quality is admin-editable data, not code.

Every challenge authors flat, static rep-max targets (TASK-248 plan §3): there
is no bodyweight-relative standard anywhere in scoring, no bodyweight series,
and no anchoring. For bodyweight-added lifts (Pull-up/Chin-up/Dip) the target
IS the added weight and the recorded LiftHistory weight IS the added weight —
the comparison is a direct ``performed >= target``, never a total-load
reconstruction. The one exception is assisted-machine equipment
(ASSISTED_EQUIPMENT): its recorded weight is net total load, not added
weight, and is incomparable to an added-weight target — those sets are
excluded from scoring on bodyweight-added lifts entirely (see
is_assisted_equipment and scoring.services.process_scored_set).
"""

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal


def is_bodyweight_added_lift(lift: str) -> bool:
    """True when the lift's strength standard is total load relative to bodyweight.

    For these lifts the multiplier × bodyweight threshold already includes the
    lifter's own weight, so the stored weight is the total load; the meaningful
    number to show a lifter is the added weight = total load − bodyweight.
    Backed by the Lift reference table seeded via seed_liftosaur_lifts
    (deferred import keeps the rest of this module importable without Django).
    """
    from liftosaur.models import Lift

    return Lift.objects.filter(name=lift, is_bodyweight_added=True).exists()


# Equipment names (as they appear after the comma in a Liftosaur history line,
# compared case-insensitively) whose recorded weight for a bodyweight-added
# lift is already the NET TOTAL load. Per the Liftoscript docs' assisted
# pull-up setup ("Is assisting?" + "Bodyweight for Bar"), the completed-set
# weight is stamped as bodyweight − assistance at workout start, so adding
# bodyweight again would double-count it.
ASSISTED_EQUIPMENT = frozenset({"leverage machine"})


def is_assisted_equipment(equipment: str) -> bool:
    """True when the equipment reports net total load rather than added weight."""
    return equipment.strip().lower() in ASSISTED_EQUIPMENT


def format_added_weight(added_weight: Decimal) -> str:
    """Format an added weight (already in display units) relative to bodyweight.

    Zero added weight (bodyweight only) is "BW"; positive weight is prefixed
    with "+"; negative (band-assisted) keeps its "-" sign. No unit is appended
    here — the caller adds it for non-"BW" values.
    """
    if added_weight == 0:
        return "BW"
    normalised = added_weight.normalize()
    if normalised == normalised.to_integral_value():
        normalised = normalised.quantize(Decimal("1"))
    sign = "+" if added_weight > 0 else ""
    return f"{sign}{normalised}"


def points_for_rep_count(reps: int) -> int:
    """Return points earned for a rep-max achievement.

    10 points for a 1RM, 1 point for a 10RM, linear in between.
    """
    if reps < 1 or reps > 10:
        return 0
    return 11 - reps


def satisfies_threshold(
    performed_reps: int,
    performed_weight: float,
    threshold_reps: int,
    threshold_weight: float,
) -> bool:
    """True if a performed set satisfies a strength standard threshold.

    The comparison is exact: ``performed_weight >= threshold_weight`` and
    ``performed_reps >= threshold_reps``. Over-performance is compatible —
    higher weight or more reps both satisfy. There is no fuzz band: every
    challenge's targets are static, entered-once weight values, so there is no
    bodyweight drift to absorb.
    """
    return performed_weight >= threshold_weight and performed_reps >= threshold_reps


def threshold_for_reps(one_rm_threshold: Decimal, reps: int) -> Decimal:
    """Return the minimum weight required to satisfy the rep-max threshold.

    Uses the Epley formula: one_rm_threshold / (1 + reps / 30).

    Special case: reps=1 returns one_rm_threshold exactly (the formula gives
    ~96.8% for reps=1 which is wrong by product decision).
    """
    if reps == 1:
        return one_rm_threshold
    return one_rm_threshold / (1 + Decimal(reps) / Decimal(30))


def estimated_one_rm(weight: Decimal, reps: int) -> Decimal:
    """Return the estimated 1RM for a set of ``reps`` at ``weight`` (Epley).

    The forward direction of :func:`threshold_for_reps`: ``weight * (1 + reps
    / 30)``, with the same ``reps == 1`` identity special case (weight
    unchanged). Used by the goal-setup "suggested from history" method to turn
    a lifter's recorded sets into a suggested rep-max ladder.

    For bodyweight-added lifts (Pull-up/Chin-up/Dip) callers must pass TOTAL
    load (``entered_bodyweight + added_weight``), never the raw recorded added
    weight — ``estimated_one_rm(0, 8)`` is exactly ``0``, the degenerate case
    that would otherwise materialise an all-zero suggested ladder for an
    unweighted bodyweight lift.
    """
    if reps == 1:
        return weight
    return (weight * (1 + Decimal(reps) / Decimal(30))).quantize(Decimal("0.01"))


@dataclass(frozen=True)
class RepMaxThreshold:
    reps: int
    weight: Decimal


@dataclass(frozen=True)
class TierThresholds:
    tier: str
    multiplier: Decimal
    one_rm_threshold: Decimal
    rep_maxes: tuple[RepMaxThreshold, ...]


def tier_thresholds(
    tier: str, multiplier: Decimal, bodyweight_kg: Decimal
) -> TierThresholds:
    """Build the 1RM–10RM threshold weights for a single tier.

    The 1RM threshold is multiplier × bodyweight; each rep-max weight is derived
    from it via threshold_for_reps (Epley), then quantised to two decimals.
    """
    one_rm = (multiplier * bodyweight_kg).quantize(Decimal("0.01"))
    rep_maxes = tuple(
        RepMaxThreshold(
            reps=n,
            weight=threshold_for_reps(one_rm, n).quantize(Decimal("0.01")),
        )
        for n in range(1, 11)
    )
    return TierThresholds(
        tier=tier,
        multiplier=multiplier,
        one_rm_threshold=one_rm,
        rep_maxes=rep_maxes,
    )


def gap_to_threshold(performed_weight: Decimal, threshold_weight: Decimal) -> Decimal:
    """Return how much weight is still needed to reach the threshold.

    Positive means the lifter is short by that many kg; zero means met or exceeded.
    """
    gap = threshold_weight - performed_weight
    if gap < 0:
        return Decimal("0.00")
    return gap.quantize(Decimal("0.01"))


def best_score_for_set(
    performed_reps: int,
    performed_weight: Decimal,
    one_rm_threshold: Decimal | Mapping[int, Decimal],
) -> tuple[int, int] | None:
    """Return (points, rep_count_satisfied) for the highest-scoring threshold met.

    Iterates rep counts 1–10 (highest points first).  A threshold for rep count
    n is satisfied when BOTH conditions hold:
      - performed_weight >= threshold for rep count n (exact, no fuzz band)
      - effective_reps >= n  where effective_reps = min(performed_reps, 10)

    Every challenge authors a flat per-lift, per-rep target table (there is no
    bodyweight-relative standard left in this product — TASK-248), so
    ``one_rm_threshold`` is normally the participant's ``Mapping[int, Decimal]``
    of ``{rep_count: threshold_weight}``, consumed directly. A bare 1RM
    ``Decimal`` is still accepted, in which case each rep count's threshold is
    derived via ``threshold_for_reps`` (Epley) — used by the goal-setup
    suggesters (challenges.goal_builders) to preview a ladder before it is
    materialised into a flat table.

    The comparison is exact: there is no tolerance fuzz band. Every target is a
    static, entered-once weight, so there is no bodyweight drift to absorb.

    Returns the first (highest-point) match, or None if no threshold is met.
    Capping effective_reps at 10 means 12+ reps at heavy weight can still earn
    10 points if the weight satisfies the 1RM threshold.
    """
    is_mapping = isinstance(one_rm_threshold, Mapping)
    effective_reps = min(performed_reps, 10)
    for n in range(1, 11):
        threshold = (
            one_rm_threshold[n]
            if is_mapping
            else threshold_for_reps(one_rm_threshold, n)
        )
        if satisfies_threshold(effective_reps, performed_weight, n, threshold):
            return (points_for_rep_count(n), n)
    return None


def best_score_for_rep_target(
    performed_reps: int,
    performed_weight: Decimal,
    target_reps: int,
    target_weight: Decimal,
) -> int | None:
    """Score a set against a Rep Target goal's single (weight, reps) pair.

    Unlike best_score_for_set, weight is a GATE here, not a tradeoff axis:
    there is only one weight to hit, so ``performed_weight >= target_weight``
    is a strict pass/fail with no substitution of extra reps for missing
    weight. Returns ``None`` when the gate fails -- the same "no threshold
    met" signal best_score_for_set returns, so callers (scoring.services)
    persist it the same way (a zero-point audit row, never current-best).

    When the gate passes, points scale with reps performed toward the target,
    capped at the target (extra reps beyond it earn nothing more):
    ``floor(10 * min(performed_reps, target_reps) / target_reps)``.

    Flooring, not rounding. Round-half-up awarded the full 10 points at 95%
    of the target, so the rep count the participant actually chose was never
    the one that earned full marks: a 32-rep target maxed out at 31, a
    100-rep target at 95 (UAT). Flooring makes every tier an honest fraction
    of the target -- N points needs N/10 of the reps, and 10 points needs all
    of them.

    A single qualifying rep against a very high target_reps can legitimately
    score 0 points (issue #85 open question #3) -- accepted by design: the
    weight gate alone already tells the lifter they're "on the board", and
    best-set-replaces-old aggregation means any later, better set overwrites
    this one upward. Flooring every qualifying set at 1 point would make an
    arbitrarily large target_reps trivially farmable for free points.
    """
    if performed_weight < target_weight:
        return None
    effective_reps = min(performed_reps, target_reps)
    points = (10 * effective_reps) // target_reps
    return max(0, min(10, points))
