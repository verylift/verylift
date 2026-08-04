"""Tests for the goal-setup threshold/gap domain functions (TASK-17)."""

from decimal import Decimal

import pytest

from scoring.domain.calculator import (
    RepMaxThreshold,
    TierThresholds,
    best_score_for_set,
    estimated_one_rm,
    format_added_weight,
    gap_to_threshold,
    is_bodyweight_added_lift,
    threshold_for_reps,
    tier_thresholds,
)


def _d(x):
    return Decimal(str(x))


class TestEstimatedOneRm:
    """estimated_one_rm is the forward Epley direction, and its inverse is
    threshold_for_reps (TASK-248 — used by the goal-setup history suggester)."""

    def test_reps_one_returns_weight_unchanged(self):
        assert estimated_one_rm(Decimal("100"), 1) == Decimal("100")

    def test_round_trips_with_threshold_for_reps(self):
        w = Decimal("100")
        for reps in range(2, 11):
            one_rm = estimated_one_rm(w, reps)
            back = threshold_for_reps(one_rm, reps)
            assert abs(back - w) < Decimal("0.1")


class TestTierThresholds:
    def test_one_rm_is_multiplier_times_bodyweight(self):
        result = tier_thresholds("Intermediate", Decimal("1.5"), Decimal("100.00"))
        assert result.one_rm_threshold == Decimal("150.00")

    def test_returns_ten_rep_maxes(self):
        result = tier_thresholds("Novice", Decimal("1.0"), Decimal("80.00"))
        assert len(result.rep_maxes) == 10
        assert [rm.reps for rm in result.rep_maxes] == list(range(1, 11))

    def test_one_rm_equals_one_rep_max(self):
        result = tier_thresholds("Elite", Decimal("2.0"), Decimal("90.00"))
        first = result.rep_maxes[0]
        assert first.reps == 1
        assert first.weight == result.one_rm_threshold == Decimal("180.00")

    def test_epley_derivation_for_known_inputs(self):
        # 1RM = 150; 5RM = 150 / (1 + 5/30) = 150 / 1.16667 = 128.57
        result = tier_thresholds("Intermediate", Decimal("1.5"), Decimal("100.00"))
        five_rm = next(rm for rm in result.rep_maxes if rm.reps == 5)
        assert five_rm.weight == Decimal("128.57")

    def test_returns_dataclasses(self):
        result = tier_thresholds("Novice", Decimal("1.0"), Decimal("70.00"))
        assert isinstance(result, TierThresholds)
        assert all(isinstance(rm, RepMaxThreshold) for rm in result.rep_maxes)
        assert result.tier == "Novice"
        assert result.multiplier == Decimal("1.0")


class TestGapToThreshold:
    def test_positive_gap_when_short(self):
        assert gap_to_threshold(Decimal("100.00"), Decimal("150.00")) == Decimal(
            "50.00"
        )

    def test_zero_when_met(self):
        assert gap_to_threshold(Decimal("150.00"), Decimal("150.00")) == Decimal("0.00")

    def test_zero_when_exceeded(self):
        assert gap_to_threshold(Decimal("200.00"), Decimal("150.00")) == Decimal("0.00")


@pytest.mark.django_db
class TestIsBodyweightAddedLift:
    """Bodyweight-added quality now comes from the seeded Lift table."""

    def test_canonical_bodyweight_lifts_are_tagged(self):
        assert is_bodyweight_added_lift("Chin-up")
        assert is_bodyweight_added_lift("Pull-up")
        assert is_bodyweight_added_lift("Dip")

    def test_barbell_lifts_are_not_tagged(self):
        assert not is_bodyweight_added_lift("Back Squat")
        assert not is_bodyweight_added_lift("Bench Press")


class TestBestScoreForSetMapping:
    """best_score_for_set accepts a flat {rep: weight} table for custom goals."""

    def test_mapping_exact_hit_scores_1rm(self):
        table = {n: Decimal("100") for n in range(1, 11)}
        assert best_score_for_set(1, Decimal("100"), table) == (10, 1)

    def test_non_epley_table_read_directly(self):
        # A descending table whose 5RM (90) is far below what Epley would give
        # from a 200 1RM (~171): a 100kg x5 set clears only the flat 5RM here.
        table = {1: Decimal("200"), 2: Decimal("190"), 3: Decimal("180")}
        table.update({4: Decimal("170"), 5: Decimal("90")})
        table.update({n: Decimal("80") for n in range(6, 11)})
        assert best_score_for_set(5, Decimal("100"), table) == (6, 5)

    def test_near_miss_no_longer_scores_without_fuzz_band(self):
        # A 99kg set one kg short of a flat 100kg target no longer counts: the
        # comparison is exact, there is no tolerance fuzz band any more.
        table = {n: Decimal("100") for n in range(1, 11)}
        assert best_score_for_set(1, Decimal("99"), table) is None

    def test_mapping_returns_none_when_no_threshold_met(self):
        table = {n: Decimal("100") for n in range(1, 11)}
        assert best_score_for_set(1, Decimal("50"), table) is None


class TestFormatAddedWeight:
    def test_zero_is_bodyweight(self):
        assert format_added_weight(Decimal("0")) == "BW"
        assert format_added_weight(Decimal("0.00")) == "BW"

    def test_positive_gets_plus_prefix(self):
        assert format_added_weight(Decimal("5")) == "+5"
        assert format_added_weight(Decimal("10.00")) == "+10"

    def test_negative_keeps_minus(self):
        assert format_added_weight(Decimal("-10")) == "-10"

    def test_fractional_values_preserved(self):
        assert format_added_weight(Decimal("2.50")) == "+2.5"
        assert format_added_weight(Decimal("-7.25")) == "-7.25"
