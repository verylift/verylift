"""Tests for creator/staff removal of a participant (TASK-169)."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.test import Client
from django.urls import reverse

from accounts.tests.factories import UserFactory
from challenges.models import Challenge, ChallengeParticipant, CustomGoal
from challenges.services import remove_participant
from challenges.tests.factories import (
    ChallengeFactory,
    ChallengeInviteLinkFactory,
    ChallengeParticipantFactory,
    CustomGoalFactory,
)
from notifications.models import Notification
from scoring.services import rank_participants
from scoring.tests.factories import PointEarnEventFactory

InviteStatus = ChallengeParticipant.InviteStatus


@pytest.fixture
def challenge(db):
    return ChallengeFactory(status=Challenge.Status.ACTIVE)


@pytest.fixture
def creator_client(challenge):
    c = Client()
    c.force_login(challenge.creator)
    return c


@pytest.fixture
def participant(challenge):
    return ChallengeParticipantFactory(
        challenge=challenge,
        user=UserFactory(display_name="Alice"),
        invite_status=InviteStatus.ACCEPTED,
    )


@pytest.fixture
def mock_sync():
    """Patch the detail view's decoupled sync/score steps (no Liftosaur calls)."""
    with (
        patch("challenges.services.sync_user_lifts") as mock_pull,
        patch("challenges.services.score_pooled_history") as mock_score,
    ):
        yield SimpleNamespace(pull=mock_pull, score=mock_score)


class TestRemoveParticipantService:
    def test_sets_flags_and_notifies(self, challenge, participant):
        remove_participant(participant)

        participant.refresh_from_db()
        assert participant.is_bailed is True
        assert participant.bailed_at is not None
        assert participant.removed_by_creator is True
        assert (
            Notification.objects.filter(
                user=participant.user,
                challenge=challenge,
                event_type=Notification.EventType.REMOVED_FROM_CHALLENGE,
            ).count()
            == 1
        )

    def test_detaches_the_active_goal_without_deleting_it(self, challenge, participant):
        goal = CustomGoalFactory(participant=participant, name="My Goal")
        participant.custom_goal = goal
        participant.save(update_fields=["custom_goal"])

        remove_participant(participant)

        participant.refresh_from_db()
        goal.refresh_from_db()
        assert participant.custom_goal_id is None
        assert participant.has_goal_configured is False
        assert CustomGoal.objects.filter(pk=goal.pk).exists()
        assert goal.name != "My Goal"


