"""Pure logic for the restamp_lb_converted_lift_history repair (TASK-327).

TASK-325 corrected accounts.units.LB_TO_KG from the truncated
Decimal("0.453592") to the exact international factor
Decimal("0.45359237"). For 35 half-pound values between 0.5 and 1000 lb
(starting at 225.5 lb), the two factors round to a different 2-decimal
weight_kg. LiftHistory's (user, lift, performed_at, reps, weight_kg) identity
means a row already pooled under the old factor no longer matches the SAME
physical set arriving again through a source that now applies the exact
factor -- producing a second row instead of an upsert.

Nothing on a LiftHistory row records the weight_lbs it was converted from, or
even whether it was converted at all -- a stored 172.36 could be a genuine
172.36 kg squat or a mis-converted 380 lb one. This module answers, for one
stored weight_kg, whether a half-pound lb value (the standard plate
increment, and the grid TASK-325's own measurement swept) exists that
reproduces it EXACTLY under the old factor. Over the full 0.5-1000 lb
half-pound grid this inverse is unique and self-consistent -- no two
half-pound values collide on the same rounded old-factor kg, and every
grid point round-trips exactly (verified by sweeping the grid; see
liftosaur/tests/test_lb_conversion_repair.py) -- so an exact round-trip match
is strong evidence the row really was converted from that lb value, not a
coincidence. A row that doesn't round-trip this way is left alone: it was
either always stored in kg, or converted from an off-grid lb value this
repair can't identify with confidence either way.
"""

from decimal import ROUND_HALF_UP, Decimal

from accounts.units import LB_TO_KG

# The truncated factor LiftHistory rows were pooled under before TASK-325.
# Hardcoded here (rather than imported) because it no longer exists anywhere
# else in the codebase -- it is a fact about history, not a live constant.
_OLD_LB_TO_KG = Decimal("0.453592")

_HALF_LB = Decimal("0.5")
_TWO_DP = Decimal("0.01")


def _round(value, exp):
    return value.quantize(exp, rounding=ROUND_HALF_UP)


def corrected_weight_kg(weight_kg: Decimal) -> Decimal | None:
    """Return the exact-factor weight_kg if `weight_kg` is confidently a
    mis-converted half-pound lb value, else None (leave the row alone).
    """
    if weight_kg is None or weight_kg <= 0:
        return None

    approx_lb = weight_kg / _OLD_LB_TO_KG
    candidate_lb = _round(approx_lb / _HALF_LB, Decimal("1")) * _HALF_LB
    if candidate_lb <= 0:
        return None

    if _round(candidate_lb * _OLD_LB_TO_KG, _TWO_DP) != weight_kg:
        return None

    new_weight_kg = _round(candidate_lb * LB_TO_KG, _TWO_DP)
    if new_weight_kg == weight_kg:
        return None
    return new_weight_kg
