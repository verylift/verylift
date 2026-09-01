"""Unit tests for challenges/goal_builders.py (TASK-248 plan step 65).

Covers the STANDARDS/HISTORY suggestion builders and their provenance-record
helpers. ``TestSuggestFromStandards``/``TestSuggestFromHistory`` include the
bidirectional total-load <-> added-weight conversion for bodyweight-added
lifts (Pull-up/Chin-up/Dip) as dedicated tests -- this is one of the two
silent-wrong-answer risks the task carries (an inverted conversion produces a
plausible-looking but wrong ladder, never a loud failure).
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from accounts.tests.factories import UserFactory
from accounts.units import to_display_weight
from challenges.goal_builders import (
    _round_to_increment,
    default_goal_name,
    history_source_detail,
    standards_source_detail,
    suggest_from_history,
    suggest_from_standards,
)
from challenges.models import CustomGoal, RepTargetGoal
from challenges.tests.factories import make_custom_challenge
from fitnessvolt.tests.factories import FitnessVoltStandardCacheFactory
from liftosaur.tests.factories import LiftHistoryFactory
from scoring.domain.calculator import estimated_one_rm, threshold_for_reps

pytestmark = pytest.mark.django_db

SNAPSHOT_VERSION = "2026-06-09"


@pytest.fixture
def user():
    return UserFactory()


class TestSuggestFromStandards:
    def test_builds_flat_table_for_covered_lift(self):
        challenge = make_custom_challenge(lifts=["Back Squat"])
        FitnessVoltStandardCacheFactory(
            population="verified",
            sex="M",
            lift_slug="squat",
            weight_class_kg=Decimal("80"),
            source_snapshot_version=SNAPSHOT_VERSION,
        )
        table, uncovered = suggest_from_standards(
            challenge,
            population="verified",
            snapshot_version=SNAPSHOT_VERSION,
            sex="M",
            bodyweight_kg=Decimal("80"),
            tier="Intermediate",
        )
        assert uncovered == []
        assert set(table) == {"Back Squat"}
        # DEFAULT_PERCENTILES: Intermediate (p50) is exactly 160 at this
        # weight class, i.e. an effective multiplier of 2.0.
        one_rm = Decimal("160.00")
        assert table["Back Squat"][1] == one_rm
        assert table["Back Squat"][8] == threshold_for_reps(one_rm, 8).quantize(
            Decimal("0.01")
        )

    def test_bodyweight_added_lift_converts_total_load_to_added_weight(self):
        # The §1a/§3 conversion under test: a bodyweight-added lift's
        # FitnessVolt cell is a TOTAL-load multiplier, but CustomGoalTarget
        # must store ADDED weight -- getting the sign of this subtraction
        # backwards would silently double the ladder instead of erroring.
        challenge = make_custom_challenge(lifts=["Pull-up"])
        FitnessVoltStandardCacheFactory(
            population="verified",
            sex="M",
            lift_slug="pullup",
            weight_class_kg=Decimal("80"),
            source_snapshot_version=SNAPSHOT_VERSION,
        )
        bodyweight_kg = Decimal("80")
        table, uncovered = suggest_from_standards(
            challenge,
            population="verified",
            snapshot_version=SNAPSHOT_VERSION,
            sex="M",
            bodyweight_kg=bodyweight_kg,
            tier="Intermediate",
        )
        assert uncovered == []
        total_load_one_rm = Decimal("160.00")
        expected_added = total_load_one_rm - bodyweight_kg
        assert table["Pull-up"][1] == expected_added
        expected_8rm_added = (
            threshold_for_reps(total_load_one_rm, 8).quantize(Decimal("0.01"))
            - bodyweight_kg
        )
        assert table["Pull-up"][8] == expected_8rm_added
        # Sanity: the added-weight ladder must be strictly less than the
        # total-load one -- catches an accidental addition instead of
        # subtraction, which would otherwise still "look like a number".
        assert table["Pull-up"][1] < total_load_one_rm

    def test_uncovered_lift_listed_when_no_cache_row(self):
        challenge = make_custom_challenge(lifts=["Back Squat", "Deadlift"])
        FitnessVoltStandardCacheFactory(
            population="verified",
            sex="M",
            lift_slug="squat",
            weight_class_kg=Decimal("80"),
            source_snapshot_version=SNAPSHOT_VERSION,
        )
        table, uncovered = suggest_from_standards(
            challenge,
            population="verified",
            snapshot_version=SNAPSHOT_VERSION,
            sex="M",
            bodyweight_kg=Decimal("80"),
            tier="Intermediate",
        )
        assert set(table) == {"Back Squat"}
        assert uncovered == ["Deadlift"]

    def test_rounding_applied_to_final_added_weight_value(self):
        """FitnessVolt's own percentile interpolation across weight classes
        lands on arbitrary values too, not just history's Epley math (UAT
        feedback) -- rounding must apply to the standards method as well,
        and to the FINAL stored value (added weight for a bodyweight-added
        lift), same reasoning as suggest_from_history.
        """
        challenge = make_custom_challenge(lifts=["Pull-up"])
        FitnessVoltStandardCacheFactory(
            population="verified",
            sex="M",
            lift_slug="pullup",
            weight_class_kg=Decimal("80"),
            source_snapshot_version=SNAPSHOT_VERSION,
        )
        table, uncovered = suggest_from_standards(
            challenge,
            population="verified",
            snapshot_version=SNAPSHOT_VERSION,
            sex="M",
            bodyweight_kg=Decimal("81.30"),
            tier="Intermediate",
            rounding_amount=Decimal("2.5"),
            rounding_unit="kg",
        )
        assert uncovered == []
        for rep, weight in table["Pull-up"].items():
            assert weight % Decimal("2.5") == 0, f"[{rep}] = {weight}"

    def test_missing_population_returns_empty_table_all_uncovered(self):
        challenge = make_custom_challenge(lifts=["Back Squat", "Deadlift"])
        table, uncovered = suggest_from_standards(
            challenge,
            population="",
            snapshot_version=SNAPSHOT_VERSION,
            sex="M",
            bodyweight_kg=Decimal("80"),
            tier="Intermediate",
        )
        assert table == {}
        assert uncovered == ["Back Squat", "Deadlift"]

    def test_lift_not_configured_on_challenge_is_ignored(self):
        # A cached cell for a lift the challenge doesn't cover must never
        # leak into the table or the uncovered list.
        challenge = make_custom_challenge(lifts=["Back Squat"])
        FitnessVoltStandardCacheFactory(
            population="verified",
            sex="M",
            lift_slug="deadlift",
            weight_class_kg=Decimal("80"),
            source_snapshot_version=SNAPSHOT_VERSION,
        )
        table, uncovered = suggest_from_standards(
            challenge,
            population="verified",
            snapshot_version=SNAPSHOT_VERSION,
            sex="M",
            bodyweight_kg=Decimal("80"),
            tier="Intermediate",
        )
        assert table == {}
        assert uncovered == ["Back Squat"]

    def test_only_requested_tier_is_included(self):
        challenge = make_custom_challenge(lifts=["Back Squat"])
        FitnessVoltStandardCacheFactory(
            population="verified",
            sex="M",
            lift_slug="squat",
            weight_class_kg=Decimal("80"),
            source_snapshot_version=SNAPSHOT_VERSION,
        )
        table, _ = suggest_from_standards(
            challenge,
            population="verified",
            snapshot_version=SNAPSHOT_VERSION,
            sex="M",
            bodyweight_kg=Decimal("80"),
            tier="Elite",
        )
        # Elite (p95) is exactly 205 at this weight class -- a different
        # ladder from Intermediate's 160, proving the tier filter is applied.
        assert table["Back Squat"][1] == Decimal("205.00")


class TestStandardsSourceDetail:
    def test_returns_expected_shape_with_bodyweight_as_string(self):
        detail = standards_source_detail(
            population="verified",
            snapshot_version=SNAPSHOT_VERSION,
            tier="Intermediate",
            sex="M",
            bodyweight_kg=Decimal("80.00"),
        )
        assert detail == {
            "population": "verified",
            "snapshot_version": SNAPSHOT_VERSION,
            "tier": "Intermediate",
            "sex": "M",
            "bodyweight_kg": "80.00",
            "rounding_amount": None,
            "rounding_unit": None,
        }
        assert isinstance(detail["bodyweight_kg"], str)

    def test_records_rounding_choice_as_amount_and_unit(self):
        detail = standards_source_detail(
            population="verified",
            snapshot_version=SNAPSHOT_VERSION,
            tier="Intermediate",
            sex="M",
            bodyweight_kg=Decimal("80.00"),
            rounding_amount=Decimal("5"),
            rounding_unit="lb",
        )
        assert detail["rounding_amount"] == "5"
        assert detail["rounding_unit"] == "lb"


class TestHistorySourceDetail:
    def test_carries_no_sex_or_bodyweight(self):
        detail = history_source_detail(uplift=0.05, lookback_days=90)
        assert detail == {
            "uplift": 0.05,
            "lookback_days": 90,
            "rounding_amount": None,
            "rounding_unit": None,
        }
        assert "sex" not in detail
        assert "bodyweight_kg" not in detail

    def test_records_rounding_choice_as_amount_and_unit(self):
        detail = history_source_detail(
            uplift=0.05,
            lookback_days=90,
            rounding_amount=Decimal("2.50"),
            rounding_unit="kg",
        )
        assert detail["rounding_amount"] == "2.50"
        assert detail["rounding_unit"] == "kg"


class TestRoundToIncrement:
    def test_none_increment_returns_value_unchanged(self):
        assert _round_to_increment(Decimal("87.34"), None) == Decimal("87.34")

    def test_non_positive_increment_returns_value_unchanged(self):
        assert _round_to_increment(Decimal("87.34"), Decimal("0")) == Decimal("87.34")

    def test_rounds_to_nearest_multiple(self):
        assert _round_to_increment(Decimal("87.34"), Decimal("2.5")) == Decimal("87.50")
        assert _round_to_increment(Decimal("86.20"), Decimal("2.5")) == Decimal("85.00")

    def test_exact_halfway_rounds_up_away_from_zero(self):
        # 88.75 is exactly halfway between 87.5 and 90 on a 2.5 grid.
        assert _round_to_increment(Decimal("88.75"), Decimal("2.5")) == Decimal("90.00")

    def test_negative_weight_rounds_away_from_zero_on_ties(self):
        # An assisted-setup added-weight rung can be negative; -8.75 is
        # exactly halfway between -7.5 and -10 on a 2.5 grid.
        assert _round_to_increment(Decimal("-8.75"), Decimal("2.5")) == Decimal(
            "-10.00"
        )

    def test_already_on_grid_is_unchanged(self):
        assert _round_to_increment(Decimal("87.5"), Decimal("2.5")) == Decimal("87.50")


class TestSuggestFromHistory:
    def test_uses_best_e1rm_in_window_uplifted_and_expanded(self, user):
        challenge = make_custom_challenge(lifts=["Back Squat"])
        recent = (datetime.now(tz=UTC) - timedelta(days=10)).date()
        LiftHistoryFactory(
            user=user,
            lift="Back Squat",
            performed_at=recent,
            reps=5,
            weight_kg=Decimal("100.00"),
        )
        table, needs_decision, assisted_only = suggest_from_history(
            user, challenge, uplift=0.10, lookback_days=90
        )
        assert needs_decision == []
        assert assisted_only == []
        best_one_rm = estimated_one_rm(Decimal("100.00"), 5)
        uplifted = (best_one_rm * Decimal("1.10")).quantize(Decimal("0.01"))
        assert table["Back Squat"][1] == uplifted
        assert table["Back Squat"][8] == threshold_for_reps(uplifted, 8).quantize(
            Decimal("0.01")
        )

    def test_picks_heaviest_e1rm_not_most_recent(self, user):
        challenge = make_custom_challenge(lifts=["Back Squat"])
        cutoff_ok = (datetime.now(tz=UTC) - timedelta(days=5)).date()
        LiftHistoryFactory(
            user=user,
            lift="Back Squat",
            performed_at=cutoff_ok,
            reps=1,
            weight_kg=Decimal("50.00"),
        )
        heavier_date = (datetime.now(tz=UTC) - timedelta(days=20)).date()
        LiftHistoryFactory(
            user=user,
            lift="Back Squat",
            performed_at=heavier_date,
            reps=1,
            weight_kg=Decimal("150.00"),
        )
        table, _, _ = suggest_from_history(
            user, challenge, uplift=0.0, lookback_days=90
        )
        assert table["Back Squat"][1] == Decimal("150.00")

    def test_rows_outside_lookback_window_excluded(self, user):
        challenge = make_custom_challenge(lifts=["Back Squat"])
        too_old = (datetime.now(tz=UTC) - timedelta(days=100)).date()
        LiftHistoryFactory(
            user=user,
            lift="Back Squat",
            performed_at=too_old,
            reps=1,
            weight_kg=Decimal("150.00"),
        )
        table, needs_decision, _ = suggest_from_history(
            user, challenge, uplift=0.0, lookback_days=90
        )
        assert "Back Squat" not in table
        assert needs_decision == ["Back Squat"]

    def test_lift_with_no_history_needs_decision(self, user):
        challenge = make_custom_challenge(lifts=["Back Squat"])
        table, needs_decision, assisted_only = suggest_from_history(
            user, challenge, uplift=0.0, lookback_days=90
        )
        assert table == {}
        assert needs_decision == ["Back Squat"]
        assert assisted_only == []

    def test_reps_capped_at_ten_for_e1rm(self, user):
        challenge = make_custom_challenge(lifts=["Back Squat"])
        recent = (datetime.now(tz=UTC) - timedelta(days=10)).date()
        LiftHistoryFactory(
            user=user,
            lift="Back Squat",
            performed_at=recent,
            reps=15,
            weight_kg=Decimal("100.00"),
        )
        table, _, _ = suggest_from_history(
            user, challenge, uplift=0.0, lookback_days=90
        )
        capped_one_rm = estimated_one_rm(Decimal("100.00"), 10)
        assert table["Back Squat"][1] == capped_one_rm

    def test_bodyweight_added_lift_total_load_then_subtracts_back(self, user):
        # The other half of the bidirectional-conversion risk: history e1RM
        # for a bodyweight-added lift must be computed on TOTAL load
        # (bodyweight + recorded added weight), then the final ladder must
        # have bodyweight subtracted back off -- getting either half of this
        # round trip wrong produces a plausible but incorrect ladder.
        challenge = make_custom_challenge(lifts=["Pull-up"])
        recent = (datetime.now(tz=UTC) - timedelta(days=10)).date()
        LiftHistoryFactory(
            user=user,
            lift="Pull-up",
            performed_at=recent,
            reps=5,
            weight_kg=Decimal("20.00"),
        )
        bodyweight_kg = Decimal("80.00")
        table, needs_decision, assisted_only = suggest_from_history(
            user,
            challenge,
            bodyweight_kg=bodyweight_kg,
            uplift=0.0,
            lookback_days=90,
        )
        assert needs_decision == []
        assert assisted_only == []
        total_load_one_rm = estimated_one_rm(Decimal("100.00"), 5)  # 80 + 20
        expected_added_one_rm = total_load_one_rm - bodyweight_kg
        assert table["Pull-up"][1] == expected_added_one_rm
        # Never the degenerate all-zero ladder a naive added-weight-only e1RM
        # would produce (estimated_one_rm(20, 5) computed without bodyweight).
        naive_wrong = estimated_one_rm(Decimal("20.00"), 5) - bodyweight_kg
        assert table["Pull-up"][1] != naive_wrong

    def test_bodyweight_added_lift_without_bodyweight_needs_decision(self, user):
        challenge = make_custom_challenge(lifts=["Pull-up"])
        recent = (datetime.now(tz=UTC) - timedelta(days=10)).date()
        LiftHistoryFactory(
            user=user,
            lift="Pull-up",
            performed_at=recent,
            reps=5,
            weight_kg=Decimal("20.00"),
        )
        table, needs_decision, assisted_only = suggest_from_history(
            user, challenge, bodyweight_kg=None, uplift=0.0, lookback_days=90
        )
        assert "Pull-up" not in table
        assert needs_decision == ["Pull-up"]
        assert assisted_only == []

    def test_bodyweight_added_lift_all_assisted_flagged_needs_decision(self, user):
        challenge = make_custom_challenge(lifts=["Pull-up"])
        recent = (datetime.now(tz=UTC) - timedelta(days=10)).date()
        LiftHistoryFactory(
            user=user,
            lift="Pull-up",
            performed_at=recent,
            reps=5,
            weight_kg=Decimal("60.00"),
            equipment="Leverage Machine",
        )
        table, needs_decision, assisted_only = suggest_from_history(
            user,
            challenge,
            bodyweight_kg=Decimal("80.00"),
            uplift=0.0,
            lookback_days=90,
        )
        assert "Pull-up" not in table
        assert needs_decision == ["Pull-up"]
        assert assisted_only == ["Pull-up"]

    def test_assisted_rows_skipped_when_usable_row_also_present(self, user):
        challenge = make_custom_challenge(lifts=["Pull-up"])
        recent = (datetime.now(tz=UTC) - timedelta(days=10)).date()
        LiftHistoryFactory(
            user=user,
            lift="Pull-up",
            performed_at=recent,
            reps=5,
            weight_kg=Decimal("999.00"),
            equipment="Leverage Machine",
        )
        LiftHistoryFactory(
            user=user,
            lift="Pull-up",
            performed_at=recent,
            reps=5,
            weight_kg=Decimal("20.00"),
        )
        bodyweight_kg = Decimal("80.00")
        table, needs_decision, assisted_only = suggest_from_history(
            user,
            challenge,
            bodyweight_kg=bodyweight_kg,
            uplift=0.0,
            lookback_days=90,
        )
        assert needs_decision == []
        assert assisted_only == []
        total_load_one_rm = estimated_one_rm(Decimal("100.00"), 5)
        assert table["Pull-up"][1] == total_load_one_rm - bodyweight_kg

    def test_rounding_increment_applied_to_final_stored_value(self, user):
        """Rounding must apply to what's actually stored (added weight for a
        bodyweight-added lift), not an intermediate total-load figure the
        participant never sees -- a rounded total-load number would not
        necessarily still be "clean" once bodyweight is subtracted back off.
        """
        challenge = make_custom_challenge(lifts=["Back Squat", "Pull-up"])
        recent = (datetime.now(tz=UTC) - timedelta(days=10)).date()
        LiftHistoryFactory(
            user=user,
            lift="Back Squat",
            performed_at=recent,
            reps=5,
            weight_kg=Decimal("101.30"),
        )
        LiftHistoryFactory(
            user=user,
            lift="Pull-up",
            performed_at=recent,
            reps=5,
            weight_kg=Decimal("21.30"),
        )
        table, needs_decision, assisted_only = suggest_from_history(
            user,
            challenge,
            bodyweight_kg=Decimal("81.30"),
            uplift=0.10,
            lookback_days=90,
            rounding_amount=Decimal("2.5"),
            rounding_unit="kg",
        )
        assert needs_decision == []
        assert assisted_only == []
        for lift in ("Back Squat", "Pull-up"):
            for rep, weight in table[lift].items():
                assert weight % Decimal("2.5") == 0, f"{lift}[{rep}] = {weight}"

    def test_lb_rounding_produces_clean_lb_values_not_kg_drift(self, user):
        """UAT regression: choosing "5 lb" produced results like 204.3 lb --
        because the increment was converted to kg (from_display_weight's 0.01
        kg precision) BEFORE rounding, and multiples of that approximate kg
        grid drift away from clean lb multiples as the multiplier grows.
        Rounding must happen in lb directly when lb was the chosen unit.
        """
        challenge = make_custom_challenge(lifts=["Back Squat"])
        recent = (datetime.now(tz=UTC) - timedelta(days=10)).date()
        LiftHistoryFactory(
            user=user,
            lift="Back Squat",
            performed_at=recent,
            reps=1,
            weight_kg=Decimal("110.00"),
        )
        table, _, _ = suggest_from_history(
            user,
            challenge,
            uplift=0.0,
            lookback_days=90,
            rounding_amount=Decimal("5"),
            rounding_unit="lb",
        )
        for rep, weight_kg in table["Back Squat"].items():
            display_lb, _ = to_display_weight(weight_kg, "lb")
            assert display_lb % Decimal("5") == 0, (
                f"1RM[{rep}] = {weight_kg} kg -> {display_lb} lb, not a clean "
                "5 lb multiple"
            )

    def test_no_rounding_keeps_raw_precision(self, user):
        challenge = make_custom_challenge(lifts=["Back Squat"])
        recent = (datetime.now(tz=UTC) - timedelta(days=10)).date()
        LiftHistoryFactory(
            user=user,
            lift="Back Squat",
            performed_at=recent,
            reps=5,
            weight_kg=Decimal("101.30"),
        )
        table, _, _ = suggest_from_history(
            user,
            challenge,
            uplift=0.10,
            lookback_days=90,
            rounding_amount=None,
        )
        best_one_rm = estimated_one_rm(Decimal("101.30"), 5)
        uplifted = (best_one_rm * Decimal("1.10")).quantize(Decimal("0.01"))
        assert table["Back Squat"][1] == uplifted


class TestDefaultGoalName:
    def test_standards_name(self):
        name = default_goal_name(
            CustomGoal.SourceMethod.STANDARDS,
            population="verified",
            tier="Intermediate",
        )
        assert name == "Verified Intermediate"

    def test_history_name_with_uplift(self):
        name = default_goal_name(CustomGoal.SourceMethod.HISTORY, uplift=0.05)
        assert name == "Suggested from history (+5%)"

    def test_history_name_without_uplift(self):
        name = default_goal_name(CustomGoal.SourceMethod.HISTORY)
        assert name == "Suggested from history"

    def test_custom_name(self):
        assert default_goal_name(CustomGoal.SourceMethod.CUSTOM) == "Custom Goal"

    def test_json_name(self):
        assert default_goal_name(CustomGoal.SourceMethod.JSON) == "Custom Goal"

    def test_rep_target_custom_name(self):
        assert default_goal_name(RepTargetGoal.SourceMethod.CUSTOM) == "Custom Goal"

    def test_rep_target_history_name_with_uplift(self):
        name = default_goal_name(RepTargetGoal.SourceMethod.HISTORY, uplift=0.05)
        assert name == "Suggested from history (+5%)"

    def test_accepts_a_plain_string_method(self):
        assert default_goal_name("custom") == "Custom Goal"
