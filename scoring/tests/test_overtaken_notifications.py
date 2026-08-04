"""Tests for create_overtaken_notifications (TASK-33)."""

from datetime import date
from decimal import Decimal

import pytest

from accounts.tests.factories import UserFactory
from challenges.models import Challenge
from challenges.tests.factories import (
    ChallengeFactory,
    ChallengeParticipantFactory,
    make_custom_challenge,
)
from liftosaur.tests.factories import LiftHistoryFactory
from notifications.models import Notification
from scoring.services import (
    create_overtaken_notifications,
    process_scored_set,
    score_pooled_history,
)
from scoring.tests.factories import PointEarnEventFactory
from scoring.tests.test_scoring_services import (
    LIFT,
    PERFORMED_AT,
    SYNCED_AT,
    _add_participant_with_goal,
    make_setup,
    targets_from_multiplier,
)


def _entry(user, total_points, rank):
    return {"user": user, "total_points": total_points, "rank": rank}


@pytest.mark.django_db
class TestCreateOvertakenNotifications:
    def test_dropped_participant_gets_one_notification(self):
        challenge = ChallengeFactory()
        a = UserFactory()
        b = UserFactory()
        ChallengeParticipantFactory(challenge=challenge, user=a)
        ChallengeParticipantFactory(challenge=challenge, user=b)

        before = [_entry(a, 10, 1), _entry(b, 5, 2)]
        after = [_entry(b, 20, 1), _entry(a, 10, 2)]

        create_overtaken_notifications(challenge, before, after)

        notes = Notification.objects.filter(event_type=Notification.EventType.OVERTAKEN)
        assert notes.count() == 1
        note = notes.get()
        assert note.user == a
        assert note.challenge == challenge
        assert note.metadata["from_rank"] == 1
        assert note.metadata["to_rank"] == 2
        assert note.metadata["overtaken_by_id"] == str(b.pk)
        assert note.metadata["overtaken_by_name"] == b.display_name

    def test_multi_position_drop_is_single_notification(self):
        """A participant who drops from 2nd to 4th gets ONE notification."""
        challenge = ChallengeFactory()
        users = [UserFactory() for _ in range(4)]
        for u in users:
            ChallengeParticipantFactory(challenge=challenge, user=u)
        a, b, c, d = users

        before = [
            _entry(a, 40, 1),
            _entry(b, 30, 2),
            _entry(c, 20, 3),
            _entry(d, 10, 4),
        ]
        # b drops from 2 to 4
        after = [
            _entry(a, 40, 1),
            _entry(c, 35, 2),
            _entry(d, 32, 3),
            _entry(b, 30, 4),
        ]

        create_overtaken_notifications(challenge, before, after)

        b_notes = Notification.objects.filter(
            event_type=Notification.EventType.OVERTAKEN, user=b
        )
        assert b_notes.count() == 1
        note = b_notes.get()
        assert note.metadata["from_rank"] == 2
        assert note.metadata["to_rank"] == 4
        # Overtaker is whoever now sits at b's old rank (2) → c
        assert note.metadata["overtaken_by_id"] == str(c.pk)

    def test_no_notification_when_rank_unchanged(self):
        challenge = ChallengeFactory()
        a = UserFactory()
        b = UserFactory()
        ChallengeParticipantFactory(challenge=challenge, user=a)
        ChallengeParticipantFactory(challenge=challenge, user=b)

        before = [_entry(a, 10, 1), _entry(b, 5, 2)]
        after = [_entry(a, 15, 1), _entry(b, 5, 2)]

        create_overtaken_notifications(challenge, before, after)
        assert Notification.objects.count() == 0

    def test_no_notification_when_rank_improves(self):
        challenge = ChallengeFactory()
        a = UserFactory()
        b = UserFactory()
        ChallengeParticipantFactory(challenge=challenge, user=a)
        ChallengeParticipantFactory(challenge=challenge, user=b)

        before = [_entry(a, 10, 1), _entry(b, 5, 2)]
        after = [_entry(b, 20, 1), _entry(a, 10, 2)]

        # Only A dropped; B improved and must not be notified.
        create_overtaken_notifications(challenge, before, after)
        assert (
            Notification.objects.filter(
                event_type=Notification.EventType.OVERTAKEN, user=b
            ).count()
            == 0
        )

    def test_bailed_participant_excluded(self):
        challenge = ChallengeFactory()
        a = UserFactory()
        b = UserFactory()
        ChallengeParticipantFactory(challenge=challenge, user=a, is_bailed=True)
        ChallengeParticipantFactory(challenge=challenge, user=b)

        before = [_entry(a, 10, 1), _entry(b, 5, 2)]
        after = [_entry(b, 20, 1), _entry(a, 10, 2)]

        create_overtaken_notifications(challenge, before, after)
        assert (
            Notification.objects.filter(
                event_type=Notification.EventType.OVERTAKEN, user=a
            ).count()
            == 0
        )

    def test_new_participant_not_in_before_is_ignored(self):
        challenge = ChallengeFactory()
        a = UserFactory()
        b = UserFactory()
        ChallengeParticipantFactory(challenge=challenge, user=a)
        ChallengeParticipantFactory(challenge=challenge, user=b)

        before = [_entry(a, 10, 1)]
        after = [_entry(a, 10, 1), _entry(b, 5, 2)]

        create_overtaken_notifications(challenge, before, after)
        assert Notification.objects.count() == 0


