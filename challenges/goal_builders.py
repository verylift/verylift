"""Materialisation of goal-setup suggestions into a CustomGoal target table.

Every challenge is CUSTOM (TASK-248 plan §3): the four goal-setting methods
(strength standards, manual entry, JSON paste, suggested from Liftosaur
history) are all just prefill strategies that produce the same flat
``{lift: {rep: kg}}``
table, which ``save_custom_goal`` then persists verbatim. This module is the
only place in the codebase where a bodyweight number appears in arithmetic,
and — since there is no legacy-participant backfill (revision 5) — the only
place one is ever written to storage at all, via :func:`standards_source_detail`.
"""

import logging
from datetime import UTC, datetime, timedelta
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal

from accounts.units import from_display_weight, to_display_weight
from challenges.models import CustomGoal
from challenges.standards import covered_lift_names
from fitnessvolt import services as fitnessvolt_services
from liftosaur.models import LiftHistory
from scoring.domain.calculator import (
    estimated_one_rm,
    is_assisted_equipment,
    is_bodyweight_added_lift,
    threshold_for_reps,
    tier_thresholds,
)

logger = logging.getLogger(__name__)


def suggest_from_standards(
    challenge,
    *,
    population,
    snapshot_version,
    sex,
    bodyweight_kg,
    tier,
    rounding_amount=None,
    rounding_unit="kg",
) -> tuple[dict, list[str]]:
    """Build a ``{lift: {rep: kg}}`` table from a FitnessVolt standards cell.

    ``sex`` and ``bodyweight_kg`` are parameters, never read from ``user`` —
    after this task's removal of ``User.sex`` there is nothing on the user to
    read. Bodyweight-added lifts (Pull-up/Chin-up/Dip) are converted from the
    total-load standard back to added weight by subtracting ``bodyweight_kg``
    (the §3 conversion — ``CustomGoalTarget.target_weight`` is always added
    weight for these lifts, never total load), THEN rounded (rounding must
    apply to the final stored value, same reasoning as suggest_from_history).
    FitnessVolt's own percentile interpolation across weight classes lands on
    arbitrary values too (UAT feedback) -- not just history's Epley math --
    so rounding is offered for this method as well; see
    :func:`_round_to_increment` for why the rounding unit matters.

    Returns ``({lift: {rep: kg}}, uncovered_lifts)``: configured lifts with no
    published FitnessVolt cell for this population/tier are listed in
    ``uncovered_lifts`` and simply absent from the table, never defaulted.
    Never raises — an unusable population/snapshot logs a warning and
    degrades to "every configured lift uncovered" so the wizard never
    dead-ends.
    """
    configured = covered_lift_names(challenge)
    if not population or not snapshot_version:
        logger.warning(
            "suggest_from_standards called with no population/snapshot "
            "(population=%s snapshot_version=%s) for challenge %s",
            population,
            snapshot_version,
            challenge.pk,
        )
        return {}, sorted(configured)

    logger.info(
        "Suggesting standards goal for challenge %s: population=%s "
        "snapshot_version=%s tier=%s",
        challenge.pk,
        population,
        snapshot_version,
        tier,
    )

    cells = fitnessvolt_services.get_standards_bulk(
        population, snapshot_version, sex, bodyweight_kg
    )

    table: dict[str, dict[int, Decimal]] = {}
    for cell in cells:
        if cell["tier_label"] != tier:
            continue
        lift = cell["lift"]
        if lift not in configured or cell["multiplier"] is None:
            continue
        thresholds = tier_thresholds(tier, cell["multiplier"], bodyweight_kg)
        lift_is_bw_added = is_bodyweight_added_lift(lift)
        table[lift] = {
            rm.reps: _round_to_increment(
                rm.weight - bodyweight_kg if lift_is_bw_added else rm.weight,
                rounding_amount,
                rounding_unit,
            )
            for rm in thresholds.rep_maxes
        }

    uncovered_lifts = sorted(configured - set(table))
    if uncovered_lifts:
        logger.warning(
            "No published FitnessVolt cell for lift(s) %s (population=%s tier=%s)",
            uncovered_lifts,
            population,
            tier,
        )
    return table, uncovered_lifts


