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
from scoring.services import build_points_over_time, build_recent_scoring_activity
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
        # nothing scored in Squat before this, so it is worth its full value
        assert row["points_delta"] == 6
        assert row["date"] == date(2024, 3, 1)

    def test_most_recent_first_ordering(self, challenge, viewer):
        alice = UserFactory(display_name="Alice")
        _accept(challenge, alice)
        for points, day in enumerate(
            (date(2024, 1, 1), date(2024, 2, 1), date(2024, 3, 1)), start=1
        ):
            PointEarnEventFactory(
                user=alice,
                challenge=challenge,
                lift="Squat",
                performed_at=day,
                points_earned=points,
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
        for day_num in range(1, 6):
            PointEarnEventFactory(
                user=alice,
                challenge=challenge,
                lift="Squat",
                performed_at=date(2024, 1, day_num),
                points_earned=day_num,
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
                points_earned=day_num,
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

    def test_deactivated_user_excluded_from_feed(self, challenge, viewer):
        """A deleted account's sessions drop out of the feed, same as a bailed
        participant's -- the feed must not name someone the leaderboard above it
        no longer lists. The active lifter's row proves the filter is scoped to
        the deleted account rather than emptying the feed."""
        alice = UserFactory(display_name="Alice")
        former = UserFactory(display_name="PseudonymName", is_active=False)
        _accept(challenge, alice)
        _accept(challenge, former)
        PointEarnEventFactory(
            user=former, challenge=challenge, lift="Squat", points_earned=5
        )
        PointEarnEventFactory(
            user=alice, challenge=challenge, lift="Bench", points_earned=3
        )

        activity = build_recent_scoring_activity(challenge, viewer)

        assert [row["name"] for row in activity] == ["Alice"]

    def test_repeat_at_an_already_reached_tier_excluded_from_feed(
        self, challenge, viewer
    ):
        # TASK-240: a lift already at its max scoreable tier can be performed
        # again without setting a new best -- both events score the same
        # non-zero points, but the later one moved nothing, so its delta is
        # zero and it must not show up looking like a fresh achievement.
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


class TestPointsDelta:
    """points_delta is the event's net effect on that lifter's total -- its
    points minus the best they already held in that lift from an earlier day."""

    def test_first_score_in_a_lift_is_worth_its_full_value(self, challenge, viewer):
        alice = UserFactory(display_name="Alice")
        _accept(challenge, alice)
        PointEarnEventFactory(
            user=alice,
            challenge=challenge,
            lift="Squat",
            performed_at=date(2024, 3, 1),
            points_earned=6,
            is_current_best=True,
        )

        assert build_recent_scoring_activity(challenge, viewer)[0]["points_delta"] == 6

    def test_beating_a_previous_pr_is_worth_only_the_improvement(
        self, challenge, viewer
    ):
        """The reason the column exists: raw points_earned reads as six points
        of progress when the leaderboard only moved by two."""
        alice = UserFactory(display_name="Alice")
        _accept(challenge, alice)
        PointEarnEventFactory(
            user=alice,
            challenge=challenge,
            lift="Squat",
            performed_at=date(2024, 3, 1),
            points_earned=4,
            is_current_best=False,
        )
        PointEarnEventFactory(
            user=alice,
            challenge=challenge,
            lift="Squat",
            performed_at=date(2024, 3, 20),
            points_earned=6,
            is_current_best=True,
        )

        row = build_recent_scoring_activity(challenge, viewer)[0]
        assert row["points_earned"] == 6
        assert row["points_delta"] == 2

    def test_a_different_lift_does_not_offset_the_delta(self, challenge, viewer):
        """Prior points only count against the same lift -- a big bench must
        not make a first-ever squat look like it gained nothing."""
        alice = UserFactory(display_name="Alice")
        _accept(challenge, alice)
        PointEarnEventFactory(
            user=alice,
            challenge=challenge,
            lift="Bench",
            performed_at=date(2024, 3, 1),
            points_earned=9,
            is_current_best=True,
        )
        PointEarnEventFactory(
            user=alice,
            challenge=challenge,
            lift="Squat",
            performed_at=date(2024, 3, 20),
            points_earned=5,
            is_current_best=True,
        )

        by_lift = {
            row["lift"]: row["points_delta"]
            for row in build_recent_scoring_activity(challenge, viewer)
        }
        assert by_lift == {"Bench": 9, "Squat": 5}

    def test_another_lifters_history_does_not_offset_the_delta(self, challenge, viewer):
        alice = UserFactory(display_name="Alice")
        bob = UserFactory(display_name="Bob")
        _accept(challenge, alice)
        _accept(challenge, bob)
        PointEarnEventFactory(
            user=alice,
            challenge=challenge,
            lift="Squat",
            performed_at=date(2024, 3, 1),
            points_earned=8,
            is_current_best=True,
        )
        PointEarnEventFactory(
            user=bob,
            challenge=challenge,
            lift="Squat",
            performed_at=date(2024, 3, 20),
            points_earned=5,
            is_current_best=True,
        )

        by_name = {
            row["name"]: row["points_delta"]
            for row in build_recent_scoring_activity(challenge, viewer)
        }
        assert by_name == {"Alice": 8, "Bob": 5}

    def test_earlier_sets_the_same_day_are_one_step_not_two(self, challenge, viewer):
        """A session is collapsed to its best set, so the delta must measure
        the whole session -- from what the lifter held before that day to what
        they held after -- not just the last working set's edge over the one
        before it."""
        alice = UserFactory(display_name="Alice")
        _accept(challenge, alice)
        for points, best in ((3, False), (7, True)):
            PointEarnEventFactory(
                user=alice,
                challenge=challenge,
                lift="Squat",
                performed_at=date(2024, 3, 10),
                points_earned=points,
                is_current_best=best,
            )

        rows = build_recent_scoring_activity(challenge, viewer)
        assert len(rows) == 1
        assert rows[0]["points_delta"] == 7

    def test_delta_matches_the_charts_rise_on_that_date(self, challenge, viewer):
        """The column and the Points Over Time chart describe the same event,
        so they must not tell two stories about it."""
        alice = UserFactory(display_name="Alice")
        _accept(challenge, alice)
        PointEarnEventFactory(
            user=alice,
            challenge=challenge,
            lift="Squat",
            performed_at=date(2024, 3, 1),
            points_earned=4,
            is_current_best=False,
        )
        PointEarnEventFactory(
            user=alice,
            challenge=challenge,
            lift="Squat",
            performed_at=date(2024, 3, 20),
            points_earned=6,
            is_current_best=True,
        )

        row = build_recent_scoring_activity(challenge, viewer)[0]
        chart = build_points_over_time(challenge)
        totals = dict(zip(chart["labels"], chart["datasets"][0]["data"], strict=True))

        assert totals["2024-03-20"] - totals["2024-03-01"] == row["points_delta"]


class TestSupersededSessionsInFeed:
    """Membership is "did this raise the lifter's total", not "is this the PR
    still standing today" -- so a lift's whole progression appears, not just
    its surviving best."""

    def test_whole_progression_of_one_lift_appears(self, challenge, viewer):
        alice = UserFactory(display_name="Alice")
        _accept(challenge, alice)
        for day, points, best in (
            (date(2024, 1, 5), 2, False),
            (date(2024, 1, 19), 4, False),
            (date(2024, 2, 2), 9, True),
        ):
            PointEarnEventFactory(
                user=alice,
                challenge=challenge,
                lift="Back Squat",
                performed_at=day,
                points_earned=points,
                is_current_best=best,
            )

        activity = build_recent_scoring_activity(challenge, viewer)

        assert [
            (r["date"], r["points_earned"], r["points_delta"]) for r in activity
        ] == [
            (date(2024, 2, 2), 9, 5),
            (date(2024, 1, 19), 4, 2),
            (date(2024, 1, 5), 2, 2),
        ]

    def test_session_that_fell_short_of_a_standing_pr_is_omitted(
        self, challenge, viewer
    ):
        """A lighter day scores points but moves nothing. Its delta would be
        negative, which never happened to the lifter's total -- a score does
        not fall -- so the session is filtered rather than shown as a loss."""
        alice = UserFactory(display_name="Alice")
        _accept(challenge, alice)
        PointEarnEventFactory(
            user=alice,
            challenge=challenge,
            lift="Deadlift",
            performed_at=date(2024, 3, 1),
            points_earned=8,
            is_current_best=True,
        )
        PointEarnEventFactory(
            user=alice,
            challenge=challenge,
            lift="Deadlift",
            performed_at=date(2024, 3, 15),
            points_earned=5,
            is_current_best=False,
        )

        activity = build_recent_scoring_activity(challenge, viewer)

        assert [r["date"] for r in activity] == [date(2024, 3, 1)]

    def test_limit_counts_qualifying_rows_not_sessions_examined(
        self, challenge, viewer
    ):
        """Non-qualifying sessions must not consume a slot -- otherwise a
        lifter with several flat days gets a feed padded with nothing."""
        alice = UserFactory(display_name="Alice")
        _accept(challenge, alice)
        PointEarnEventFactory(
            user=alice,
            challenge=challenge,
            lift="Bench Press",
            performed_at=date(2024, 4, 1),
            points_earned=6,
            is_current_best=True,
        )
        # three later sessions that all fall short of the standing 6
        for day in (date(2024, 4, 8), date(2024, 4, 15), date(2024, 4, 22)):
            PointEarnEventFactory(
                user=alice,
                challenge=challenge,
                lift="Bench Press",
                performed_at=day,
                points_earned=4,
                is_current_best=False,
            )

        activity = build_recent_scoring_activity(challenge, viewer, limit=2)

        assert [r["date"] for r in activity] == [date(2024, 4, 1)]

    def test_every_row_is_a_rise_in_the_chart(self, challenge, viewer):
        """The feed and the chart are two views of one ledger: each row is a
        date the line went up, and Gain is how far."""
        alice = UserFactory(display_name="Alice")
        _accept(challenge, alice)
        for day, points, best in (
            (date(2024, 5, 1), 3, False),
            (date(2024, 5, 10), 7, True),
        ):
            PointEarnEventFactory(
                user=alice,
                challenge=challenge,
                lift="Front Squat",
                performed_at=day,
                points_earned=points,
                is_current_best=best,
            )

        chart = build_points_over_time(challenge)
        totals = dict(zip(chart["labels"], chart["datasets"][0]["data"], strict=True))
        labels = chart["labels"]

        for row in build_recent_scoring_activity(challenge, viewer):
            label = row["date"].isoformat()
            previous = labels[labels.index(label) - 1]
            assert totals[label] - totals[previous] == row["points_delta"]
