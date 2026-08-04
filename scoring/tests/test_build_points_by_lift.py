"""Tests for build_points_by_lift grouped-bar chart data (TASK-221)."""

from datetime import date

import pytest

from accounts.tests.factories import UserFactory
from challenges.models import Challenge, ChallengeParticipant
from challenges.tests.factories import (
    ChallengeParticipantFactory,
    make_custom_challenge,
)
from scoring.services import build_points_by_lift, get_leaderboard
from scoring.tests.factories import PointEarnEventFactory


@pytest.fixture
def challenge(db):
    # A custom challenge lets us pin the covered lift set explicitly.
    return make_custom_challenge(
        lifts=["Bench", "Squat"], status=Challenge.Status.ACTIVE
    )


def _accept(challenge, user, **kwargs):
    return ChallengeParticipantFactory(
        challenge=challenge,
        user=user,
        invite_status=ChallengeParticipant.InviteStatus.ACCEPTED,
        **kwargs,
    )


class TestBuildPointsByLift:
    def test_labels_are_sorted_covered_lifts(self, challenge):
        data = build_points_by_lift(challenge)
        assert data["labels"] == ["Bench", "Squat"]

    def test_one_series_per_participant_summed_per_lift(self, challenge):
        alice = UserFactory(display_name="Alice")
        bob = UserFactory(display_name="Bob")
        _accept(challenge, alice)
        _accept(challenge, bob)
        PointEarnEventFactory(
            user=alice,
            challenge=challenge,
            lift="Squat",
            points_earned=6,
            is_current_best=True,
        )
        PointEarnEventFactory(
            user=alice,
            challenge=challenge,
            lift="Bench",
            points_earned=4,
            is_current_best=True,
        )
        PointEarnEventFactory(
            user=bob,
            challenge=challenge,
            lift="Squat",
            points_earned=3,
            is_current_best=True,
        )

        data = build_points_by_lift(challenge)

        by_label = {ds["label"]: ds["data"] for ds in data["datasets"]}
        # labels order: ["Bench", "Squat"]
        assert by_label["Alice"] == [4, 6]
        assert by_label["Bob"] == [0, 3]

    def test_unscored_lift_renders_as_zero_bar(self, challenge):
        alice = UserFactory(display_name="Alice")
        _accept(challenge, alice)
        PointEarnEventFactory(
            user=alice,
            challenge=challenge,
            lift="Squat",
            points_earned=5,
            is_current_best=True,
        )

        data = build_points_by_lift(challenge)

        # Bench has no event -> zero-height bar, not a missing category.
        assert data["labels"] == ["Bench", "Squat"]
        assert data["datasets"][0]["data"] == [0, 5]

    def test_only_current_best_counts(self, challenge):
        alice = UserFactory(display_name="Alice")
        _accept(challenge, alice)
        PointEarnEventFactory(
            user=alice,
            challenge=challenge,
            lift="Squat",
            performed_at=date(2024, 1, 1),
            points_earned=2,
            is_current_best=False,
        )
        PointEarnEventFactory(
            user=alice,
            challenge=challenge,
            lift="Squat",
            performed_at=date(2024, 2, 1),
            points_earned=6,
            is_current_best=True,
        )

        data = build_points_by_lift(challenge)

        # superseded 2-point row excluded; only the current-best 6 counts
        assert data["datasets"][0]["data"] == [0, 6]

    def test_per_lift_bars_sum_to_leaderboard_total(self, challenge):
        alice = UserFactory(display_name="Alice")
        _accept(challenge, alice)
        PointEarnEventFactory(
            user=alice,
            challenge=challenge,
            lift="Squat",
            points_earned=6,
            is_current_best=True,
        )
        PointEarnEventFactory(
            user=alice,
            challenge=challenge,
            lift="Bench",
            points_earned=4,
            is_current_best=True,
        )

        data = build_points_by_lift(challenge)
        leaderboard = get_leaderboard(challenge)

        alice_bars = next(
            ds["data"] for ds in data["datasets"] if ds["label"] == "Alice"
        )
        alice_total = next(
            e["total_points"] for e in leaderboard if e["user"].pk == alice.pk
        )
        assert sum(alice_bars) == alice_total == 10

    def test_bailed_participant_excluded(self, challenge):
        alice = UserFactory(display_name="Alice")
        gone = UserFactory(display_name="Gone")
        _accept(challenge, alice)
        _accept(challenge, gone, is_bailed=True)
        PointEarnEventFactory(
            user=alice,
            challenge=challenge,
            lift="Squat",
            points_earned=5,
            is_current_best=True,
        )
        PointEarnEventFactory(
            user=gone,
            challenge=challenge,
            lift="Squat",
            points_earned=9,
            is_current_best=True,
        )

        data = build_points_by_lift(challenge)

        labels = {ds["label"] for ds in data["datasets"]}
        assert "Gone" not in labels
        assert labels == {"Alice"}

    def test_deactivated_user_labelled_former_participant(self, challenge):
        former = UserFactory(display_name="Former", is_active=False)
        _accept(challenge, former)
        PointEarnEventFactory(
            user=former,
            challenge=challenge,
            lift="Squat",
            points_earned=5,
            is_current_best=True,
        )

        data = build_points_by_lift(challenge)

        assert data["datasets"][0]["label"] == "Former Participant"