@pytest.mark.django_db
class TestProcessScoredSetFiresOvertaken:
    def test_high_watermark_overtake_creates_notification(self):
        leader, challenge, _ = make_setup(multiplier=Decimal("1.0000"))
        # leader: 6 pts (5RM)
        process_scored_set(
            user=leader,
            challenge=challenge,
            lift=LIFT,
            performed_at=PERFORMED_AT,
            reps=5,
            weight=Decimal("87.00"),
            synced_at=SYNCED_AT,
        )

        challenger = UserFactory()
        targets = targets_from_multiplier(Decimal("1.0000"), Decimal("100.00"))
        _add_participant_with_goal(challenge, challenger, {LIFT: targets})
        # challenger: 10 pts (1RM) → overtakes leader
        process_scored_set(
            user=challenger,
            challenge=challenge,
            lift=LIFT,
            performed_at=PERFORMED_AT,
            reps=1,
            weight=Decimal("100.00"),
            synced_at=SYNCED_AT,
        )

        notes = Notification.objects.filter(event_type=Notification.EventType.OVERTAKEN)
        assert notes.count() == 1
        note = notes.get()
        assert note.user == leader
        assert note.metadata["overtaken_by_id"] == str(challenger.pk)
        assert note.metadata["from_rank"] == 1
        assert note.metadata["to_rank"] == 2

    def test_non_best_set_creates_no_overtaken(self):
        user, challenge, _ = make_setup(multiplier=Decimal("1.0000"))
        # First best: 10 pts
        process_scored_set(
            user=user,
            challenge=challenge,
            lift=LIFT,
            performed_at=PERFORMED_AT,
            reps=1,
            weight=Decimal("100.00"),
            synced_at=SYNCED_AT,
        )
        # Weaker set: not a new best → no leaderboard change
        process_scored_set(
            user=user,
            challenge=challenge,
            lift=LIFT,
            performed_at=PERFORMED_AT,
            reps=5,
            weight=Decimal("87.00"),
            synced_at=SYNCED_AT,
        )
        assert (
            Notification.objects.filter(
                event_type=Notification.EventType.OVERTAKEN
            ).count()
            == 0
        )


@pytest.mark.django_db
class TestBackfillDiffsOnce:
    """score_pooled_history diffs the leaderboard once for the whole run (TASK-125).

    A first-time history backfill scores many of a lifter's own progressive PRs
    in a single pass. The board must be diffed once — before vs after the whole
    run — so overtaken notifications reflect the net standings change, not the
    transient intermediate standings each PR briefly produced.
    """

    def _backfill_setup(self):
        # Challenger A joins a from-start challenge and backfills two PRs.
        # 1RM threshold = 1.0 x 100kg bodyweight = 100kg.
        user = UserFactory()
        challenge = make_custom_challenge(
            lifts=[LIFT],
            history_window=Challenge.HistoryWindow.FROM_START,
            start_date=date(2025, 1, 1),
            end_date=date(2026, 12, 31),
        )
        targets = targets_from_multiplier(Decimal("1.0000"), Decimal("100.00"))
        _add_participant_with_goal(challenge, user, {LIFT: targets})
        # Pre-scored rivals sitting above A: B=9 (r1), C=7 (r2), D=5 (r3).
        rivals = {}
        for name, points in (("B", 9), ("C", 7), ("D", 5)):
            rival = UserFactory()
            _add_participant_with_goal(challenge, rival, {LIFT: targets})
            PointEarnEventFactory(
                user=rival,
                challenge=challenge,
                points_earned=points,
                is_current_best=True,
            )
            rivals[name] = rival
        return user, challenge, rivals

    def test_backfill_notifies_net_change_not_transient(self):
        user, challenge, rivals = self._backfill_setup()

        # First (earlier) PR: 86kg x5 -> 6 pts. A slots transiently between D and C.
        LiftHistoryFactory(
            user=user,
            lift=LIFT,
            performed_at=date(2025, 3, 1),
            reps=5,
            weight_kg=Decimal("86.00"),
        )
        # Later PR: 100kg x1 -> 10 pts. A finishes on top.
        LiftHistoryFactory(
            user=user,
            lift=LIFT,
            performed_at=date(2025, 6, 1),
            reps=1,
            weight_kg=Decimal("100.00"),
        )

        score_pooled_history(user=user, challenge=challenge)

        notes = Notification.objects.filter(event_type=Notification.EventType.OVERTAKEN)
        # Net final standings: A(10) > B(9) > C(7) > D(5). Each rival drops one
        # rank, overtaken by whoever now sits at their old rank.
        by_user = {n.user_id: n for n in notes}
        assert set(by_user) == {r.pk for r in rivals.values()}

        b, c, d = rivals["B"], rivals["C"], rivals["D"]
        assert by_user[b.pk].metadata["overtaken_by_id"] == str(user.pk)
        assert by_user[c.pk].metadata["overtaken_by_id"] == str(b.pk)
        # D is overtaken by C in the net standings — NOT by A, which was only a
        # transient intermediate rank A briefly held after the first PR.
        assert by_user[d.pk].metadata["overtaken_by_id"] == str(c.pk)

    def test_idempotent_rerun_emits_no_notifications(self):
        user, challenge, _ = self._backfill_setup()
        LiftHistoryFactory(
            user=user,
            lift=LIFT,
            performed_at=date(2025, 6, 1),
            reps=1,
            weight_kg=Decimal("100.00"),
        )
        score_pooled_history(user=user, challenge=challenge)
        first_count = Notification.objects.filter(
            event_type=Notification.EventType.OVERTAKEN
        ).count()

        # Re-run: everything already scored, so no new events and no new notifs.
        score_pooled_history(user=user, challenge=challenge)
        assert (
            Notification.objects.filter(
                event_type=Notification.EventType.OVERTAKEN
            ).count()
            == first_count
        )
