"""Tests for the corrected_weight_kg inversion (TASK-327)."""

from decimal import Decimal

import pytest

from core.lb_conversion_repair import corrected_weight_kg


class TestCorrectedWeightKg:
    @pytest.mark.parametrize(
        "old_kg,new_kg",
        [
            (Decimal("102.28"), Decimal("102.29")),  # 225.5 lb
            (Decimal("172.36"), Decimal("172.37")),  # 380 lb
        ],
    )
    def test_identifies_a_known_affected_half_pound_value(self, old_kg, new_kg):
        assert corrected_weight_kg(old_kg) == new_kg

    def test_leaves_a_half_pound_value_alone_when_both_factors_agree(self):
        # 100 lb rounds to 45.36 kg under both the old and exact factor, so
        # there is nothing to restamp even though it round-trips the grid.
        assert corrected_weight_kg(Decimal("45.36")) is None

    def test_leaves_a_value_off_the_half_pound_grid_alone(self):
        # No half-pound lb value rounds to 102.30 kg under the old factor, so
        # this could only be a genuine native-kg row -- must not be touched.
        assert corrected_weight_kg(Decimal("102.30")) is None

    def test_none_input_returns_none(self):
        assert corrected_weight_kg(None) is None

    def test_zero_returns_none(self):
        assert corrected_weight_kg(Decimal("0")) is None

    def test_a_weight_too_small_to_round_to_any_half_pound_returns_none(self):
        # 0.01 kg divides down to 0 lb on the half-pound grid -- there is no
        # candidate lb value to check at all.
        assert corrected_weight_kg(Decimal("0.01")) is None
