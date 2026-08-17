"""Tests for build_career_stats — the dashboard hero card aggregates (TASK-246)."""

from datetime import date
from decimal import Decimal

import pytest

from accounts.tests.factories import UserFactory
from challenges.models import Challenge, ChallengeParticipant
from challenges.tests.factories import (
    ChallengeFactory,
    ChallengeParticipantFactory,
)
from scoring.services import build_career_stats
from scoring.tests.factories import PointEarnEventFactory


@pytest.fixture
def user(db):
    return UserFactory()


def _accepted(user, challenge, **kwargs):
    return ChallengeParticipantFactory(
        challenge=challenge,
        user=user,
        invite_status=ChallengeParticipant.InviteStatus.ACCEPTED,
        **kwargs,
    )


class TestNoHistory:
    def test_empty_career(self, user):
        stats = build_career_stats(user)
        assert stats["challenges_played"] == 0
        assert stats["wins"] == 0
        assert stats["total_points"] == 0
        assert stats["points_per_week"] is None
        assert stats["avg_points"] is None
        assert stats["lifts"] == []
        assert stats["has_history"] is False


class TestChallengesPlayed:
    def test_counts_accepted_non_draft(self, user):
        _accepted(user, ChallengeFactory(status=Challenge.Status.ACTIVE))
        _accepted(user, ChallengeFactory(status=Challenge.Status.COMPLETED))
        stats = build_career_stats(user)
        assert stats["challenges_played"] == 2
        assert stats["has_history"] is True

    def test_excludes_drafts_and_non_accepted(self, user):
        _accepted(user, ChallengeFactory(status=Challenge.Status.DRAFT))
        ChallengeParticipantFactory(
            challenge=ChallengeFactory(status=Challenge.Status.ACTIVE),
            user=user,
            invite_status=ChallengeParticipant.InviteStatus.INVITED,
        )
        ChallengeParticipantFactory(
            challenge=ChallengeFactory(status=Challenge.Status.ACTIVE),
            user=user,
            invite_status=ChallengeParticipant.InviteStatus.DECLINED,
        )
        assert build_career_stats(user)["challenges_played"] == 0

    def test_bailed_participation_still_counts(self, user):
        _accepted(
            user,
            ChallengeFactory(status=Challenge.Status.ACTIVE),
            is_bailed=True,
        )
        assert build_career_stats(user)["challenges_played"] == 1


class TestWins:
    def test_rank_one_in_completed_counts(self, user):
        challenge = ChallengeFactory(status=Challenge.Status.COMPLETED)
        _accepted(user, challenge)
        loser = UserFactory()
        _accepted(loser, challenge)
        PointEarnEventFactory(user=user, challenge=challenge, points_earned=50)
        PointEarnEventFactory(user=loser, challenge=challenge, points_earned=10)
        assert build_career_stats(user)["wins"] == 1

    def test_second_place_is_not_a_win(self, user):
        challenge = ChallengeFactory(status=Challenge.Status.COMPLETED)
        _accepted(user, challenge)
        winner = UserFactory()
        _accepted(winner, challenge)
        PointEarnEventFactory(user=user, challenge=challenge, points_earned=10)
        PointEarnEventFactory(user=winner, challenge=challenge, points_earned=50)
        assert build_career_stats(user)["wins"] == 0

    def test_leading_an_active_challenge_is_not_a_win(self, user):
        challenge = ChallengeFactory(status=Challenge.Status.ACTIVE)
        _accepted(user, challenge)
        PointEarnEventFactory(user=user, challenge=challenge, points_earned=50)
        assert build_career_stats(user)["wins"] == 0


