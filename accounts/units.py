"""Display-only weight unit conversion.

Weights are always stored in kg internally. These helpers convert to/from a
user's preferred display unit. Conversion is display-only — never persist the
converted value.
"""

from decimal import ROUND_HALF_UP, Decimal

KG = "kg"
LB = "lb"

# Canonical conversion factor: 1 lb = 0.453592 kg. Stored weights are always
# kg, so this is the single source of truth; KG_TO_LB is its exact inverse.
LB_TO_KG = Decimal("0.453592")
KG_TO_LB = Decimal(1) / LB_TO_KG


def to_display_weight(kg_value, unit_preference):
    """Convert a stored kg weight to the user's preferred display unit.

    Returns a (display_value, unit_label) tuple. display_value is a Decimal
    rounded to one decimal place, or None when kg_value is None. unit_label is
    "kg" or "lb". Raises ValueError for any other unit_preference.
    """
    if unit_preference not in (KG, LB):
        raise ValueError(f"Unknown unit preference: {unit_preference!r}")
    label = unit_preference

    if kg_value is None:
        return None, label

    value = Decimal(kg_value)
    if unit_preference == LB:
        value = value * KG_TO_LB
    return value.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP), label


def from_display_weight(display_value, unit_preference):
    """Convert a value entered in the user's display unit back to kg for storage.

    Returns a Decimal in kg, or None when display_value is None. Raises
    ValueError for any unit_preference other than "kg" or "lb".
    """
    if unit_preference not in (KG, LB):
        raise ValueError(f"Unknown unit preference: {unit_preference!r}")
    if display_value is None:
        return None
    value = Decimal(display_value)
    if unit_preference == LB:
        return (value / KG_TO_LB).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