def standards_source_detail(
    *,
    population,
    snapshot_version,
    tier,
    sex,
    bodyweight_kg,
    rounding_amount=None,
    rounding_unit=None,
) -> dict:
    """Build the STANDARDS ``CustomGoal.source_detail`` provenance record.

    The ``str()`` on ``bodyweight_kg`` is load-bearing: this JSONField uses
    Django's default encoder, which cannot serialise a ``Decimal`` (raises
    ``TypeError`` at save time), and a ``float`` would drift the very number
    this field exists to pin. This is the only writer of a bodyweight or a
    sex value into storage anywhere in the product — see CustomGoal.source_detail
    and TASK-248 plan §4. ``rounding_amount``/``rounding_unit`` are recorded
    the same way as :func:`history_source_detail` does — a derivation
    parameter, not personal data, recorded as the exact chosen amount+unit
    rather than a kg-converted figure.
    """
    return {
        "population": population,
        "snapshot_version": snapshot_version,
        "tier": tier,
        "sex": sex,
        "bodyweight_kg": str(bodyweight_kg),
        "rounding_amount": (
            str(rounding_amount) if rounding_amount is not None else None
        ),
        "rounding_unit": rounding_unit,
    }


def history_source_detail(
    *, uplift, lookback_days, rounding_amount=None, rounding_unit=None
) -> dict:
    """Build the HISTORY ``CustomGoal.source_detail`` provenance record.

    Deliberately carries no sex/bodyweight (asymmetric with STANDARDS by
    design — see TASK-248 plan §4): a history-derived ladder's inputs
    (LiftHistory rows, uplift, lookback) all remain in the database, so the
    number stays explainable without a separate recorded figure. ``uplift``
    is user-editable (TASK-248 UAT feedback) and may arrive as a ``Decimal``
    from the goal-setup wizard's own resolution of it — cast to ``float``
    here, the type it's always had (``settings.CHALLENGES_GOAL_SUGGESTION_
    UPLIFT`` is a float, and this JSONField's default encoder cannot
    serialise a ``Decimal``). ``rounding_amount``/``rounding_unit`` are
    derivation *parameters* like uplift/lookback_days, not personal data, so
    they ARE recorded (``str()`` on the amount for the same JSONField/
    Decimal reason as :func:`standards_source_detail` — see there). Recorded
    as the exact chosen amount+unit, not a kg-converted figure: a kg
    equivalent of e.g. "5 lb" is not itself a round number, so it would be
    poor provenance for "why is this target this number" even though it's
    mathematically equivalent. ``None``/``None`` means the participant chose
    not to round.
    """
    return {
        "uplift": float(uplift),
        "lookback_days": lookback_days,
        "rounding_amount": (
            str(rounding_amount) if rounding_amount is not None else None
        ),
        "rounding_unit": rounding_unit,
    }


def _round_to_increment(
    weight_kg: Decimal, increment_amount: Decimal | None, increment_unit: str = "kg"
) -> Decimal:
    """Round a computed target to the nearest multiple of ``increment_amount``,
    expressed in ``increment_unit`` ("kg" or "lb") -- NOT in kg regardless of
    the participant's chosen unit.

    Raw Epley/uplift math lands on arbitrary hundredths of a kg -- a real
    barbell, dumbbell, or machine only offers discrete steps, so an
    unrounded suggestion reads as nonsensical (UAT feedback: "crazy
    numbers"). The rounding must happen IN the chosen unit: converting a
    clean "5 lb" into kg first quantizes to 2.27 kg (from_display_weight's
    own 0.01 kg precision), and rounding to multiples of THAT drifts away
    from clean 5 lb multiples as the multiplier grows -- caught in UAT as a
    "204.3 lb" result from a "5 lb" choice. Converting to the target unit,
    rounding there, and converting only the FINAL value back to kg avoids
    this entirely -- the stored kg value is whatever kg equals an exactly
    clean number in the unit that was actually asked for.

    ``None`` or a non-positive amount means the participant asked for no
    rounding; the raw value is returned unchanged. Ties round away from zero
    (ROUND_HALF_UP), which also does the right thing for a negative
    added-weight rung on an assisted setup.
    """
    if increment_amount is None or increment_amount <= 0:
        return weight_kg
    display_value, _ = to_display_weight(weight_kg, increment_unit)
    steps = (display_value / increment_amount).to_integral_value(rounding=ROUND_HALF_UP)
    rounded_display = steps * increment_amount
    return from_display_weight(rounded_display, increment_unit)


