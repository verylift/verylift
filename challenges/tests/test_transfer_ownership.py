"""Tests for transferring challenge ownership (TASK-170)."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.test import Client
from django.urls import reverse

from accounts.tests.factories import UserFactory
from challenges.models import Challenge, ChallengeInviteLink, ChallengeParticipant
from challenges.services import activate_draft_for_creator, transfer_ownership
from challenges.tests.factories import (
    ChallengeFactory,
    ChallengeParticipantFactory,
    CustomGoalFactory,
)
from notifications.models import Notification
from notifications.views import build_display_text

InviteStatus = ChallengeParticipant.InviteStatus


@pytest.fixture
def challenge(db):
    """An ACTIVE challenge whose creator has an accepted participant row."""
    comp = ChallengeFactory(status=Challenge.Status.ACTIVE)
    ChallengeParticipantFactory(
        challenge=comp,
        user=comp.creator,
        invite_status=InviteStatus.ACCEPTED,
    )
    return comp


@pytest.fixture
def new_owner(challenge):
    """An accepted, non-bailed participant eligible to become owner."""
    return ChallengeParticipantFactory(
        challenge=challenge,
        user=UserFactory(),
        invite_status=InviteStatus.ACCEPTED,
    ).user


@pytest.fixture
def creator_client(challenge):
    c = Client()
    c.force_login(challenge.creator)
    return c


@pytest.fixture
def mock_sync():
    """Patch the detail view's decoupled sync/score steps (no Liftosaur calls)."""
    with (
        patch("challenges.services.sync_user_lifts") as mock_pull,
        patch("challenges.services.score_pooled_history") as mock_score,
    ):
        yield SimpleNamespace(pull=mock_pull, score=mock_score)


def transfer_url(challenge, user):
    return reverse("challenges:transfer", args=[challenge.pk, user.pk])


class TestTransferOwnershipService:
    def test_reassigns_creator_and_notifies_new_owner(self, challenge, new_owner):
        old_creator = challenge.creator
        old_row = ChallengeParticipant.objects.get(
            challenge=challenge, user=old_creator
        )
        new_row = ChallengeParticipant.objects.get(challenge=challenge, user=new_owner)

        transfer_ownership(challenge, new_owner)

        challenge.refresh_from_db()
        assert challenge.creator_id == new_owner.id

        notifications = Notification.objects.filter(
            user=new_owner,
            challenge=challenge,
            event_type=Notification.EventType.OWNERSHIP_TRANSFERRED,
        )
        assert notifications.count() == 1

        # No other participant/challenge fields changed (AC #2).
        old_row.refresh_from_db()
        new_row.refresh_from_db()
        assert old_row.invite_status == InviteStatus.ACCEPTED
        assert old_row.is_bailed is False
        assert new_row.invite_status == InviteStatus.ACCEPTED
        assert new_row.is_bailed is False
        assert Notification.objects.filter(user=old_creator).count() == 0

    def test_build_display_text_for_new_event(self, challenge, new_owner):
        transfer_ownership(challenge, new_owner)
        notification = Notification.objects.get(
            user=new_owner,
            event_type=Notification.EventType.OWNERSHIP_TRANSFERRED,
        )
        assert build_display_text(notification) == (
            f"You are now the owner of {challenge.name}"
        )


