"""Tests for the Challenge equipment/scoring config fields (TASK-119).

Covers plate_unit / smallest_plate: their fixed defaults and canonical kg
storage, and the guarantee that the viewing user's personal unit_preference
(never the challenge's plate_unit) governs how weights are displayed. These
fields are no longer creator-configurable at creation time (TASK-247 removed
the units/rounding "Advanced" drawer from the create wizard) -- every new
challenge gets the fixed defaults asserted in TestConfigDefaults; the fields
still exist on the model because scoring display (_kg_to_display) still reads
them. bodyweight_tolerance was removed entirely by TASK-248: every target is
now a static, hand-authored weight with no bodyweight anchor to tolerate
drift around.
"""

from decimal import Decimal

from accounts.units import to_display_weight
from challenges.models import Challenge
from challenges.services import _kg_to_display, _round_to_increment
from challenges.tests.factories import ChallengeFactory


class TestConfigDefaults:
    def test_model_defaults_are_lb_unit_with_canonical_kg_values(self, db):
        comp = ChallengeFactory()
        assert comp.plate_unit == Challenge.PlateUnit.LB
        assert comp.smallest_plate == Decimal("1.25")


class TestDisplayIsIndependentOfPlateUnit:
    def test_plate_unit_does_not_affect_display_unit(self, db):
        # Two challenges with the SAME canonical smallest_plate but different
        # plate_unit must display an identical weight identically — plate_unit is
        # input-interpretation only, never a display override.
        comp_lb = ChallengeFactory(
            plate_unit=Challenge.PlateUnit.LB, smallest_plate=Decimal("1.25")
        )
        comp_kg = ChallengeFactory(
            plate_unit=Challenge.PlateUnit.KG, smallest_plate=Decimal("1.25")
        )
        for comp in (comp_lb, comp_kg):
            assert _kg_to_display(Decimal("100.00"), "kg", comp) == Decimal("100.0")
            assert (
                _kg_to_display(Decimal("100.00"), "lb", comp)
                == to_display_weight(Decimal("100.00"), "lb")[0]
            )

    def test_viewer_unit_preference_governs_display(self, db):
        comp = ChallengeFactory(smallest_plate=Decimal("1.25"))
        kg_view = _kg_to_display(Decimal("100.00"), "kg", comp)
        lb_view = _kg_to_display(Decimal("100.00"), "lb", comp)
        assert kg_view == Decimal("100.0")
        assert lb_view == to_display_weight(Decimal("100.00"), "lb")[0]
        assert kg_view != lb_view


class TestPlateRounding:
    def test_snaps_to_twice_smallest_plate_grid(self, db):
        comp = ChallengeFactory(smallest_plate=Decimal("0.5"))  # 1.0 kg grid
        assert _kg_to_display(Decimal("100.40"), "kg", comp) == Decimal("100.0")
        assert _kg_to_display(Decimal("100.60"), "kg", comp) == Decimal("101.0")

    def test_none_weight_returns_none(self, db):
        comp = ChallengeFactory()
        assert _kg_to_display(None, "kg", comp) is None

    def test_round_to_increment_zero_increment_is_a_noop(self):
        assert _round_to_increment(Decimal("83.27"), Decimal("0")) == Decimal("83.3")