class TestRemoveParticipantView:
    def test_get_renders_confirm_page(self, creator_client, challenge, participant):
        url = reverse("challenges:remove", args=[challenge.pk, participant.pk])
        response = creator_client.get(url)

        assert response.status_code == 200
        assert b"Remove Participant" in response.content
        assert b"Alice" in response.content
        participant.refresh_from_db()
        assert participant.is_bailed is False

    def test_confirm_copy_states_removal_is_permanent(
        self, creator_client, challenge, participant
    ):
        """TASK-272 AC#3: the copy no longer promises a re-invite readmits them
        — there is no re-invite action left, and nothing clears
        removed_by_creator."""
        url = reverse("challenges:remove", args=[challenge.pk, participant.pk])
        content = creator_client.get(url).content.decode()

        assert "They cannot rejoin, even with an invite link." in content
        assert "re-inviting" not in content.lower()

    def test_post_removes_and_redirects(self, creator_client, challenge, participant):
        url = reverse("challenges:remove", args=[challenge.pk, participant.pk])
        response = creator_client.post(url)

        assert response.status_code == 302
        assert response["Location"] == reverse(
            "challenges:settings", args=[challenge.pk]
        )
        participant.refresh_from_db()
        assert participant.is_bailed is True
        assert participant.removed_by_creator is True
        assert Notification.objects.filter(
            user=participant.user,
            challenge=challenge,
            event_type=Notification.EventType.REMOVED_FROM_CHALLENGE,
        ).exists()

    def test_removed_user_dropped_from_leaderboard(
        self, creator_client, challenge, participant
    ):
        # TASK-199 reverses the earlier frozen-but-visible decision: once a
        # participant is removed (is_bailed=True), their ledger is excluded from
        # the leaderboard even though the PointEarnEvent rows are preserved.
        PointEarnEventFactory(
            user=participant.user,
            challenge=challenge,
            points_earned=8,
            is_current_best=True,
        )
        url = reverse("challenges:remove", args=[challenge.pk, participant.pk])
        creator_client.post(url)

        leaderboard_users = {row["user"].pk for row in rank_participants(challenge)}
        assert participant.user.pk not in leaderboard_users

    def test_works_on_draft_challenge(self, db):
        challenge = ChallengeFactory(status=Challenge.Status.DRAFT)
        participant = ChallengeParticipantFactory(
            challenge=challenge,
            user=UserFactory(),
            invite_status=InviteStatus.ACCEPTED,
        )
        c = Client()
        c.force_login(challenge.creator)
        url = reverse("challenges:remove", args=[challenge.pk, participant.pk])

        assert c.post(url).status_code == 302
        participant.refresh_from_db()
        assert participant.removed_by_creator is True

    def test_completed_challenge_blocked(self, db):
        challenge = ChallengeFactory(status=Challenge.Status.COMPLETED)
        participant = ChallengeParticipantFactory(
            challenge=challenge,
            user=UserFactory(),
            invite_status=InviteStatus.ACCEPTED,
        )
        c = Client()
        c.force_login(challenge.creator)
        url = reverse("challenges:remove", args=[challenge.pk, participant.pk])

        assert c.post(url).status_code == 400
        participant.refresh_from_db()
        assert participant.is_bailed is False

    def test_cancelled_challenge_blocked(self, db):
        challenge = ChallengeFactory(status=Challenge.Status.CANCELLED)
        participant = ChallengeParticipantFactory(
            challenge=challenge,
            user=UserFactory(),
            invite_status=InviteStatus.ACCEPTED,
        )
        c = Client()
        c.force_login(challenge.creator)
        url = reverse("challenges:remove", args=[challenge.pk, participant.pk])

        assert c.post(url).status_code == 400

    def test_staff_override_can_remove(self, challenge, participant):
        staff = UserFactory(is_staff=True)
        c = Client()
        c.force_login(staff)
        url = reverse("challenges:remove", args=[challenge.pk, participant.pk])

        assert c.post(url).status_code == 302
        participant.refresh_from_db()
        assert participant.removed_by_creator is True

    def test_non_creator_non_staff_gets_403(self, challenge, participant):
        other = ChallengeParticipantFactory(
            challenge=challenge,
            user=UserFactory(),
            invite_status=InviteStatus.ACCEPTED,
        ).user
        c = Client()
        c.force_login(other)
        url = reverse("challenges:remove", args=[challenge.pk, participant.pk])

        assert c.post(url).status_code == 403

    def test_cannot_remove_creator_row(self, creator_client, challenge):
        creator_row = ChallengeParticipantFactory(
            challenge=challenge,
            user=challenge.creator,
            invite_status=InviteStatus.ACCEPTED,
        )
        url = reverse("challenges:remove", args=[challenge.pk, creator_row.pk])

        assert creator_client.post(url).status_code == 400

    def test_already_bailed_row_gets_400(self, creator_client, challenge):
        row = ChallengeParticipantFactory(
            challenge=challenge,
            user=UserFactory(),
            invite_status=InviteStatus.ACCEPTED,
            is_bailed=True,
        )
        url = reverse("challenges:remove", args=[challenge.pk, row.pk])

        assert creator_client.post(url).status_code == 400

    def test_invited_row_gets_400(self, creator_client, challenge):
        row = ChallengeParticipantFactory(
            challenge=challenge,
            user=UserFactory(),
            invite_status=InviteStatus.INVITED,
        )
        url = reverse("challenges:remove", args=[challenge.pk, row.pk])

        assert creator_client.post(url).status_code == 400

    def test_requires_login(self, challenge, participant):
        url = reverse("challenges:remove", args=[challenge.pk, participant.pk])
        response = Client().post(url)
        assert response.status_code == 302
        assert "/accounts/login/" in response["Location"]


class TestRemovedUserRejoinBlocked:
    """Removal is permanent (TASK-272): the invite link is the only way back in,
    and it refuses a creator-removed user."""

    def _link_url(self, challenge):
        link = ChallengeInviteLinkFactory(
            challenge=challenge, created_by=challenge.creator
        )
        return reverse("challenges:invite-link", args=[link.token])

    def test_removed_user_cannot_rejoin_with_invite_link(self, challenge, participant):
        remove_participant(participant)
        c = Client()
        c.force_login(participant.user)

        response = c.get(self._link_url(challenge))

        assert response.status_code == 400
        participant.refresh_from_db()
        assert participant.is_bailed is True

    def test_voluntarily_bailed_user_can_rejoin(self, challenge):
        user = UserFactory(liftosaur_api_key="test-liftosaur-key")
        row = ChallengeParticipantFactory(
            challenge=challenge,
            user=user,
            invite_status=InviteStatus.ACCEPTED,
            is_bailed=True,
        )
        c = Client()
        c.force_login(user)

        with patch("challenges.views._notify_user_joined"):
            response = c.get(self._link_url(challenge))

        assert response.status_code == 302
        row.refresh_from_db()
        assert row.is_bailed is False


