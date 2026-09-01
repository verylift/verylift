"""Tests for challenges.services.close_challenge (TASK-34)."""

from unittest.mock import patch

import pytest

from challenges.models import Challenge, ChallengeParticipant
from challenges.services import close_challenge
from challenges.tests.factories import (
    ChallengeFactory,
    ChallengeParticipantFactory,
)
from notifications.models import Notification


def _accepted(challenge, **kwargs):
    return ChallengeParticipantFactory(
        challenge=challenge,
        invite_status=ChallengeParticipant.InviteStatus.ACCEPTED,
        **kwargs,
    )


@pytest.mark.django_db
class TestCloseChallenge:
    def test_syncs_each_accepted_non_bailed_participant(self):
        challenge = ChallengeFactory(status=Challenge.Status.ACTIVE)
        p1 = _accepted(challenge)
        p2 = _accepted(challenge)
        _accepted(challenge, is_bailed=True)
        ChallengeParticipantFactory(
            challenge=challenge,
            invite_status=ChallengeParticipant.InviteStatus.INVITED,
        )

        with (
            patch("challenges.services.sync_user_lifts") as mock_sync,
            patch("challenges.services.score_pooled_history") as mock_score,
        ):
            close_challenge(challenge)

        synced_users = {call.args[0] for call in mock_sync.call_args_list}
        assert synced_users == {p1.user, p2.user}
        assert mock_sync.call_count == 2
        scored_users = {call.kwargs["user"] for call in mock_score.call_args_list}
        assert scored_users == {p1.user, p2.user}

    def test_all_participants_synced_before_status_flip(self):
        challenge = ChallengeFactory(status=Challenge.Status.ACTIVE)
        _accepted(challenge)

        statuses_at_sync = []

        def record_status(user):
            statuses_at_sync.append(challenge.status)

        with (
            patch(
                "challenges.services.sync_user_lifts",
                side_effect=record_status,
            ),
            patch("challenges.services.score_pooled_history"),
        ):
            close_challenge(challenge)

        assert statuses_at_sync == [Challenge.Status.ACTIVE]

    def test_sync_exception_does_not_abort_close(self):
        challenge = ChallengeFactory(status=Challenge.Status.ACTIVE)
        _accepted(challenge)
        _accepted(challenge)

        with (
            patch(
                "challenges.services.sync_user_lifts",
                side_effect=RuntimeError("boom"),
            ),
            patch("challenges.services.score_pooled_history"),
        ):
            close_challenge(challenge)

        challenge.refresh_from_db()
        assert challenge.status == Challenge.Status.COMPLETED
        assert Notification.objects.count() == 2

    def test_status_flips_to_completed(self):
        challenge = ChallengeFactory(status=Challenge.Status.ACTIVE)

        with (
            patch("challenges.services.sync_user_lifts"),
            patch("challenges.services.score_pooled_history"),
        ):
            close_challenge(challenge)

        challenge.refresh_from_db()
        assert challenge.status == Challenge.Status.COMPLETED

    def test_notifications_created_for_all_accepted_including_bailed(self):
        challenge = ChallengeFactory(status=Challenge.Status.ACTIVE)
        active = _accepted(challenge)
        bailed = _accepted(challenge, is_bailed=True)
        declined = ChallengeParticipantFactory(
            challenge=challenge,
            invite_status=ChallengeParticipant.InviteStatus.DECLINED,
        )

        with (
            patch("challenges.services.sync_user_lifts"),
            patch("challenges.services.score_pooled_history"),
        ):
            close_challenge(challenge)

        notified_users = set(
            Notification.objects.filter(
                event_type=Notification.EventType.CHALLENGE_CLOSED,
                challenge=challenge,
            ).values_list("user", flat=True)
        )
        assert notified_users == {active.user.pk, bailed.user.pk}
        assert declined.user.pk not in notified_users

    @pytest.mark.parametrize(
        "status", [Challenge.Status.COMPLETED, Challenge.Status.CANCELLED]
    )
    def test_terminal_challenge_is_noop(self, status):
        """A cancelled challenge must not be closable: flipping it to COMPLETED
        would resurrect a voided challenge and fire challenge_closed
        notifications for it."""
        challenge = ChallengeFactory(status=status)
        _accepted(challenge)

        with (
            patch("challenges.services.sync_user_lifts") as mock_sync,
            patch("challenges.services.score_pooled_history") as mock_score,
        ):
            close_challenge(challenge)

        mock_sync.assert_not_called()
        mock_score.assert_not_called()
        assert Notification.objects.count() == 0
        challenge.refresh_from_db()
        assert challenge.status == status
