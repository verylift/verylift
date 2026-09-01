"""Parsing, validation, and persistence of participant-authored Rep Target goals.

The REP_TARGET sibling of challenges.custom_goals: each participant authors a
single (target_weight, target_reps) pair per configured lift, rather than a
1RM-10RM ladder. Manual entry is the only input path (there is no
strength-standards or JSON-paste method for this mode -- issue #85); a
"Suggest targets" convenience (challenges.goal_builders.suggest_rep_targets_
from_history) can prefill the grid, but the participant always confirms
through this same grid before it locks.
"""

import logging
from decimal import Decimal

from django.db import transaction
from django.utils.translation import gettext

from accounts.units import to_display_weight
from challenges.custom_goals import _bodyweight_added_lift_names, _to_kg
from challenges.models import RepTargetGoal, RepTargetGoalTarget
from challenges.standards import covered_lift_names

logger = logging.getLogger(__name__)

MAX_TARGET_REPS = 999


def rep_target_field_names(lift_index: int) -> tuple[str, str]:
    """POST field names for one lift's manual-grid row, keyed by ordinal
    position -- same rationale as challenges.custom_goals.grid_field_name:
    position (not a slugified name) so two distinct lift names never collide.
    """
    return f"target_weight__{lift_index}", f"target_reps__{lift_index}"


def _reps_to_int(raw) -> int | None:
    try:
        value = int(str(raw).strip())
    except (ValueError, TypeError):
        return None
    if value < 1 or value > MAX_TARGET_REPS:
        return None
    return value


def _is_untouched_row(
    raw_weight: str, raw_reps: str, *, is_bodyweight_added: bool
) -> bool:
    """Is this grid row still exactly as the page served it?

    A row the participant has not reached is blank -- except on a
    bodyweight-added lift, whose weight is served prefilled at 0 (see
    :func:`challenges.services.build_rep_target_goal_context`). That prefill
    is a starting point, not an answer, so a bodyweight row holding 0 and no
    reps has to count as untouched too. Without this the row parses as
    half-filled and the participant is told their rep count "must be a whole
    number between 1 and 999" -- a complaint about a field they never typed
    in -- instead of the plain "is missing a target" every other skipped row
    gets, and "Suggest targets" pins the 0 as if they had chosen it.
    """
    if raw_reps:
        return False
    if not raw_weight:
        return True
    if not is_bodyweight_added:
        return False
    try:
        return Decimal(raw_weight) == 0
    except (ArithmeticError, ValueError):
        return False


def parse_rep_target_grid(
    post_data, challenge, unit: str
) -> tuple[dict[str, tuple[Decimal, int]], list[str]]:
    """Parse manual-grid POST fields into a ``{lift: (target_weight_kg, target_reps)}``
    table.

    Untouched rows are left out (completeness is checked separately by
    :func:`rep_target_goal_is_complete`, so they still come back as "missing a
    target" -- see :func:`_is_untouched_row` for what counts as untouched); a
    non-numeric weight, a non-positive weight (except for bodyweight-added
    lifts, whose targets are added weight), or a rep count outside
    1..MAX_TARGET_REPS all surface as errors.
    """
    errors: list[str] = []
    targets: dict[str, tuple[Decimal, int]] = {}
    configured = covered_lift_names(challenge)
    bw_added = _bodyweight_added_lift_names(configured)
    for lift_index, lift_name in enumerate(sorted(configured)):
        weight_field, reps_field = rep_target_field_names(lift_index)
        raw_weight = (post_data.get(weight_field) or "").strip()
        raw_reps = (post_data.get(reps_field) or "").strip()
        allow_non_positive = lift_name in bw_added
        if _is_untouched_row(
            raw_weight, raw_reps, is_bodyweight_added=allow_non_positive
        ):
            continue

        weight_kg = _to_kg(raw_weight, unit, allow_non_positive=allow_non_positive)
        if weight_kg is None:
            message = (
                gettext('"%(lift_name)s" target weight must be a number.')
                if allow_non_positive
                else gettext('"%(lift_name)s" target weight must be a positive number.')
            )
            errors.append(message % {"lift_name": lift_name})

        reps = _reps_to_int(raw_reps)
        if reps is None:
            errors.append(
                gettext(
                    '"%(lift_name)s" target reps must be a whole number between '
                    "1 and %(max_reps)s."
                )
                % {"lift_name": lift_name, "max_reps": MAX_TARGET_REPS}
            )

        if weight_kg is not None and reps is not None:
            targets[lift_name] = (weight_kg, reps)

    return targets, errors