class TestRemoveLinkRendering:
    @pytest.fixture
    def configured(self, db):
        challenge = ChallengeFactory(status=Challenge.Status.ACTIVE)
        ChallengeParticipantFactory(
            challenge=challenge,
            user=challenge.creator,
            invite_status=InviteStatus.ACCEPTED,
        )
        return challenge

    def test_remove_link_for_active_participant(self, configured, mock_sync):
        challenge = configured
        member = ChallengeParticipantFactory(
            challenge=challenge,
            user=UserFactory(),
            invite_status=InviteStatus.ACCEPTED,
        )
        c = Client()
        c.force_login(challenge.creator)

        response = c.get(reverse("challenges:settings", args=[challenge.pk]))

        assert (
            reverse("challenges:remove", args=[challenge.pk, member.pk]).encode()
            in response.content
        )

    def test_no_remove_link_for_creator_or_bailed(self, configured, mock_sync):
        challenge = configured
        creator_row = ChallengeParticipant.objects.get(
            challenge=challenge, user=challenge.creator
        )
        bailed = ChallengeParticipantFactory(
            challenge=challenge,
            user=UserFactory(),
            invite_status=InviteStatus.ACCEPTED,
            is_bailed=True,
        )
        c = Client()
        c.force_login(challenge.creator)

        response = c.get(reverse("challenges:settings", args=[challenge.pk]))

        assert (
            reverse("challenges:remove", args=[challenge.pk, creator_row.pk]).encode()
            not in response.content
        )
        assert (
            reverse("challenges:remove", args=[challenge.pk, bailed.pk]).encode()
            not in response.content
        )

    def test_no_remove_links_when_locked(self, db, mock_sync):
        challenge = ChallengeFactory(status=Challenge.Status.COMPLETED)
        ChallengeParticipantFactory(
            challenge=challenge,
            user=challenge.creator,
            invite_status=InviteStatus.ACCEPTED,
        )
        member = ChallengeParticipantFactory(
            challenge=challenge,
            user=UserFactory(),
            invite_status=InviteStatus.ACCEPTED,
        )
        c = Client()
        c.force_login(challenge.creator)

        response = c.get(reverse("challenges:settings", args=[challenge.pk]))

        assert (
            reverse("challenges:remove", args=[challenge.pk, member.pk]).encode()
            not in response.content
        )


class TestParticipantRowMenu:
    """The per-row actions live in a kebab dropdown (TASK-173)."""

    @pytest.fixture
    def configured(self, db):
        challenge = ChallengeFactory(status=Challenge.Status.ACTIVE)
        ChallengeParticipantFactory(
            challenge=challenge,
            user=challenge.creator,
            invite_status=InviteStatus.ACCEPTED,
        )
        return challenge

    def test_actionable_row_has_kebab_menu_and_badge(self, configured, mock_sync):
        challenge = configured
        ChallengeParticipantFactory(
            challenge=challenge,
            user=UserFactory(),
            invite_status=InviteStatus.ACCEPTED,
        )
        c = Client()
        c.force_login(challenge.creator)

        content = c.get(
            reverse("challenges:settings", args=[challenge.pk])
        ).content.decode()

        # Actions tuck into a per-row dropdown; accepted rows show no status badge.
        assert 'aria-haspopup="menu"' in content
        assert "Make owner" in content

    def test_locked_challenge_has_no_kebab_menu(self, db, mock_sync):
        challenge = ChallengeFactory(status=Challenge.Status.COMPLETED)
        ChallengeParticipantFactory(
            challenge=challenge,
            user=challenge.creator,
            invite_status=InviteStatus.ACCEPTED,
        )
        member = ChallengeParticipantFactory(
            challenge=challenge,
            user=UserFactory(display_name="Locked Member"),
            invite_status=InviteStatus.ACCEPTED,
        )
        c = Client()
        c.force_login(challenge.creator)

        content = c.get(
            reverse("challenges:settings", args=[challenge.pk])
        ).content.decode()

        assert 'aria-haspopup="menu"' not in content
        # The row itself still renders, just with no actions on it.
        assert "Locked Member" in content
        assert reverse("challenges:remove", args=[challenge.pk, member.pk]) not in (
            content
        )
