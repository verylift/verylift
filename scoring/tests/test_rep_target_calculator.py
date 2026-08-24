"""Tests for scoring.domain.calculator.best_score_for_rep_target (issue #85)."""

from decimal import Decimal

import pytest

from scoring.domain.calculator import best_score_for_rep_target


def _d(x):
    return Decimal(str(x))


class TestBestScoreForRepTarget:
    def test_weight_below_target_is_a_gate_failure(self):
        # Weight is a gate, not a tradeoff axis: no amount of extra reps
        # substitutes for missing weight (unlike Classic).
        assert best_score_for_rep_target(100, _d("10"), 20, _d("15")) is None

    def test_weight_exactly_at_target_passes_the_gate(self):
        assert best_score_for_rep_target(20, _d("15"), 20, _d("15")) == 10

    def test_full_target_reps_scores_ten(self):
        assert best_score_for_rep_target(20, _d("0"), 20, _d("0")) == 10

    def test_extra_reps_beyond_target_earn_nothing_more(self):
        assert best_score_for_rep_target(25, _d("0"), 20, _d("0")) == 10

    @pytest.mark.parametrize(
        "performed_reps,target_reps,expected_points",
        [
            (10, 20, 5),  # half the target reps -> exactly half the points
            (12, 20, 6),  # 12/20 * 10 = 6 exactly (issue #85's own example)
            (1, 5, 2),  # 1/5 * 10 = 2 exactly
            (2, 5, 4),  # 2/5 * 10 = 4 exactly
        ],
    )
    def test_scales_linearly_with_reps_toward_target(
        self, performed_reps, target_reps, expected_points
    ):
        points = best_score_for_rep_target(
            performed_reps, _d("0"), target_reps, _d("0")
        )
        assert points == expected_points

    def test_round_half_up_not_bankers_rounding(self):
        # 3/8 * 10 = 3.75 -> rounds up to 4, not down to a banker's-rounding 4
        # (this particular fraction doesn't distinguish the two -- picked for
        # a case that does: 1/8 * 10 = 1.25 -> 1, 5/8*10=6.25->6).
        assert best_score_for_rep_target(1, _d("0"), 8, _d("0")) == 1
        assert best_score_for_rep_target(5, _d("0"), 8, _d("0")) == 6

    def test_single_qualifying_rep_can_round_to_zero(self):
        # Open question #3 (issue #85): accepted by design -- the weight gate
        # alone already signals "on the board", and best-set-replaces-old
        # means a later, better set overwrites this upward.
        assert best_score_for_rep_target(1, _d("0"), 999, _d("0")) == 0

    def test_points_never_exceed_ten(self):
        for reps in (20, 50, 999):
            assert best_score_for_rep_target(reps, _d("0"), 20, _d("0")) == 10

    def test_bodyweight_added_lift_uses_added_weight_convention(self):
        # Negative target = assisted; met by an equal-or-lighter assisted set,
        # matching Classic's added-weight sign convention.
        assert best_score_for_rep_target(10, _d("-5"), 10, _d("-5")) == 10
        assert best_score_for_rep_target(10, _d("-10"), 10, _d("-5")) is None