class TestTransferOwnershipView:
    def test_creator_get_renders_confirmation(
        self, creator_client, challenge, new_owner
    ):
        new_owner.display_name = "Pickle Rick"
        new_owner.save(update_fields=["display_name"])

        response = creator_client.get(transfer_url(challenge, new_owner))

        assert response.status_code == 200
        assert b"Transfer Ownership" in response.content
        assert b"Pickle Rick" in response.content
        challenge.refresh_from_db()
        assert challenge.creator_id != new_owner.id

    def test_creator_post_transfers_and_redirects_to_detail(
        self, creator_client, challenge, new_owner
    ):
        response = creator_client.post(transfer_url(challenge, new_owner))

        assert response.status_code == 302
        assert response["Location"] == reverse("challenges:detail", args=[challenge.pk])
        challenge.refresh_from_db()
        assert challenge.creator_id == new_owner.id
        assert Notification.objects.filter(
            user=new_owner,
            challenge=challenge,
            event_type=Notification.EventType.OWNERSHIP_TRANSFERRED,
        ).exists()

    def test_post_transfer_redirect_destination_loads_for_ex_creator(
        self, creator_client, challenge, new_owner, mock_sync
    ):
        """Following the post-transfer redirect must not land the ex-creator on a
        403 — transfer must never send the actor to the creator-only settings
        page they can no longer access."""
        response = creator_client.post(transfer_url(challenge, new_owner), follow=True)

        assert response.status_code == 200
        challenge.refresh_from_db()
        assert challenge.creator_id == new_owner.id

    def test_requires_login(self, db, challenge, new_owner):
        response = Client().get(transfer_url(challenge, new_owner))
        assert response.status_code == 302
        assert "/accounts/login/" in response["Location"]

    def test_completed_challenge_blocked(self, db):
        comp = ChallengeFactory(status=Challenge.Status.COMPLETED)
        target = ChallengeParticipantFactory(
            challenge=comp, invite_status=InviteStatus.ACCEPTED
        ).user
        c = Client()
        c.force_login(comp.creator)
        url = transfer_url(comp, target)
        assert c.get(url).status_code == 400
        assert c.post(url).status_code == 400
        comp.refresh_from_db()
        assert comp.creator_id != target.id

    def test_cancelled_challenge_blocked(self, db):
        comp = ChallengeFactory(status=Challenge.Status.CANCELLED)
        target = ChallengeParticipantFactory(
            challenge=comp, invite_status=InviteStatus.ACCEPTED
        ).user
        c = Client()
        c.force_login(comp.creator)
        url = transfer_url(comp, target)
        assert c.get(url).status_code == 400
        assert c.post(url).status_code == 400

    def test_non_participant_target_404(self, creator_client, challenge):
        stranger = UserFactory()
        assert creator_client.get(transfer_url(challenge, stranger)).status_code == 404
        assert creator_client.post(transfer_url(challenge, stranger)).status_code == 404

    def test_invited_target_blocked(self, creator_client, challenge):
        invited = ChallengeParticipantFactory(
            challenge=challenge, invite_status=InviteStatus.INVITED
        ).user
        assert creator_client.get(transfer_url(challenge, invited)).status_code == 400
        assert creator_client.post(transfer_url(challenge, invited)).status_code == 400

    def test_declined_target_blocked(self, creator_client, challenge):
        declined = ChallengeParticipantFactory(
            challenge=challenge, invite_status=InviteStatus.DECLINED
        ).user
        assert creator_client.post(transfer_url(challenge, declined)).status_code == 400

    def test_bailed_target_blocked(self, creator_client, challenge):
        bailed = ChallengeParticipantFactory(
            challenge=challenge,
            invite_status=InviteStatus.ACCEPTED,
            is_bailed=True,
        ).user
        assert creator_client.post(transfer_url(challenge, bailed)).status_code == 400

    def test_inactive_target_blocked(self, creator_client, challenge):
        inactive = ChallengeParticipantFactory(
            challenge=challenge,
            user=UserFactory(is_active=False),
            invite_status=InviteStatus.ACCEPTED,
        ).user
        assert creator_client.post(transfer_url(challenge, inactive)).status_code == 400

    def test_transfer_to_self_blocked(self, creator_client, challenge):
        url = transfer_url(challenge, challenge.creator)
        assert creator_client.get(url).status_code == 400
        assert creator_client.post(url).status_code == 400

    def test_non_creator_non_staff_gets_403(self, challenge, new_owner):
        c = Client()
        c.force_login(new_owner)
        url = transfer_url(challenge, new_owner)
        assert c.get(url).status_code == 403
        assert c.post(url).status_code == 403

    def test_staff_override_redirects_to_dashboard(self, challenge, new_owner):
        staff = UserFactory(is_staff=True)
        c = Client()
        c.force_login(staff)

        response = c.post(transfer_url(challenge, new_owner))

        assert response.status_code == 302
        assert response["Location"] == reverse("challenges:dashboard")
        challenge.refresh_from_db()
        assert challenge.creator_id == new_owner.id

    def test_post_transfer_authorization_swaps(self, challenge, new_owner):
        old_owner = challenge.creator
        old_client = Client()
        old_client.force_login(old_owner)
        new_client = Client()
        new_client.force_login(new_owner)

        old_client.post(transfer_url(challenge, new_owner))

        share_url = reverse("challenges:share", args=[challenge.pk])
        regenerate_url = reverse(
            "challenges:regenerate-invite-link", args=[challenge.pk]
        )

        # Old owner loses creator-only access.
        assert (
            old_client.get(reverse("challenges:close", args=[challenge.pk])).status_code
            == 403
        )
        assert old_client.get(share_url).status_code == 403
        assert old_client.post(regenerate_url).status_code == 403

        # New owner gains it.
        assert (
            new_client.get(reverse("challenges:close", args=[challenge.pk])).status_code
            == 200
        )
        assert new_client.get(share_url).status_code == 200
        assert new_client.post(regenerate_url).status_code == 302
        assert ChallengeInviteLink.objects.filter(
            challenge=challenge, created_by=new_owner, revoked_at__isnull=True
        ).exists()

    def test_draft_transfer_and_activation_follows_new_owner(self, db):
        comp = ChallengeFactory(status=Challenge.Status.DRAFT)
        old_owner = comp.creator
        ChallengeParticipantFactory(
            challenge=comp, user=old_owner, invite_status=InviteStatus.ACCEPTED
        )
        new_owner = ChallengeParticipantFactory(
            challenge=comp, invite_status=InviteStatus.ACCEPTED
        ).user
        c = Client()
        c.force_login(old_owner)

        response = c.post(transfer_url(comp, new_owner))
        assert response.status_code == 302

        comp.refresh_from_db()
        assert comp.creator_id == new_owner.id
        # Old owner can no longer activate; new owner can (AC #5).
        assert activate_draft_for_creator(comp, old_owner) is False
        assert activate_draft_for_creator(comp, new_owner) is True
        comp.refresh_from_db()
        assert comp.status == Challenge.Status.ACTIVE