class TestPointTotals:
    def test_total_points_sums_current_best_only(self, user):
        challenge = ChallengeFactory(status=Challenge.Status.ACTIVE)
        _accepted(user, challenge)
        PointEarnEventFactory(
            user=user, challenge=challenge, points_earned=6, is_current_best=True
        )
        PointEarnEventFactory(
            user=user, challenge=challenge, points_earned=99, is_current_best=False
        )
        assert build_career_stats(user)["total_points"] == 6

    def test_avg_points_per_challenge(self, user):
        first = ChallengeFactory(status=Challenge.Status.ACTIVE)
        second = ChallengeFactory(status=Challenge.Status.ACTIVE)
        _accepted(user, first)
        _accepted(user, second)
        PointEarnEventFactory(user=user, challenge=first, points_earned=10)
        PointEarnEventFactory(user=user, challenge=second, points_earned=5)
        assert build_career_stats(user)["avg_points"] == Decimal("7.5")

    def test_velocity_over_event_span(self, user):
        challenge = ChallengeFactory(status=Challenge.Status.ACTIVE)
        _accepted(user, challenge)
        PointEarnEventFactory(
            user=user,
            challenge=challenge,
            points_earned=10,
            performed_at=date(2026, 1, 1),
        )
        PointEarnEventFactory(
            user=user,
            challenge=challenge,
            points_earned=20,
            performed_at=date(2026, 1, 15),
        )
        # 30 points over a 14-day span = 15.0 points/week
        assert build_career_stats(user)["points_per_week"] == Decimal("15.0")

    def test_velocity_floors_at_one_week(self, user):
        challenge = ChallengeFactory(status=Challenge.Status.ACTIVE)
        _accepted(user, challenge)
        PointEarnEventFactory(
            user=user,
            challenge=challenge,
            points_earned=10,
            performed_at=date(2026, 1, 1),
        )
        assert build_career_stats(user)["points_per_week"] == Decimal("10.0")


class TestLiftComparison:
    def test_first_vs_latest_per_lift(self, db):
        user = UserFactory(unit_preference="kg")
        challenge = ChallengeFactory(status=Challenge.Status.ACTIVE)
        _accepted(user, challenge)
        PointEarnEventFactory(
            user=user,
            challenge=challenge,
            lift="Squat",
            weight=Decimal("60.00"),
            performed_at=date(2026, 1, 1),
        )
        PointEarnEventFactory(
            user=user,
            challenge=challenge,
            lift="Squat",
            weight=Decimal("100.00"),
            performed_at=date(2026, 3, 1),
        )
        PointEarnEventFactory(
            user=user,
            challenge=challenge,
            lift="Bench Press",
            weight=Decimal("40.00"),
            performed_at=date(2026, 2, 1),
        )
        lifts = build_career_stats(user)["lifts"]
        assert [row["lift"] for row in lifts] == ["Bench Press", "Squat"]
        squat = lifts[1]
        assert squat["first_weight"] == Decimal("60.0")
        assert squat["latest_weight"] == Decimal("100.0")
        assert squat["first_date"] == date(2026, 1, 1)
        assert squat["latest_date"] == date(2026, 3, 1)
        assert squat["unit"] == "kg"

    def test_weights_convert_to_lb_preference(self, db):
        user = UserFactory(unit_preference="lb")
        challenge = ChallengeFactory(status=Challenge.Status.ACTIVE)
        _accepted(user, challenge)
        PointEarnEventFactory(
            user=user,
            challenge=challenge,
            lift="Squat",
            weight=Decimal("100.00"),
            performed_at=date(2026, 1, 1),
        )
        row = build_career_stats(user)["lifts"][0]
        assert row["unit"] == "lb"
        assert row["first_weight"] == Decimal("220.5")

    def test_zero_point_events_excluded(self, user):
        challenge = ChallengeFactory(status=Challenge.Status.ACTIVE)
        _accepted(user, challenge)
        PointEarnEventFactory(
            user=user,
            challenge=challenge,
            lift="Squat",
            points_earned=0,
            performed_at=date(2026, 1, 1),
        )
        stats = build_career_stats(user)
        assert stats["lifts"] == []
        assert stats["points_per_week"] is None

    def test_merges_same_lift_across_challenges(self, db):
        user = UserFactory(unit_preference="kg")
        first = ChallengeFactory(status=Challenge.Status.COMPLETED)
        second = ChallengeFactory(status=Challenge.Status.ACTIVE)
        _accepted(user, first)
        _accepted(user, second)
        PointEarnEventFactory(
            user=user,
            challenge=first,
            lift="Squat",
            weight=Decimal("60.00"),
            performed_at=date(2025, 1, 1),
        )
        PointEarnEventFactory(
            user=user,
            challenge=second,
            lift="Squat",
            weight=Decimal("120.00"),
            performed_at=date(2026, 1, 1),
        )
        lifts = build_career_stats(user)["lifts"]
        assert len(lifts) == 1
        assert lifts[0]["first_weight"] == Decimal("60.0")
        assert lifts[0]["latest_weight"] == Decimal("120.0")
