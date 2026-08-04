"""The standards-source seam for challenges.

Every challenge is CUSTOM (TASK-248 plan §3): the owner names its lifts
explicitly at creation time (ChallengeLift), and every goal-setting method
(strength standards, fully custom, suggested from history) materialises into
the same flat CustomGoal/CustomGoalTarget shape. This module has collapsed to
the one function every other app still calls to resolve a challenge's
configured lift set.
"""


def covered_lift_names(challenge) -> set[str]:
    """The set of lifts a challenge is configured on."""
    return set(challenge.custom_lifts.values_list("name", flat=True))