def merge_suggested_fields(
    post_data, suggested, challenge, unit: str
) -> tuple[dict[str, str], set[str]]:
    """Per-field merge of history suggestions into the participant's typed grid.

    A field the participant already filled is pinned -- kept verbatim, never
    overwritten -- and only blank fields take the suggestion, mirroring
    Classic's Compute button (custom_goal_setup.html), which fills blank
    cells and treats non-blank ones as anchors.

    A bodyweight-added row still holding its served 0 with no reps counts as
    blank here (:func:`_is_untouched_row`), not as a pinned answer. Otherwise
    Suggest would fill that row's reps and leave its weight alone, and the
    two cells of one row would come back styled differently -- reps muted as
    suggested, weight plain as typed -- over a 0 the participant never chose.

    Returns ``({field_name: display_value}, suggested_field_names)`` --
    display-unit strings ready to re-render the grid, plus the set of fields
    the suggestion (rather than the participant) filled, for the template's
    suggested-cell styling and the ``suggested_fields`` hidden input.
    """
    values: dict[str, str] = {}
    suggested_fields: set[str] = set()
    configured = covered_lift_names(challenge)
    bw_added = _bodyweight_added_lift_names(configured)
    for lift_index, lift_name in enumerate(sorted(configured)):
        weight_field, reps_field = rep_target_field_names(lift_index)
        weight_kg, reps = (suggested or {}).get(lift_name, (None, None))
        raw_weight = (post_data.get(weight_field) or "").strip()
        raw_reps = (post_data.get(reps_field) or "").strip()
        if _is_untouched_row(
            raw_weight, raw_reps, is_bodyweight_added=lift_name in bw_added
        ):
            raw_weight = ""
        if raw_weight:
            values[weight_field] = raw_weight
        elif weight_kg is not None:
            display_value, _ = to_display_weight(weight_kg, unit)
            values[weight_field] = str(display_value)
            suggested_fields.add(weight_field)
        if raw_reps:
            values[reps_field] = raw_reps
        elif reps is not None:
            values[reps_field] = str(reps)
            suggested_fields.add(reps_field)
    return values, suggested_fields


def parse_suggested_fields(post_data) -> set[str]:
    """The ``suggested_fields`` hidden input, parsed back into a set.

    Restricted to real grid field names so a crafted POST can't smuggle
    arbitrary strings back into the template.
    """
    raw = {f for f in (post_data.get("suggested_fields") or "").split(",") if f}
    return {f for f in raw if f.startswith(("target_weight__", "target_reps__"))}


def rep_target_goal_is_complete(
    targets: dict[str, tuple[Decimal, int]], challenge
) -> list[str]:
    """Return missing-lift errors: every configured lift needs a target."""
    errors: list[str] = []
    for lift_name in sorted(covered_lift_names(challenge)):
        if lift_name not in targets:
            errors.append(
                gettext('"%(lift_name)s" is missing a target.')
                % {"lift_name": lift_name}
            )
    return errors


def save_rep_target_goal(
    participant,
    name: str,
    targets: dict[str, tuple[Decimal, int]],
    *,
    source_method: str = RepTargetGoal.SourceMethod.CUSTOM,
) -> RepTargetGoal:
    """Persist a complete target table as the participant's create-only Rep
    Target goal. Mirrors challenges.custom_goals.save_custom_goal: goals are
    locked once joined, so this raises if the participant already has one.
    """
    if participant.rep_target_goal_id is not None:
        logger.error(
            "User %s attempted to overwrite an existing rep target goal for "
            "challenge %s (participant %s already has goal %s)",
            participant.user_id,
            participant.challenge_id,
            participant.pk,
            participant.rep_target_goal_id,
        )
        raise ValueError("Participant already has a locked rep target goal.")

    with transaction.atomic():
        goal = RepTargetGoal.objects.create(
            participant=participant,
            name=name,
            source_method=source_method,
        )

        RepTargetGoalTarget.objects.bulk_create(
            [
                RepTargetGoalTarget(
                    goal=goal,
                    lift=lift_name,
                    target_weight=weight_kg,
                    target_reps=reps,
                )
                for lift_name, (weight_kg, reps) in targets.items()
            ]
        )

        participant.rep_target_goal = goal
        participant.save(update_fields=["rep_target_goal"])

    logger.info(
        "User %s saved rep target goal %s (%s lift(s)) for challenge %s",
        participant.user_id,
        goal.pk,
        len(targets),
        participant.challenge_id,
    )
    return goal


def detach_active_rep_target_goal(participant) -> None:
    """Detach (not delete) a participant's active Rep Target goal when they
    leave. Mirrors challenges.custom_goals.detach_active_goal exactly --
    same rename-to-free-the-name trick for the (participant, name) unique
    constraint on rejoin. No-op if the participant never finished goal setup.
    """
    if participant.rep_target_goal_id is None:
        return
    goal = participant.rep_target_goal
    # Truncate so name + suffix fits max_length=100 -- a 62+ char goal name
    # would otherwise overflow and 500 the leave/bail path. The detached name
    # is never surfaced, so losing its tail is harmless.
    suffix = f" [{goal.id}]"
    goal.name = f"{goal.name[: 100 - len(suffix)]}{suffix}"
    goal.save(update_fields=["name"])
    participant.rep_target_goal = None
