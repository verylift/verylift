"""Tests for build_points_over_time chart data (TASK-28)."""

from datetime import date

import pytest

from accounts.tests.factories import UserFactory
from challenges.models import Challenge, ChallengeParticipant
from challenges.tests.factories import (
    ChallengeFactory,
    ChallengeParticipantFactory,
)
from scoring.models import PointEarnEvent
from scoring.services import build_points_over_time, rank_participants
from scoring.tests.factories import PointEarnEventFactory


@pytest.fixture
def challenge(db):
    return ChallengeFactory(status=Challenge.Status.ACTIVE)


def _accept(challenge, user):
    return ChallengeParticipantFactory(
        challenge=challenge,
        user=user,
        invite_status=ChallengeParticipant.InviteStatus.ACCEPTED,
    )


class TestBuildPointsOverTime:
    def test_structure_and_cumulative_for_known_events(self, challenge):
        alice = UserFactory(display_name="Alice")
        _accept(challenge, alice)
        PointEarnEventFactory(
            user=alice,
            challenge=challenge,
            lift="Squat",
            performed_at=date(2024, 1, 15),
            points_earned=5,
            is_current_best=True,
        )
        PointEarnEventFactory(
            user=alice,
            challenge=challenge,
            lift="Bench",
            performed_at=date(2024, 1, 22),
            points_earned=3,
            is_current_best=True,
        )

        data = build_points_over_time(challenge)

        # leading zero-baseline label precedes the first scored event
        assert data["labels"] == ["2024-01-14", "2024-01-15", "2024-01-22"]
        assert len(data["datasets"]) == 1
        ds = data["datasets"][0]
        assert ds["label"] == "Alice"
        # 0 baseline, then cumulative high-watermark: 5, then 5+3
        assert ds["data"] == [0, 5, 8]

    def test_superseded_event_keeps_the_points_it_held_at_the_time(self, challenge):
        """A re-earned lift must not rewrite its own past. Bob held 2 points
        from Feb 1 until the Feb 10 PR replaced them with 6; the chart shows
        that, rather than back-dating today's 6 and zeroing out Feb 1."""
        bob = UserFactory(display_name="Bob")
        _accept(challenge, bob)
        # superseded earlier attempt on the same lift
        PointEarnEventFactory(
            user=bob,
            challenge=challenge,
            lift="Squat",
            performed_at=date(2024, 2, 1),
            points_earned=2,
            is_current_best=False,
        )
        PointEarnEventFactory(
            user=bob,
            challenge=challenge,
            lift="Squat",
            performed_at=date(2024, 2, 10),
            points_earned=6,
            is_current_best=True,
        )

        data = build_points_over_time(challenge)

        assert data["labels"] == ["2024-01-31", "2024-02-01", "2024-02-10"]
        # the second squat replaces the first's contribution rather than
        # stacking on it -- 6, not 2+6
        assert data["datasets"][0]["data"] == [0, 2, 6]

    def test_re_earning_a_lift_does_not_change_earlier_points(self, challenge):
        """The property the whole design exists for: adding a new PR extends
        the trace without altering any value already drawn."""
        carol = UserFactory(display_name="Carol")
        _accept(challenge, carol)
        PointEarnEventFactory(
            user=carol,
            challenge=challenge,
            lift="Squat",
            performed_at=date(2024, 3, 1),
            points_earned=4,
            is_current_best=True,
        )

        before = build_points_over_time(challenge)
        history_before = dict(
            zip(before["labels"], before["datasets"][0]["data"], strict=True)
        )

        # a later, better squat supersedes the first
        first = PointEarnEvent.objects.get(user=carol, performed_at=date(2024, 3, 1))
        first.is_current_best = False
        first.save(update_fields=["is_current_best"])
        PointEarnEventFactory(
            user=carol,
            challenge=challenge,
            lift="Squat",
            performed_at=date(2024, 3, 20),
            points_earned=7,
            is_current_best=True,
        )

        after = build_points_over_time(challenge)
        history_after = dict(
            zip(after["labels"], after["datasets"][0]["data"], strict=True)
        )

        for label, value in history_before.items():
            assert history_after[label] == value, f"{label} changed"
        assert history_after["2024-03-20"] == 7

    def test_backdated_set_raises_the_curve_at_the_date_performed(self, challenge):
        """Recording a forgotten set after the fact credits the day it was
        actually lifted, not the day it synced -- performed_at is what every
        other scoring path keys on."""
        dave = UserFactory(display_name="Dave")
        _accept(challenge, dave)
        PointEarnEventFactory(
            user=dave,
            challenge=challenge,
            lift="Bench",
            performed_at=date(2024, 4, 20),
            points_earned=5,
            is_current_best=True,
        )
        # remembered later: a squat performed well before the bench above
        PointEarnEventFactory(
            user=dave,
            challenge=challenge,
            lift="Squat",
            performed_at=date(2024, 4, 5),
            points_earned=3,
            is_current_best=True,
        )

        data = build_points_over_time(challenge)

        assert data["labels"] == ["2024-04-04", "2024-04-05", "2024-04-20"]
        assert data["datasets"][0]["data"] == [0, 3, 8]

    def test_zero_point_rows_add_no_labels_and_no_points(self, challenge):
        """Sub-threshold audit rows can never raise a per-lift maximum, so
        they must not litter the shared x-axis with flat dates."""
        erin = UserFactory(display_name="Erin")
        _accept(challenge, erin)
        PointEarnEventFactory(
            user=erin,
            challenge=challenge,
            lift="Squat",
            performed_at=date(2024, 5, 1),
            points_earned=0,
            is_current_best=False,
        )
        PointEarnEventFactory(
            user=erin,
            challenge=challenge,
            lift="Squat",
            performed_at=date(2024, 5, 10),
            points_earned=4,
            is_current_best=True,
        )

        data = build_points_over_time(challenge)

        assert data["labels"] == ["2024-05-09", "2024-05-10"]
        assert data["datasets"][0]["data"] == [0, 4]

    def test_final_value_matches_the_leaderboard_total(self, challenge):
        """The trace's last point and the leaderboard must agree, or the page
        contradicts itself. Both reduce to the per-lift best."""
        frank = UserFactory(display_name="Frank")
        _accept(challenge, frank)
        PointEarnEventFactory(
            user=frank,
            challenge=challenge,
            lift="Squat",
            performed_at=date(2024, 6, 1),
            points_earned=2,
            is_current_best=False,
        )
        PointEarnEventFactory(
            user=frank,
            challenge=challenge,
            lift="Squat",
            performed_at=date(2024, 6, 5),
            points_earned=6,
            is_current_best=True,
        )
        PointEarnEventFactory(
            user=frank,
            challenge=challenge,
            lift="Bench",
            performed_at=date(2024, 6, 8),
            points_earned=3,
            is_current_best=True,
        )

        data = build_points_over_time(challenge)
        leaderboard = {
            r["user"].pk: r["total_points"] for r in rank_participants(challenge)
        }

        assert data["datasets"][0]["data"][-1] == leaderboard[frank.pk]

    def test_multiple_participants_share_label_axis(self, challenge):
        alice = UserFactory(display_name="Alice")
        bob = UserFactory(display_name="Bob")
        _accept(challenge, alice)
        _accept(challenge, bob)
        PointEarnEventFactory(
            user=alice,
            challenge=challenge,
            lift="Squat",
            performed_at=date(2024, 1, 1),
            points_earned=4,
            is_current_best=True,
        )
        PointEarnEventFactory(
            user=bob,
            challenge=challenge,
            lift="Bench",
            performed_at=date(2024, 1, 5),
            points_earned=7,
            is_current_best=True,
        )

        data = build_points_over_time(challenge)

        # each participant contributes a zero-baseline day before their first
        # event; the axis stays a single shared, sorted date list
        assert data["labels"] == [
            "2023-12-31",
            "2024-01-01",
            "2024-01-04",
            "2024-01-05",
        ]
        by_label = {ds["label"]: ds["data"] for ds in data["datasets"]}
        # Alice: 0 baseline, gains 4 on day 1, holds through the rest
        assert by_label["Alice"] == [0, 4, 4, 4]
        # Bob: 0 until his own event on day 5
        assert by_label["Bob"] == [0, 0, 0, 7]

    def test_participant_with_no_events_produces_zero_series(self, challenge):
        alice = UserFactory(display_name="Alice")
        idle = UserFactory(display_name="Idle")
        _accept(challenge, alice)
        _accept(challenge, idle)
        PointEarnEventFactory(
            user=alice,
            challenge=challenge,
            lift="Squat",
            performed_at=date(2024, 1, 1),
            points_earned=4,
            is_current_best=True,
        )

        data = build_points_over_time(challenge)

        # Alice's baseline widens the axis to two labels; Idle stays flat zero
        # and contributes no baseline of her own
        assert data["labels"] == ["2023-12-31", "2024-01-01"]
        by_label = {ds["label"]: ds["data"] for ds in data["datasets"]}
        assert by_label["Idle"] == [0, 0]

    def test_no_events_at_all_empty_labels(self, challenge):
        alice = UserFactory(display_name="Alice")
        _accept(challenge, alice)

        data = build_points_over_time(challenge)

        assert data["labels"] == []
        assert data["datasets"] == [{"label": "Alice", "data": []}]

    def test_deactivated_user_label(self, challenge):
        gone = UserFactory(display_name="Gone User", is_active=False)
        _accept(challenge, gone)
        PointEarnEventFactory(
            user=gone,
            challenge=challenge,
            lift="Squat",
            performed_at=date(2024, 1, 1),
            points_earned=4,
            is_current_best=True,
        )

        data = build_points_over_time(challenge)

        labels = [ds["label"] for ds in data["datasets"]]
        assert "Gone User (deleted)" in labels
        assert "Gone User" not in labels

    def test_rejoined_participant_keeps_pre_bail_progress(self, challenge):
        """A participant scored before bailing still shows that progress after
        rejoining: rejoin resets joined_at but leaves the current-best events,
        which the chart credits window-independently (TASK-164)."""
        from datetime import UTC, datetime

        rejoiner = UserFactory(display_name="Rejoiner")
        participant = _accept(challenge, rejoiner)
        participant.joined_at = datetime(2024, 1, 1, tzinfo=UTC)
        participant.save(update_fields=["joined_at"])
        PointEarnEventFactory(
            user=rejoiner,
            challenge=challenge,
            lift="Squat",
            performed_at=date(2024, 1, 10),
            points_earned=5,
            is_current_best=True,
        )

        # bail
        participant.is_bailed = True
        participant.bailed_at = datetime(2024, 2, 1, tzinfo=UTC)
        participant.save(update_fields=["is_bailed", "bailed_at"])

        # rejoin (mirrors invite_link_view: joined_at reset to now)
        participant.is_bailed = False
        participant.bailed_at = None
        participant.invite_status = ChallengeParticipant.InviteStatus.ACCEPTED
        participant.joined_at = datetime(2024, 6, 1, tzinfo=UTC)
        participant.save(
            update_fields=["is_bailed", "bailed_at", "invite_status", "joined_at"]
        )

        data = build_points_over_time(challenge)

        by_label = {ds["label"]: ds["data"] for ds in data["datasets"]}
        # joined_at was reset to after the pre-bail event, so the baseline
        # falls back to the day before that event rather than the reset window
        assert data["labels"] == ["2024-01-09", "2024-01-10"]
        assert by_label["Rejoiner"] == [0, 5]

    def test_single_event_slopes_up_from_zero_baseline(self, challenge):
        """Regression (TASK-220): a participant with exactly one scored event
        gets a zero point before it, so the line slopes up from 0 rather than
        appearing to start already at the value."""
        newbie = UserFactory(display_name="Newbie")
        _accept(challenge, newbie)
        PointEarnEventFactory(
            user=newbie,
            challenge=challenge,
            lift="Squat",
            performed_at=date(2024, 5, 20),
            points_earned=9,
            is_current_best=True,
        )

        data = build_points_over_time(challenge)

        assert data["labels"] == ["2024-05-19", "2024-05-20"]
        ds = data["datasets"][0]
        assert ds["label"] == "Newbie"
        # zero baseline first, then the single event's value — a rising slope
        assert ds["data"] == [0, 9]

    def test_from_start_baseline_at_challenge_start(self, db):
        """FROM_START: the zero baseline sits at the challenge start date for
        every participant, even one whose first event is much later."""
        comp = ChallengeFactory(
            status=Challenge.Status.ACTIVE,
            history_window=Challenge.HistoryWindow.FROM_START,
            start_date=date(2024, 1, 1),
        )
        alice = UserFactory(display_name="Alice")
        _accept(comp, alice)
        PointEarnEventFactory(
            user=alice,
            challenge=comp,
            lift="Squat",
            performed_at=date(2024, 3, 1),
            points_earned=6,
            is_current_best=True,
        )

        data = build_points_over_time(comp)

        assert data["labels"] == ["2024-01-01", "2024-03-01"]
        assert data["datasets"][0]["data"] == [0, 6]

    def test_from_join_baseline_at_join_date_after_start(self, db):
        """FROM_JOIN: a participant who joined well after the challenge start
        gets their zero baseline at their join date, not the start date."""
        from datetime import UTC, datetime

        comp = ChallengeFactory(
            status=Challenge.Status.ACTIVE,
            history_window=Challenge.HistoryWindow.FROM_JOIN,
            start_date=date(2024, 1, 1),
        )
        latecomer = UserFactory(display_name="Latecomer")
        participant = _accept(comp, latecomer)
        participant.joined_at = datetime(2024, 2, 15, tzinfo=UTC)
        participant.save(update_fields=["joined_at"])
        PointEarnEventFactory(
            user=latecomer,
            challenge=comp,
            lift="Squat",
            performed_at=date(2024, 3, 1),
            points_earned=8,
            is_current_best=True,
        )

        data = build_points_over_time(comp)

        assert data["labels"] == ["2024-02-15", "2024-03-01"]
        assert data["datasets"][0]["data"] == [0, 8]

    def test_top_n_keeps_highest_final_cumulative_datasets(self, challenge):
        """TASK-303: top_n truncates to the N datasets with the highest final
        value, without touching the shared label axis."""
        high = UserFactory(display_name="High")
        mid = UserFactory(display_name="Mid")
        low = UserFactory(display_name="Low")
        for user, points in [(high, 10), (mid, 5), (low, 1)]:
            _accept(challenge, user)
            PointEarnEventFactory(
                user=user,
                challenge=challenge,
                lift="Squat",
                performed_at=date(2024, 1, 1),
                points_earned=points,
                is_current_best=True,
            )

        full = build_points_over_time(challenge)
        truncated = build_points_over_time(challenge, top_n=2)

        assert len(full["datasets"]) == 3
        assert truncated["labels"] == full["labels"]
        labels = {ds["label"] for ds in truncated["datasets"]}
        assert labels == {"High", "Mid"}

    def test_top_n_is_a_no_op_when_not_exceeded(self, challenge):
        alice = UserFactory(display_name="Alice")
        _accept(challenge, alice)
        PointEarnEventFactory(
            user=alice,
            challenge=challenge,
            lift="Squat",
            performed_at=date(2024, 1, 1),
            points_earned=4,
            is_current_best=True,
        )

        data = build_points_over_time(challenge, top_n=5)

        assert len(data["datasets"]) == 1

    def test_excludes_bailed_and_invited(self, challenge):
        active = UserFactory(display_name="Active")
        _accept(challenge, active)
        bailed = UserFactory(display_name="Bailed")
        ChallengeParticipantFactory(
            challenge=challenge,
            user=bailed,
            invite_status=ChallengeParticipant.InviteStatus.ACCEPTED,
            is_bailed=True,
        )
        invited = UserFactory(display_name="Invited")
        ChallengeParticipantFactory(
            challenge=challenge,
            user=invited,
            invite_status=ChallengeParticipant.InviteStatus.INVITED,
        )

        data = build_points_over_time(challenge)

        labels = [ds["label"] for ds in data["datasets"]]
        assert labels == ["Active"]
