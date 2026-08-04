"""Tests for the weight unit conversion helpers (TASK-83)."""

from decimal import Decimal

import pytest

from accounts.units import from_display_weight, to_display_weight


class TestToDisplayWeight:
    def test_kg_passthrough(self):
        value, label = to_display_weight(Decimal("100.00"), "kg")
        assert value == Decimal("100.0")
        assert label == "kg"

    def test_kg_to_lb_conversion(self):
        value, label = to_display_weight(Decimal("100.00"), "lb")
        assert value == Decimal("220.5")
        assert label == "lb"

    def test_rounds_to_one_decimal(self):
        value, _ = to_display_weight(Decimal("80.456"), "kg")
        assert value == Decimal("80.5")

    def test_none_value_returns_none_with_label(self):
        value, label = to_display_weight(None, "lb")
        assert value is None
        assert label == "lb"

    def test_unknown_preference_raises(self):
        with pytest.raises(ValueError, match="stone"):
            to_display_weight(Decimal("50"), "stone")

    def test_accepts_int_and_float(self):
        assert to_display_weight(50, "kg")[0] == Decimal("50.0")
        assert to_display_weight(50.0, "kg")[0] == Decimal("50.0")


class TestFromDisplayWeight:
    def test_kg_passthrough(self):
        assert from_display_weight(Decimal("100"), "kg") == Decimal("100.00")

    def test_lb_to_kg_conversion(self):
        assert from_display_weight(Decimal("220.462"), "lb") == Decimal("100.00")

    def test_none_returns_none(self):
        assert from_display_weight(None, "lb") is None

    def test_roundtrip_kg(self):
        display, _ = to_display_weight(Decimal("100.00"), "lb")
        back = from_display_weight(display, "lb")
        assert abs(back - Decimal("100.00")) < Decimal("0.1")

    def test_unknown_preference_raises(self):
        with pytest.raises(ValueError, match="stone"):
            from_display_weight(Decimal("50"), "stone")
