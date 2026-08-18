"""Tests for build_recent_scoring_activity feed data (TASK-221)."""

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from accounts.models import User
from accounts.tests.factories import UserFactory
from challenges.models import Challenge, ChallengeParticipant
from challenges.tests.factories import (
    ChallengeFactory,
    ChallengeParticipantFactory,
)
from scoring.services import build_recent_scoring_activity
from scoring.tests.factories import PointEarnEventFactory


@pytest.fixture
def challenge(db):
    return ChallengeFactory(status=Challenge.Status.ACTIVE)


@pytest.fixture
def viewer(db):
    return UserFactory(display_name="Viewer", unit_preference=User.UnitPreference.KG)


def _accept(challenge, user, **kwargs):
    return ChallengeParticipantFactory(
        challenge=challenge,
        user=user,
        invite_status=ChallengeParticipant.InviteStatus.ACCEPTED,
        **kwargs,
    )


class TestBuildRecentScoringActivity:
    def test_empty_state_is_empty_list(self, challenge, viewer):
        assert build_recent_scoring_activity(challenge, viewer) == []

    def test_row_shape_and_fields(self, challenge, viewer):
        alice = UserFactory(display_name="Alice")
        _accept(challenge, alice)
        PointEarnEventFactory(
            user=alice,
            challenge=challenge,
            lift="Squat",
            performed_at=date(2024, 3, 1),
            weight=Decimal("100.00"),
            reps=5,
            points_earned=6,
            is_current_best=True,
        )

        activity = build_recent_scoring_activity(challenge, viewer)

        assert len(activity) == 1
        row = activity[0]
        assert row["name"] == "Alice"
        assert row["lift"] == "Squat"
        assert row["weight"] == Decimal("100.0")
        assert row["unit"] == "kg"
        assert row["reps"] == 5
        assert row["points_earned"] == 6
        assert row["date"] == date(2024, 3, 1)

    def test_most_recent_first_ordering(self, challenge, viewer):
        alice = UserFactory(display_name="Alice")
        _accept(challenge, alice)
        for day in (date(2024, 1, 1), date(2024, 2, 1), date(2024, 3, 1)):
            PointEarnEventFactory(
                user=alice,
                challenge=challenge,
                lift="Squat",
                performed_at=day,
                points_earned=1,
            )

        activity = build_recent_scoring_activity(challenge, viewer)

        dates = [row["date"] for row in activity]
        assert dates == [date(2024, 3, 1), date(2024, 2, 1), date(2024, 1, 1)]

    def test_scoped_to_challenge(self, challenge, viewer):
        other = ChallengeFactory(status=Challenge.Status.ACTIVE)
        alice = UserFactory(display_name="Alice")
        _accept(challenge, alice)
        _accept(other, alice)
        PointEarnEventFactory(
            user=alice, challenge=challenge, lift="Squat", points_earned=6
        )
        PointEarnEventFactory(
            user=alice, challenge=other, lift="Bench", points_earned=4
        )

        activity = build_recent_scoring_activity(challenge, viewer)

        assert [row["lift"] for row in activity] == ["Squat"]

    def test_bailed_participant_excluded(self, challenge, viewer):
        alice = UserFactory(display_name="Alice")
        gone = UserFactory(display_name="Gone")
        _accept(challenge, alice)
        _accept(challenge, gone, is_bailed=True)
        PointEarnEventFactory(
            user=alice, challenge=challenge, lift="Squat", points_earned=6
        )
        PointEarnEventFactory(
            user=gone, challenge=challenge, lift="Bench", points_earned=9
        )

        activity = build_recent_scoring_activity(challenge, viewer)

        assert [row["name"] for row in activity] == ["Alice"]

    def test_bounded_by_limit(self, challenge, viewer):
        alice = UserFactory(display_name="Alice")
        _accept(challenge, alice)
        for day in (
            date(2024, 1, 1),
            date(2024, 1, 2),
            date(2024, 1, 3),
            date(2024, 1, 4),
            date(2024, 1, 5),
        ):
            PointEarnEventFactory(
                user=alice,
                challenge=challenge,
                lift="Squat",
                performed_at=day,
                points_earned=1,
            )

        activity = build_recent_scoring_activity(challenge, viewer, limit=3)

        assert len(activity) == 3

    def test_default_limit_is_five(self, challenge, viewer):
        alice = UserFactory(display_name="Alice")
        _accept(challenge, alice)
        for day_num in range(1, 8):
            PointEarnEventFactory(
                user=alice,
                challenge=challenge,
                lift="Squat",
                performed_at=date(2024, 1, day_num),
                points_earned=1,
            )

        activity = build_recent_scoring_activity(challenge, viewer)

        assert len(activity) == 5

    def test_zero_point_events_excluded(self, challenge, viewer):
        alice = UserFactory(display_name="Alice")
        _accept(challenge, alice)
        PointEarnEventFactory(
            user=alice,
            challenge=challenge,
            lift="Squat",
            performed_at=date(2024, 1, 1),
            points_earned=0,
        )
        PointEarnEventFactory(
            user=alice,
            challenge=challenge,
            lift="Bench",
            performed_at=date(2024, 1, 2),
            points_earned=4,
        )

        activity = build_recent_scoring_activity(challenge, viewer)

        assert [row["lift"] for row in activity] == ["Bench"]

    def test_same_day_same_lift_collapses_to_best_set(self, challenge, viewer):
        # synced_at is set explicitly (rather than left to the factory's
        # timezone.now() default) so the query's -synced_at ordering is
        # deterministic: the earlier-synced (but higher-scoring) event is
        # iterated *second*, which is what exercises the "a later-seen event
        # beats the current best" update path rather than just the
        # first-seen-happens-to-be-best path.
        alice = UserFactory(display_name="Alice")
        _accept(challenge, alice)
        PointEarnEventFactory(
            user=alice,
            challenge=challenge,
            lift="Bench",
            performed_at=date(2024, 1, 1),
            synced_at=datetime(2024, 1, 1, 10, 0, tzinfo=UTC),
            weight=Decimal("90.00"),
            reps=5,
            points_earned=8,
        )
        PointEarnEventFactory(
            user=alice,
            challenge=challenge,
            lift="Bench",
            performed_at=date(2024, 1, 1),
            synced_at=datetime(2024, 1, 1, 11, 0, tzinfo=UTC),
            weight=Decimal("80.00"),
            reps=5,
            points_earned=5,
        )

        activity = build_recent_scoring_activity(challenge, viewer)

        assert len(activity) == 1
        assert activity[0]["points_earned"] == 8
        assert activity[0]["weight"] == Decimal("90.0")

    def test_same_day_different_lift_not_collapsed(self, challenge, viewer):
        alice = UserFactory(display_name="Alice")
        _accept(challenge, alice)
        PointEarnEventFactory(
            user=alice,
            challenge=challenge,
            lift="Bench",
            performed_at=date(2024, 1, 1),
            points_earned=5,
        )
        PointEarnEventFactory(
            user=alice,
            challenge=challenge,
            lift="Squat",
            performed_at=date(2024, 1, 1),
            points_earned=8,
        )

        activity = build_recent_scoring_activity(challenge, viewer)

        assert len(activity) == 2

    def test_same_day_same_lift_different_lifter_not_collapsed(self, challenge, viewer):
        alice = UserFactory(display_name="Alice")
        bob = UserFactory(display_name="Bob")
        _accept(challenge, alice)
        _accept(challenge, bob)
        PointEarnEventFactory(
            user=alice,
            challenge=challenge,
            lift="Bench",
            performed_at=date(2024, 1, 1),
            points_earned=5,
        )
        PointEarnEventFactory(
            user=bob,
            challenge=challenge,
            lift="Bench",
            performed_at=date(2024, 1, 1),
            points_earned=8,
        )

        activity = build_recent_scoring_activity(challenge, viewer)

        assert len(activity) == 2

    def test_weight_converted_to_viewer_unit(self, challenge):
        lb_viewer = UserFactory(unit_preference=User.UnitPreference.LB)
        alice = UserFactory(display_name="Alice")
        _accept(challenge, alice)
        PointEarnEventFactory(
            user=alice,
            challenge=challenge,
            lift="Squat",
            weight=Decimal("100.00"),
            points_earned=6,
        )

        activity = build_recent_scoring_activity(challenge, lb_viewer)

        assert activity[0]["unit"] == "lb"
        # 100 kg -> ~220.5 lb
        assert activity[0]["weight"] == Decimal("220.5")

    def test_deactivated_user_shown_under_their_current_display_name(
        self, challenge, viewer
    ):
        # Deactivated users only ever get there via anonymize_account, which
        # already replaced their real name with a pseudonym -- there's no
        # separate "Former Participant" masking layer on top of that anymore
        # (it disagreed with other pages that already showed the pseudonym).
        former = UserFactory(display_name="PseudonymName", is_active=False)
        _accept(challenge, former)
        PointEarnEventFactory(
            user=former, challenge=challenge, lift="Squat", points_earned=5
        )

        activity = build_recent_scoring_activity(challenge, viewer)

        assert activity[0]["name"] == "PseudonymName (deleted)"

    def test_superseded_event_excluded_from_feed(self, challenge, viewer):
        # TASK-240: a lift already at its max scoreable tier can be performed
        # again without setting a new best -- both events score the same
        # non-zero points, but only the earlier is_current_best=True one
        # should read as activity. The later, superseded event must not show
        # up looking like a fresh achievement.
        alice = UserFactory(display_name="Alice")
        _accept(challenge, alice)
        PointEarnEventFactory(
            user=alice,
            challenge=challenge,
            lift="Deadlift",
            performed_at=date(2024, 1, 1),
            points_earned=10,
            is_current_best=True,
        )
        PointEarnEventFactory(
            user=alice,
            challenge=challenge,
            lift="Deadlift",
            performed_at=date(2024, 1, 15),
            points_earned=10,
            is_current_best=False,
        )

        activity = build_recent_scoring_activity(challenge, viewer)

        assert len(activity) == 1
        assert activity[0]["date"] == date(2024, 1, 1)