class TestTransferLinkVisibility:
    @pytest.fixture
    def configured_challenge(self, db):
        comp = ChallengeFactory(status=Challenge.Status.ACTIVE)
        participant = ChallengeParticipantFactory(
            challenge=comp,
            user=comp.creator,
            invite_status=InviteStatus.ACCEPTED,
        )
        goal = CustomGoalFactory(participant=participant)
        participant.custom_goal = goal
        participant.save(update_fields=["custom_goal"])
        return comp

    def test_eligible_row_shows_make_owner_link(self, configured_challenge, mock_sync):
        comp = configured_challenge
        eligible = ChallengeParticipantFactory(
            challenge=comp,
            user=UserFactory(),
            invite_status=InviteStatus.ACCEPTED,
        ).user
        c = Client()
        c.force_login(comp.creator)

        response = c.get(reverse("challenges:settings", args=[comp.pk]))

        assert transfer_url(comp, eligible).encode() in response.content
        # Never a self-transfer link for the creator's own row.
        assert transfer_url(comp, comp.creator).encode() not in response.content

    def test_ineligible_rows_have_no_link(self, configured_challenge, mock_sync):
        comp = configured_challenge
        invited = ChallengeParticipantFactory(
            challenge=comp,
            user=UserFactory(),
            invite_status=InviteStatus.INVITED,
        ).user
        declined = ChallengeParticipantFactory(
            challenge=comp,
            user=UserFactory(),
            invite_status=InviteStatus.DECLINED,
        ).user
        bailed = ChallengeParticipantFactory(
            challenge=comp,
            user=UserFactory(),
            invite_status=InviteStatus.ACCEPTED,
            is_bailed=True,
        ).user
        inactive = ChallengeParticipantFactory(
            challenge=comp,
            user=UserFactory(is_active=False),
            invite_status=InviteStatus.ACCEPTED,
        ).user
        c = Client()
        c.force_login(comp.creator)

        response = c.get(reverse("challenges:settings", args=[comp.pk]))

        for user in (invited, declined, bailed, inactive):
            assert transfer_url(comp, user).encode() not in response.content

    def test_detail_shows_owner_label(self, configured_challenge, mock_sync):
        comp = configured_challenge
        c = Client()
        c.force_login(comp.creator)

        response = c.get(reverse("challenges:detail", args=[comp.pk]))

        assert b"Owner:" in response.content
        assert b"Created by" not in response.content
