"""Presentation-only lift groupings for the challenge custom-lift picker.

These are UI filter presets, not intrinsic lift qualities: no scoring,
validation, or Liftosaur-sync behavior depends on them, so they live as Python
constants rather than DB flags (contrast Lift.is_bodyweight_added /
is_liftosaur_builtin, which drive server behavior and warrant migrations).

Every name below must match a seeded liftosaur.Lift name verbatim — the
guard test in challenges/tests/test_lift_presets.py pins these constants to
the seeded catalogue so a rename in the fixtures fails CI.
"""

# The legacy very lift catalogue: originally the exact set of lifts
# in the built-in strength-standards fixtures (TASK-248 removed that app and
# its data entirely, so this is now a fixed curated list with nothing left to
# cross-check against -- kept for the custom-lift picker's grouping only).
CLASSIC_LIFT_NAMES: frozenset[str] = frozenset(
    {
        "Back Squat",
        "Front Squat",
        "Deadlift",
        "Sumo Deadlift",
        "Power Clean",
        "Bench Press",
        "Incline Bench Press",
        "Overhead Press",
        "Push Press",
        "Snatch Press",
        "Pull-up",
        "Chin-up",
        "Dip",
        "Pendlay Row",
    }
)

# The IPF powerlifting challenge lifts (the "big three"). Sumo Deadlift is
# deliberately excluded: IPF runs a single Deadlift event where stance is the
# lifter's choice, so "Deadlift" is the one entry — a separate Sumo Deadlift row
# is a very lift distinction, not an IPF one.
IPF_LIFT_NAMES: frozenset[str] = frozenset(
    {
        "Back Squat",
        "Bench Press",
        "Deadlift",
    }
)

# Bodyweight/calisthenics exercises the lift picker pre-checks under its own
# "Calisthenics" group (issue #85). Available regardless of challenge mode --
# an owner can still build a CLASSIC push-up rep-max chart -- but this is the
# preset a REP_TARGET challenge (no rep-max ladder makes sense for these)
# will typically reach for.
CALISTHENICS_LIFT_NAMES: frozenset[str] = frozenset(
    {
        "Push Up",
        "Pull-up",
        "Chin-up",
        "Dip",
        "Sit Up",
        "Handstand Push Up",
        "Hanging Leg Raise",
        "Toes To Bar",
        "Pistol Squat",
        "Inverted Row",
    }
)
