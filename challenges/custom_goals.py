"""Parsing, validation, and persistence of participant-authored custom goals.

Custom-standards challenges have no bodyweight-relative formula: each
participant authors a flat 1RM–10RM target table per configured lift, bundled
under one named :class:`CustomGoal`. Targets can be populated two equally
first-class ways — a pasted JSON payload or a manually filled grid — and both
funnel through the same validation vocabulary and completeness check here so
scoring only ever sees a complete, kg-normalised table.

JSON payload spec (weights in the payload's ``unit``, defaulting to the
participant's display unit)::

    {
      "unit": "lb",
      "targets": {
        "Bench Press": {"1": 225, "2": 215, ..., "10": 155}
      }
    }

A stray "name" key is tolerated and ignored — goals are no longer named by
the participant (see :func:`challenges.goal_builders.default_goal_name`).

For bodyweight-added lifts (Chin-up/Pull-up/Dip — flagged via
``core.Lift.is_bodyweight_added``) the target is the ADDED weight
relative to bodyweight, not the absolute load: 0 means bodyweight-only and a
negative value means leverage-machine assistance. Zero/negative targets are
therefore accepted for those lifts and rejected for every other lift.
"""

import json
import logging
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils.translation import gettext

from accounts.units import KG, LB, from_display_weight
from challenges.events import record_challenge_event
from challenges.models import ChallengeEvent, CustomGoal, CustomGoalTarget
from challenges.standards import covered_lift_names
from core.models import Lift

logger = logging.getLogger(__name__)

REP_COUNTS = range(1, 11)


def grid_field_name(lift_index: int, rep_count: int) -> str:
    """Field name for a manual-grid cell, keyed by the lift's ordinal position.

    Position (not a slugified name) keys the cell so two distinct lift names can
    never collide on the same field, and the parser never needs a reverse
    slug->name mapping — it re-derives the ordering from covered_lift_names.
    """
    return f"target__{lift_index}__{rep_count}"


def _positive_number_error(lift_name: str, rep: int) -> str:
    return gettext('"%(lift_name)s" %(rep)sRM target must be a positive number.') % {
        "lift_name": lift_name,
        "rep": rep,
    }


def _number_error(lift_name: str, rep: int) -> str:
    return gettext('"%(lift_name)s" %(rep)sRM target must be a number.') % {
        "lift_name": lift_name,
        "rep": rep,
    }


def unknown_lift_error(lift_name: str) -> str:
    """Message for a JSON-pasted lift name not configured for the challenge.

    Exposed (not module-private) so :class:`~challenges.forms.CustomGoalForm`
    can reuse the exact same wording when it decides to surface these as
    fatal, rather than duplicating the string.
    """
    return gettext(
        'Unknown lift "%(lift_name)s" — not configured for this challenge.'
    ) % {
        "lift_name": lift_name,
    }


def _bodyweight_added_lift_names(configured: set[str]) -> set[str]:
    """Names among ``configured`` whose targets are authored as added weight.

    Single query against the seeded Lift reference table so the parsers can allow
    zero/negative targets only for bodyweight-added lifts (Chin-up/Pull-up/Dip).
    """
    return set(
        Lift.objects.filter(name__in=configured, is_bodyweight_added=True).values_list(
            "name", flat=True
        )
    )


def _to_kg(raw, unit: str, *, allow_non_positive: bool = False) -> Decimal | None:
    """Convert a raw display-unit weight to kg.

    Returns None when ``raw`` is not a number, or when it is zero/negative and
    ``allow_non_positive`` is False. Bodyweight-added lifts pass
    ``allow_non_positive=True`` because their targets are added weight (0 =
    bodyweight-only, negative = machine-assisted).
    """
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError, TypeError):
        return None
    # Decimal("NaN")/Decimal("Infinity") parse fine but blow up in the
    # comparison below (InvalidOperation) or in from_display_weight's
    # quantize -- a crafted POST used to 500 both goal-setup views.
    if not value.is_finite():
        return None
    if not allow_non_positive and value <= 0:
        return None
    return from_display_weight(value, unit)