def suggest_from_history(
    user,
    challenge,
    *,
    bodyweight_kg=None,
    uplift,
    lookback_days,
    rounding_amount=None,
    rounding_unit="kg",
) -> tuple[dict, list[str], list[str]]:
    """Build a suggested ``{lift: {rep: kg}}`` table from Liftosaur history.

    For each lift the suggestion is the best e1RM in the lookback window
    (``estimated_one_rm``, capped at 10 reps to mirror ``best_score_for_set``),
    uplifted by ``uplift``, expanded into rep-max targets via
    ``threshold_for_reps``, then rounded to the nearest multiple of
    ``rounding_amount`` IN ``rounding_unit`` (see :func:`_round_to_increment`
    for why the rounding unit matters, not just the kg-equivalent amount) --
    the raw math alone lands on arbitrary hundredths of a kg, which read as
    nonsensical for an actual barbell/dumbbell/machine (UAT feedback).
    Rounding is applied to the FINAL stored value (added weight for
    bodyweight-added lifts, absolute weight otherwise) — the number the
    participant actually sees and loads, not an intermediate total-load
    figure they never see.

    Bodyweight-added lifts (Pull-up/Chin-up/Dip) compute e1RM on TOTAL load
    (``bodyweight_kg + row.weight_kg``) — Epley on the raw added weight alone
    is degenerate (``estimated_one_rm(0, 8) == 0``, which would materialise an
    all-zero ladder) — then subtract ``bodyweight_kg`` back off each rung so
    the stored target is added weight, matching every other CustomGoalTarget.
    Assisted-equipment rows are always skipped for these lifts: their
    recorded weight is net total load, not added weight, and is not
    comparable (see is_assisted_equipment / TASK-248 plan §1b).

    ``bodyweight_kg`` is NOT persisted by this function — history_source_detail
    takes ``uplift``, ``lookback_days``, ``rounding_amount``, and
    ``rounding_unit`` (the sex/bodyweight asymmetry with the standards method
    is deliberate, see TASK-248 plan §4; the rounding choice is a derivation
    parameter, not personal data, so it IS recorded there).

    Returns ``({lift: {rep: kg}}, lifts_needing_decision, assisted_only_lifts)``.
    A lift lands in ``lifts_needing_decision`` when it has no usable history in
    the window, OR it is bodyweight-added with a usable (non-assisted) row but
    no ``bodyweight_kg`` was entered (the degenerate case is never silently
    defaulted). ``assisted_only_lifts`` lists bodyweight-added lifts whose
    window history was entirely assisted-equipment, so the wizard can warn
    that they will never score (TASK-248 plan §1b).
    """
    configured = sorted(covered_lift_names(challenge))
    cutoff = (datetime.now(tz=UTC) - timedelta(days=lookback_days)).date()

    table: dict[str, dict[int, Decimal]] = {}
    needs_decision: list[str] = []
    assisted_only_lifts: list[str] = []

    for lift in configured:
        rows = LiftHistory.objects.filter(
            user=user, lift=lift, performed_at__gte=cutoff
        )
        is_added = is_bodyweight_added_lift(lift)

        best_one_rm: Decimal | None = None
        saw_any_row = False
        saw_usable_row = False
        for row in rows:
            saw_any_row = True
            if is_added:
                if is_assisted_equipment(row.equipment):
                    continue
                if bodyweight_kg is None:
                    # A usable (non-assisted) row exists but there is nothing
                    # to convert total load with — needs an explicit decision,
                    # never a guessed default.
                    saw_usable_row = True
                    continue
                one_rm = estimated_one_rm(
                    bodyweight_kg + row.weight_kg, min(row.reps, 10)
                )
            else:
                one_rm = estimated_one_rm(row.weight_kg, min(row.reps, 10))
            saw_usable_row = True
            if best_one_rm is None or one_rm > best_one_rm:
                best_one_rm = one_rm

        if is_added and saw_any_row and not saw_usable_row:
            assisted_only_lifts.append(lift)

        if best_one_rm is None:
            needs_decision.append(lift)
            continue

        uplifted = (best_one_rm * (1 + Decimal(str(uplift)))).quantize(Decimal("0.01"))
        rungs = {
            n: threshold_for_reps(uplifted, n).quantize(Decimal("0.01"))
            for n in range(1, 11)
        }
        if is_added:
            rungs = {n: (weight - bodyweight_kg) for n, weight in rungs.items()}
        table[lift] = {
            n: _round_to_increment(weight, rounding_amount, rounding_unit)
            for n, weight in rungs.items()
        }

    if needs_decision:
        logger.warning(
            "History suggestion: lift(s) %s for user %s need an explicit "
            "decision (no usable history in the last %s day(s))",
            needs_decision,
            user.id,
            lookback_days,
        )
    if assisted_only_lifts:
        logger.info(
            "History suggestion: lift(s) %s for user %s had only "
            "assisted-equipment sets in the window",
            assisted_only_lifts,
            user.id,
        )
    return table, needs_decision, assisted_only_lifts


REP_TARGET_SUGGESTED_REPS = 5
_REP_TARGET_ROUNDING_LB = Decimal("5")


