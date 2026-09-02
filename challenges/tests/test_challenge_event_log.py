"""Tests for the challenge activity log (challenges.events).

The log has two halves that have to behave differently: stored ChallengeEvent
rows, which only exist from when an action happened, and scoring entries
derived from the PointEarnEvent rows that already exist. These cover the
recording seam, each emission point, the merge, and the privacy rule that a
deleted account is never named.
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.db import transaction
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from accounts.services import anonymize_account
from accounts.tests.factories import UserFactory
from challenges.events import build_challenge_event_log, record_challenge_event
from challenges.models import Challenge, ChallengeEvent, ChallengeParticipant
from challenges.services import close_challenge, remove_participant, transfer_ownership
from challenges.tests.factories import (
    ChallengeInviteLinkFactory,
    ChallengeParticipantFactory,
    CustomGoalFactory,
    make_custom_challenge,
)
from scoring.tests.factories import PointEarnEventFactory

pytestmark = pytest.mark.django_db

EventType = ChallengeEvent.EventType


@pytest.fixture
def challenge(db):
    return make_custom_challenge(lifts=["Squat"], status=Challenge.Status.ACTIVE)


@pytest.fixture
def mock_sync():
    with (
        patch("challenges.services.sync_user_lifts") as mock_pull,
        patch("challenges.services.score_pooled_history") as mock_score,
    ):
        yield SimpleNamespace(pull=mock_pull, score=mock_score)


def _accepted(challenge, user, **kwargs):
    return ChallengeParticipantFactory(
        challenge=challenge,
        user=user,
        invite_status=ChallengeParticipant.InviteStatus.ACCEPTED,
        joined_at=timezone.now(),
        **kwargs,
    )


def _kinds(challenge):
    return list(
        ChallengeEvent.objects.filter(challenge=challenge).values_list(
            "event_type", flat=True
        )
    )


class TestRecordChallengeEvent:
    def test_a_failed_write_does_not_break_the_action_it_describes(self, challenge):
        """The log is a side effect of something that already happened. If the
        insert blows up, the caller must still succeed -- so this swallows and
        logs rather than propagating."""
        with patch(
            "challenges.events.ChallengeEvent.objects.create",
            side_effect=RuntimeError("db is having a day"),
        ):
            assert record_challenge_event(challenge, EventType.CLOSED) is None

    def test_a_failed_write_inside_a_transaction_leaves_it_usable(self, challenge):
        """The half of "never raises" that only shows up under a real database
        error. Several call sites (save_custom_goal, remove_participant,
        transfer_ownership) run inside atomic(); swallowing a DB error without
        a savepoint leaves the enclosing transaction broken, so the *next*
        query raises TransactionManagementError and the action this was only
        supposed to annotate fails after all.

        event_type is a varchar(30), so an over-long value is a genuine
        database error rather than a mocked one -- Django does not validate
        max_length on save.
        """
        with transaction.atomic():
            assert record_challenge_event(challenge, "x" * 100) is None
            # The transaction must still accept work, and commit cleanly.
            challenge.name = "Still Writable"
            challenge.save(update_fields=["name"])

        challenge.refresh_from_db()
        assert challenge.name == "Still Writable"
        assert not ChallengeEvent.objects.filter(challenge=challenge).exists()


class TestEmission:
    """Each emission point, driven through the real action rather than by
    calling record_challenge_event directly -- the thing worth pinning is that
    the action remembers to log, not that the recorder works."""

    def test_joining_via_invite_link_is_logged(self, challenge, mock_sync):
        link = ChallengeInviteLinkFactory(
            challenge=challenge, created_by=challenge.creator
        )
        joiner = UserFactory()
        client = Client()
        client.force_login(joiner)

        client.post(reverse("challenges:invite-accept", args=[link.token]))

        event = ChallengeEvent.objects.get(
            challenge=challenge, event_type=EventType.JOINED
        )
        assert event.actor == joiner

    def test_bailing_is_logged(self, challenge, mock_sync):
        leaver = UserFactory()
        _accepted(challenge, leaver)
        client = Client()
        client.force_login(leaver)

        client.post(reverse("challenges:bail", args=[challenge.pk]))

        event = ChallengeEvent.objects.get(
            challenge=challenge, event_type=EventType.LEFT
        )
        assert event.actor == leaver

    def test_removal_is_logged(self, challenge):
        participant = _accepted(challenge, UserFactory())

        remove_participant(participant)

        event = ChallengeEvent.objects.get(
            challenge=challenge, event_type=EventType.REMOVED
        )
        assert event.actor == participant.user

    def test_ownership_transfer_is_logged(self, challenge):
        new_owner = UserFactory()
        _accepted(challenge, new_owner)

        transfer_ownership(challenge, new_owner)

        event = ChallengeEvent.objects.get(
            challenge=challenge, event_type=EventType.OWNERSHIP_TRANSFERRED
        )
        assert event.actor == new_owner

    def test_rename_is_logged_with_both_names(self, challenge, mock_sync):
        """The log is the only place the old name survives -- challenge.name is
        overwritten in place."""
        original = challenge.name
        client = Client()
        client.force_login(challenge.creator)

        client.post(
            reverse("challenges:rename", args=[challenge.pk]), {"name": "New Name"}
        )

        event = ChallengeEvent.objects.get(
            challenge=challenge, event_type=EventType.RENAMED
        )
        assert event.metadata == {
            "previous_name": original,
            "new_name": "New Name",
        }

    def test_close_is_logged(self, challenge):
        with (
            patch("challenges.services.sync_user_lifts"),
            patch("challenges.services.score_pooled_history"),
        ):
            close_challenge(challenge)

        assert EventType.CLOSED in _kinds(challenge)

    def test_cancel_is_logged(self, challenge, mock_sync):
        client = Client()
        client.force_login(challenge.creator)

        client.post(reverse("challenges:cancel", args=[challenge.pk]))

        event = ChallengeEvent.objects.get(
            challenge=challenge, event_type=EventType.CANCELLED
        )
        assert event.actor == challenge.creator

    def test_locking_a_goal_is_logged(self, challenge):
        from challenges.custom_goals import save_custom_goal

        participant = _accepted(challenge, UserFactory())

        save_custom_goal(
            participant,
            "Targets",
            {"Squat": {rep: Decimal("100.00") for rep in range(1, 11)}},
        )

        event = ChallengeEvent.objects.get(
            challenge=challenge, event_type=EventType.GOAL_LOCKED
        )
        assert event.actor == participant.user

    def test_locking_a_rep_target_goal_is_logged(self, db):
        from challenges.rep_target_goals import save_rep_target_goal
        from challenges.tests.factories import make_rep_target_challenge

        challenge = make_rep_target_challenge(
            lifts=["Push Up"], status=Challenge.Status.ACTIVE
        )
        participant = _accepted(challenge, UserFactory())

        save_rep_target_goal(participant, "Targets", {"Push Up": (Decimal("0"), 20)})

        event = ChallengeEvent.objects.get(
            challenge=challenge, event_type=EventType.GOAL_LOCKED
        )
        assert event.actor == participant.user


class TestBuildChallengeEventLog:
    def test_merges_stored_events_and_derived_scoring_newest_first(self, challenge):
        """The two halves interleave by time rather than concatenating. The
        scoring entry is not stored anywhere -- it is derived from the
        PointEarnEvent that already existed."""
        lifter = UserFactory(display_name="Alice")
        _accepted(challenge, lifter)
        PointEarnEventFactory(
            user=lifter,
            challenge=challenge,
            lift="Squat",
            performed_at=date.today() - timedelta(days=2),
            points_earned=6,
        )
        old_event = record_challenge_event(challenge, EventType.JOINED, actor=lifter)
        ChallengeEvent.objects.filter(pk=old_event.pk).update(
            created_at=datetime.now(tz=UTC) - timedelta(days=5)
        )
        record_challenge_event(challenge, EventType.CLOSED)

        log = build_challenge_event_log(challenge)

        assert [entry["kind"] for entry in log] == ["closed", "scored", "joined"]
        assert log[1]["detail"] == {"lift": "Squat", "points": 6}
        assert log[1]["actor"] == "Alice"

    def test_scoring_entries_survive_the_lifter_leaving(self, challenge):
        """Unlike the leaderboard, this is a history: a bailed lifter's scoring
        must not vanish from it retroactively, or the log cannot explain what
        the challenge looked like before they left."""
        leaver = UserFactory(display_name="Bailer")
        _accepted(challenge, leaver, is_bailed=True)
        PointEarnEventFactory(
            user=leaver, challenge=challenge, lift="Squat", points_earned=6
        )

        log = build_challenge_event_log(challenge)

        assert [(e["kind"], e["actor"]) for e in log] == [("scored", "Bailer")]

    def test_deleted_account_is_never_named(self, challenge):
        """The whole reason the log exists instead of an anonymized roster row:
        it says what happened without reintroducing the pseudonym."""
        gone = UserFactory(display_name="Gone User")
        _accepted(challenge, gone)
        record_challenge_event(challenge, EventType.JOINED, actor=gone)
        PointEarnEventFactory(
            user=gone, challenge=challenge, lift="Squat", points_earned=6
        )
        record_challenge_event(challenge, EventType.LEFT, actor=gone)

        anonymize_account(gone)
        log = build_challenge_event_log(challenge)

        assert {entry["actor"] for entry in log} == {"a deleted account"}
        assert gone.display_name not in {entry["actor"] for entry in log}
        assert len(log) == 3

    def test_null_actor_renders_the_same_as_a_deleted_one(self, challenge):
        """actor is SET_NULL so an audit row can never block removing a user
        row; a null actor has nothing left to name either."""
        record_challenge_event(challenge, EventType.JOINED, actor=None)

        assert build_challenge_event_log(challenge)[0]["actor"] == "a deleted account"

    def test_respects_the_limit(self, challenge):
        for _ in range(5):
            record_challenge_event(challenge, EventType.JOINED)

        assert len(build_challenge_event_log(challenge, limit=3)) == 3

    def test_empty_for_a_fresh_challenge(self, challenge):
        assert build_challenge_event_log(challenge) == []


class TestSettingsPageRendering:
    def test_log_renders_on_the_settings_page(self, challenge, mock_sync):
        creator_participant = _accepted(challenge, challenge.creator)
        CustomGoalFactory(participant=creator_participant, name="Goal")
        leaver = UserFactory(display_name="Departed Lifter")
        _accepted(challenge, leaver)
        record_challenge_event(challenge, EventType.LEFT, actor=leaver)
        client = Client()
        client.force_login(challenge.creator)

        content = client.get(
            reverse("challenges:settings", args=[challenge.pk])
        ).content.decode()

        assert "Departed Lifter left" in content