def parse_custom_goal_json(
    payload_text: str, challenge, default_unit: str
) -> tuple[dict[str, dict[int, Decimal]], list[str], list[str]]:
    """Parse a pasted JSON goal payload into a ``{lift: {rep: kg}}`` table.

    Returns ``(targets, errors, unknown_lifts)``: whatever cells parsed
    cleanly, a list of human-readable error messages, and a separate list of
    lift names present in the payload but not configured for this challenge.
    Unknown lift names are deliberately NOT folded into ``errors`` —
    TASK-314 lets the form/view decide whether they're fatal (blocked
    outright) or skippable-with-acknowledgment, unlike every other problem
    here, which is always fatal. Missing rep counts are NOT reported here —
    completeness is a separate concern (:func:`custom_goal_is_complete`)
    shared with the grid path — but malformed JSON, a mis-shaped lift, a bad
    unit, and non-numeric weights all surface as errors. Non-positive weights
    are rejected too, except for bodyweight-added lifts whose targets are
    added weight (0 = bodyweight-only, negative = machine-assisted). A stray
    "name" key in the payload is ignored, not validated.
    """
    try:
        data = json.loads(payload_text)
    except (json.JSONDecodeError, TypeError, ValueError):
        return (
            {},
            [gettext("Could not parse JSON. Please paste a valid JSON object.")],
            [],
        )

    if not isinstance(data, dict):
        return (
            {},
            [gettext('JSON payload must be an object with a "targets" key.')],
            [],
        )

    errors: list[str] = []
    unit = data.get("unit", default_unit)
    if unit not in (KG, LB):
        errors.append(
            gettext('Unknown unit "%(unit)s"; use "kg" or "lb".') % {"unit": unit}
        )
        unit = default_unit

    raw_targets = data.get("targets")
    if not isinstance(raw_targets, dict):
        errors.append(
            gettext(
                'JSON payload must contain a "targets" object mapping lift names to '
                "rep-max weights."
            )
        )
        return {}, errors, []

    configured = covered_lift_names(challenge)
    bw_added = _bodyweight_added_lift_names(configured)
    targets: dict[str, dict[int, Decimal]] = {}
    unknown_lifts: list[str] = []
    for lift_name, cells in raw_targets.items():
        if lift_name not in configured:
            unknown_lifts.append(lift_name)
            continue
        if not isinstance(cells, dict):
            errors.append(
                gettext(
                    'Targets for "%(lift_name)s" must be an object of '
                    "rep count to weight."
                )
                % {"lift_name": lift_name}
            )
            continue
        allow_non_positive = lift_name in bw_added
        lift_targets: dict[int, Decimal] = {}
        for rep in REP_COUNTS:
            raw = cells.get(str(rep))
            if raw is None:
                raw = cells.get(rep)
            if raw is None:
                continue
            weight_kg = _to_kg(raw, unit, allow_non_positive=allow_non_positive)
            if weight_kg is None:
                errors.append(
                    _number_error(lift_name, rep)
                    if allow_non_positive
                    else _positive_number_error(lift_name, rep)
                )
                continue
            lift_targets[rep] = weight_kg
        if lift_targets:
            targets[lift_name] = lift_targets
    return targets, errors, unknown_lifts


def parse_custom_goal_grid(
    post_data, challenge, unit: str
) -> tuple[dict[str, dict[int, Decimal]], list[str]]:
    """Parse manual-grid POST fields into a ``{lift: {rep: kg}}`` table.

    Mirrors :func:`parse_custom_goal_json`'s output shape and error vocabulary.
    Blank cells are left out (completeness is checked separately); non-numeric
    entries surface as errors, as do non-positive entries except for
    bodyweight-added lifts (whose targets are added weight).
    """
    errors: list[str] = []
    targets: dict[str, dict[int, Decimal]] = {}
    configured = covered_lift_names(challenge)
    bw_added = _bodyweight_added_lift_names(configured)
    for lift_index, lift_name in enumerate(sorted(configured)):
        allow_non_positive = lift_name in bw_added
        lift_targets: dict[int, Decimal] = {}
        for rep in REP_COUNTS:
            raw = (post_data.get(grid_field_name(lift_index, rep)) or "").strip()
            if not raw:
                continue
            weight_kg = _to_kg(raw, unit, allow_non_positive=allow_non_positive)
            if weight_kg is None:
                errors.append(
                    _number_error(lift_name, rep)
                    if allow_non_positive
                    else _positive_number_error(lift_name, rep)
                )
                continue
            lift_targets[rep] = weight_kg
        if lift_targets:
            targets[lift_name] = lift_targets
    return targets, errors


def _rep_max_order_error(lift_name: str, lower_rep: int, higher_rep: int) -> str:
    return gettext(
        '"%(lift_name)s" %(higher_rep)sRM target can\'t be heavier than its '
        "%(lower_rep)sRM target — lifting more reps can never require more "
        "weight than fewer reps."
    ) % {
        "lift_name": lift_name,
        "higher_rep": higher_rep,
        "lower_rep": lower_rep,
    }


def validate_rep_max_monotonicity(
    targets: dict[str, dict[int, Decimal]], challenge
) -> list[str]:
    """Reject a lift whose weight rises as rep count increases.

    A valid rep-max ladder is non-increasing 1RM..10RM (equal is fine — a
    genuinely rep-independent max, e.g. 5RM == 6RM). Comparing each present
    cell against the nearest lower-rep cell that is also present (not
    strictly N vs N-1) is sufficient to catch any ordering violation even
    across gaps, since non-increasing over every present pair implies
    non-increasing overall. Applied uniformly regardless of source — grid,
    JSON paste, or calculator-assisted — since none of parse_custom_goal_grid
    or parse_custom_goal_json cross-validate adjacent rep columns themselves.
    Only the first violation per lift is reported, matching
    custom_goal_is_complete's one-error-per-lift shape.
    """
    errors: list[str] = []
    for lift_name in sorted(covered_lift_names(challenge)):
        lift_targets = targets.get(lift_name) or {}
        previous_rep = None
        previous_weight = None
        for rep in REP_COUNTS:
            weight = lift_targets.get(rep)
            if weight is None:
                continue
            if previous_weight is not None and weight > previous_weight:
                errors.append(_rep_max_order_error(lift_name, previous_rep, rep))
                break
            previous_rep, previous_weight = rep, weight
    return errors


