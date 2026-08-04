"""Tests for threshold_for_reps and best_score_for_set (migrated from BDD)."""

from decimal import Decimal

import pytest

from scoring.domain.calculator import best_score_for_set, threshold_for_reps


class TestThresholdForReps:
    def test_reps_1_special_case_returns_full_1rm(self):
        # reps=1 special case returns the full 1RM threshold.
        assert threshold_for_reps(Decimal(140), 1) == Decimal(140)


class TestBestScoreForSet:
    @pytest.mark.parametrize(
        ("performed_reps", "performed_weight", "expected_points", "expected_reps"),
        [
            # basic point scale
            (1, 140, 10, 1),  # a 1RM performance earns 10 points
            (10, 105, 1, 10),  # a 10RM performance earns 1 point
            (5, 120, 6, 5),  # a 5RM performance earns 6 points
            # over-performance: highest threshold satisfied wins
            # 8 reps at the 5RM weight clears n=5 but not n<5 (weight too low).
            (8, 120, 6, 5),
            # 122 exceeds the 5RM threshold (120) but falls short of the 4RM
            # threshold (~123.5), so with 5 reps the best tier is still 5RM.
            (5, 122, 6, 5),
            # exact boundary: exactly meeting the 10RM threshold earns 1 point
            (10, 105, 1, 10),
            # reps > 10 cap: effective_reps = min(12, 10) = 10
            # weight=140 satisfies the 1RM tier -> 10 points (cap doesn't reduce)
            (12, 140, 10, 1),
            # weight=105 satisfies only the 10RM tier
            (12, 105, 1, 10),
            # exact comparison (TASK-135): a set exactly on the threshold scores
            (10, 105, 1, 10),
        ],
    )
    def test_best_score_for_set_scores(
        self, performed_reps, performed_weight, expected_points, expected_reps
    ):
        result = best_score_for_set(
            performed_reps, Decimal(performed_weight), Decimal(140)
        )
        assert result == (expected_points, expected_reps)

    @pytest.mark.parametrize(
        ("performed_reps", "performed_weight"),
        [
            (5, 80),  # under-performance earns no score
            (3, 80),  # set that meets no threshold at all returns None
            # near-miss (TASK-135): 104 misses the 10RM threshold (105) by 1 and
            # there is no fuzz band, so it no longer scores.
            (10, 104),
        ],
    )
    def test_best_score_for_set_returns_none(self, performed_reps, performed_weight):
        result = best_score_for_set(
            performed_reps, Decimal(performed_weight), Decimal(140)
        )
        assert result is None
