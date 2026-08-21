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
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils.translation import gettext

from accounts.units import from_display_weight
from challenges.models import RepTargetGoal, RepTargetGoalTarget
from challenges.standards import covered_lift_names
from liftosaur.models import Lift

logger = logging.getLogger(__name__)

MAX_TARGET_REPS = 999


def rep_target_field_names(lift_index: int) -> tuple[str, str]:
    """POST field names for one lift's manual-grid row, keyed by ordinal
    position -- same rationale as challenges.custom_goals.grid_field_name:
    position (not a slugified name) so two distinct lift names never collide.
    """
    return f"target_weight__{lift_index}", f"target_reps__{lift_index}"


def _bodyweight_added_lift_names(configured: set[str]) -> set[str]:
    return set(
        Lift.objects.filter(name__in=configured, is_bodyweight_added=True).values_list(
            "name", flat=True
        )
    )


def _weight_to_kg(raw, unit: str, *, allow_non_positive: bool) -> Decimal | None:
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError, TypeError):
        return None
    if not allow_non_positive and value <= 0:
        return None
    return from_display_weight(value, unit)


def _reps_to_int(raw) -> int | None:
    try:
        value = int(str(raw).strip())
    except (ValueError, TypeError):
        return None
    if value < 1 or value > MAX_TARGET_REPS:
        return None
    return value


def parse_rep_target_grid(
    post_data, challenge, unit: str
) -> tuple[dict[str, tuple[Decimal, int]], list[str]]:
    """Parse manual-grid POST fields into a ``{lift: (target_weight_kg, target_reps)}``
    table.

    Blank rows are left out (completeness is checked separately by
    :func:`rep_target_goal_is_complete`); a non-numeric weight, a non-positive
    weight (except for bodyweight-added lifts, whose targets are added
    weight), or a rep count outside 1..MAX_TARGET_REPS all surface as errors.
    """
    errors: list[str] = []
    targets: dict[str, tuple[Decimal, int]] = {}
    configured = covered_lift_names(challenge)
    bw_added = _bodyweight_added_lift_names(configured)
    for lift_index, lift_name in enumerate(sorted(configured)):
        weight_field, reps_field = rep_target_field_names(lift_index)
        raw_weight = (post_data.get(weight_field) or "").strip()
        raw_reps = (post_data.get(reps_field) or "").strip()
        if not raw_weight and not raw_reps:
            continue

        allow_non_positive = lift_name in bw_added
        weight_kg = _weight_to_kg(
            raw_weight, unit, allow_non_positive=allow_non_positive
        )
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
    goal.name = f"{goal.name} [{goal.id}]"
    goal.save(update_fields=["name"])
    participant.rep_target_goal = None