def custom_goal_is_complete(
    targets: dict[str, dict[int, Decimal]], challenge
) -> list[str]:
    """Return missing-cell errors: every configured lift needs all 10 rep counts.

    Empty list means the table is complete and the goal is usable.
    """
    errors: list[str] = []
    for lift_name in sorted(covered_lift_names(challenge)):
        lift_targets = targets.get(lift_name) or {}
        missing = [rep for rep in REP_COUNTS if rep not in lift_targets]
        if missing:
            missing_str = ", ".join(f"{rep}RM" for rep in missing)
            errors.append(
                gettext('"%(lift_name)s" is missing targets for %(missing)s.')
                % {"lift_name": lift_name, "missing": missing_str}
            )
    return errors


def save_custom_goal(
    participant,
    name: str,
    targets: dict[str, dict[int, Decimal]],
    *,
    source_method: str = CustomGoal.SourceMethod.CUSTOM,
    source_detail: dict | None = None,
) -> CustomGoal:
    """Persist a complete target table as the participant's create-only goal.

    Charts are locked once a participant joins (AC#4): a goal is created once
    and never replaced. If ``participant.custom_goal`` is already set this
    raises rather than overwriting it — defence-in-depth against a double
    submit race past the ``has_goal_configured`` guard in the view. Callers
    must pass a table that already passed :func:`custom_goal_is_complete`.

    ``source_detail`` is an already-built provenance dict (see
    ``CustomGoal.source_detail`` and TASK-248 plan §4) — this function never
    builds it itself, so it never needs to know about sex or bodyweight at
    all; it just writes the JSON it is handed. Defaults to CUSTOM/``{}``.
    """
    if participant.custom_goal_id is not None:
        logger.error(
            "User %s attempted to overwrite an existing custom goal for "
            "challenge %s (participant %s already has goal %s)",
            participant.user_id,
            participant.challenge_id,
            participant.pk,
            participant.custom_goal_id,
        )
        raise ValueError("Participant already has a locked custom goal.")

    with transaction.atomic():
        goal = CustomGoal.objects.create(
            participant=participant,
            name=name,
            source_method=source_method,
            source_detail=source_detail or {},
        )

        CustomGoalTarget.objects.bulk_create(
            [
                CustomGoalTarget(
                    goal=goal,
                    lift=lift_name,
                    rep_count=rep,
                    target_weight=weight_kg,
                )
                for lift_name, cells in targets.items()
                for rep, weight_kg in cells.items()
            ]
        )

        participant.custom_goal = goal
        participant.save(update_fields=["custom_goal"])
        record_challenge_event(
            participant.challenge,
            ChallengeEvent.EventType.GOAL_LOCKED,
            actor=participant.user,
        )

    logger.info(
        "User %s saved custom goal %s (%s lift(s)) for challenge %s",
        participant.user_id,
        goal.pk,
        len(targets),
        participant.challenge_id,
    )
    return goal


def detach_active_goal(participant) -> None:
    """Detach (not delete) a participant's active goal when they leave.

    Called from bail_view/remove_participant. The CustomGoal row itself is
    untouched -- still reachable via participant.custom_goals for history --
    but clearing participant.custom_goal makes has_goal_configured False
    again, so rejoining (which un-bails this same participant row rather
    than creating a fresh one) routes back through goal setup for a new
    chart instead of resurrecting the old one.

    Also renames the detached goal to free up its name: CustomGoal has a
    unique constraint on (participant, name), and that same participant row
    persists across a leave/rejoin cycle, so without this a rejoining user
    who types the same goal name again (or takes the same derived one, e.g.
    "Verified Intermediate") hits an IntegrityError against their own
    archived goal. Appending the goal's own
    id is guaranteed collision-free and permanent; nothing currently surfaces
    a detached goal's name anywhere, so the ugly suffix is invisible.
    No-op if the participant never finished goal setup.
    """
    if participant.custom_goal_id is None:
        return
    goal = participant.custom_goal
    # Truncate so name + suffix fits max_length=100 -- a 62+ char goal name
    # would otherwise overflow and 500 the leave/bail path. The detached name
    # is never surfaced, so losing its tail is harmless.
    suffix = f" [{goal.id}]"
    goal.name = f"{goal.name[: 100 - len(suffix)]}{suffix}"
    goal.save(update_fields=["name"])
    participant.custom_goal = None
