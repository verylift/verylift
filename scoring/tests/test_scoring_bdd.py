"""Tests for points_for_rep_count and satisfies_threshold (migrated from BDD)."""

import pytest

from scoring.domain.calculator import points_for_rep_count, satisfies_threshold


class TestPointsForRepCount:
    @pytest.mark.parametrize(
        ("reps", "expected"),
        [
            (1, 10),  # a 1RM earns 10 points
            (10, 1),  # a 10RM earns 1 point
            (5, 6),  # a 5RM earns 6 points
            (11, 0),  # an out-of-range rep count earns 0 points
        ],
    )
    def test_points_for_rep_count(self, reps, expected):
        assert points_for_rep_count(reps) == expected


class TestSatisfiesThreshold:
    @pytest.mark.parametrize(
        (
            "performed_reps",
            "performed_weight",
            "threshold_reps",
            "threshold_weight",
            "expected",
        ),
        [
            # a performed set that meets the threshold satisfies it
            (5, 100, 5, 100, True),
            # a performed set with higher weight satisfies the threshold
            (5, 110, 5, 100, True),
            # a performed set with lower weight does not satisfy the threshold
            (5, 90, 5, 100, False),
            # a near-miss just below the threshold does not satisfy it
            (5, 99, 5, 100, False),
        ],
    )
    def test_satisfies_threshold(
        self,
        performed_reps,
        performed_weight,
        threshold_reps,
        threshold_weight,
        expected,
    ):
        assert (
            satisfies_threshold(
                performed_reps, performed_weight, threshold_reps, threshold_weight
            )
            is expected
        )