def _ceil_to_5lb(weight_kg: Decimal) -> Decimal:
    """Round a computed target UP to the nearest 5 lb, in kg.

    Always rounds up, never to the nearest or down: this is used to turn an
    uplifted historical max into a target that is guaranteed to still sit
    above that max after rounding (a floor or nearest-rounding could round
    the uplift itself away for a small uplift/max combination, silently
    undoing the "must be a new goal, not one you've already hit" guarantee).
    Unlike :func:`_round_to_increment`, the rounding unit is hardcoded to lb
    rather than the participant's ``unit_preference``: "the nearest 5 lb" is
    a product decision about how coarse the suggestion should be, not a
    display concern, so it doesn't follow kg/lb display choice the way a
    rep-max ladder rung does.
    """
    display_lb, _ = to_display_weight(weight_kg, "lb")
    steps = (display_lb / _REP_TARGET_ROUNDING_LB).to_integral_value(
        rounding=ROUND_CEILING
    )
    return from_display_weight(steps * _REP_TARGET_ROUNDING_LB, "lb")


def suggest_rep_targets_from_history(
    user, challenge, *, lookback_days, uplift
) -> tuple[dict[str, tuple[Decimal, int]], list[str]]:
    """Build a suggested ``{lift: (target_weight_kg, target_reps)}`` table from
    Liftosaur/pooled LiftHistory, for the Rep Target goal-setup "Suggest
    targets" convenience (issue #85).

    For a regular (non-bodyweight-added) lift, the target is the heaviest
    weight recorded in the window, uplifted by ``uplift`` (the same
    ``CHALLENGES_GOAL_SUGGESTION_UPLIFT`` fraction Classic's ladder builder
    uses) and rounded UP to the nearest 5 lb (:func:`_ceil_to_5lb`), at a
    fixed ``REP_TARGET_SUGGESTED_REPS``-rep target. Scoring gates on raw
    performed weight >= target weight (``best_score_for_rep_target``), so a
    target derived from e1RM/Epley math on the lifter's own sets -- however it
    was reshaped -- is mathematically guaranteed to sit at or below a weight
    they already lifted, which is exactly why that approach (an earlier
    version of this function) suggested "goals" the lifter had already scored
    10/10 points on before even confirming them (UAT feedback). Uplifting the
    raw historical max and rounding up, rather than down, guarantees the
    opposite: the target sits strictly above anything already logged, so a
    freshly confirmed goal always starts at 0 points, with the smallest step
    up that still clears the rounding as the "good chance to score soon"
    case.

    Bodyweight-added lifts (Pull-up/Chin-up/Dip/Pistol Squat) keep the older
    verbatim behavior: the best already-recorded (weight, reps) row, ties
    broken toward more reps. Their "weight" is added weight, not total load;
    revisit alongside this same 0-points concern if it comes up for them too.

    Assisted-equipment rows on bodyweight-added lifts are skipped (their
    recorded weight is net total load, not added weight, and isn't comparable
    -- same rule as suggest_from_history). Returns
    ``({lift: (weight_kg, reps)}, lifts_with_no_history)``.
    """
    configured = sorted(covered_lift_names(challenge))
    cutoff = (datetime.now(tz=UTC) - timedelta(days=lookback_days)).date()

    table: dict[str, tuple[Decimal, int]] = {}
    no_history: list[str] = []

    for lift in configured:
        is_added = is_bodyweight_added_lift(lift)
        rows = LiftHistory.objects.filter(
            user=user, lift=lift, performed_at__gte=cutoff
        )
        if is_added:
            best: tuple[Decimal, int] | None = None
            for row in rows:
                if is_assisted_equipment(row.equipment):
                    continue
                candidate = (row.weight_kg, row.reps)
                if best is None or candidate > best:
                    best = candidate
            if best is None:
                no_history.append(lift)
            else:
                table[lift] = best
            continue

        weights = [row.weight_kg for row in rows]
        if not weights:
            no_history.append(lift)
        else:
            target_weight = _ceil_to_5lb(max(weights) * (1 + Decimal(str(uplift))))
            table[lift] = (target_weight, REP_TARGET_SUGGESTED_REPS)

    if no_history:
        logger.warning(
            "Rep target history suggestion: lift(s) %s for user %s have no "
            "usable history in the last %s day(s)",
            no_history,
            user.id,
            lookback_days,
        )
    return table, no_history


def default_goal_name(method, *, tier=None, population=None, uplift=None) -> str:
    """A sensible, never-demanded default CustomGoal.name for each method."""
    if method == CustomGoal.SourceMethod.STANDARDS and population and tier:
        return f"{population.capitalize()} {tier}"
    if method == CustomGoal.SourceMethod.HISTORY:
        if uplift is not None:
            return f"Suggested from history (+{Decimal(str(uplift)) * 100:.0f}%)"
        return "Suggested from history"
    return "My Goal"
