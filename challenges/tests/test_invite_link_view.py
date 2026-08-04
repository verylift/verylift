"""Tests for the invite-link landing/join view (TASK-249)."""

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from accounts.tests.factories import UserFactory
from challenges.models import Challenge, ChallengeParticipant
from challenges.tests.factories import (
    ChallengeFactory,
    ChallengeInviteLinkFactory,
    ChallengeParticipantFactory,
)

InviteStatus = ChallengeParticipant.InviteStatus


@pytest.fixture
def challenge(db):
    return ChallengeFactory(status=Challenge.Status.ACTIVE)


@pytest.fixture
def link(challenge):
    return ChallengeInviteLinkFactory(
        challenge=challenge,
        revoked_at=None,
        expires_at=timezone.now() + timedelta(days=7),
    )


class TestUnknownToken:
    def test_unknown_token_404s(self, db):
        response = Client().get(reverse("challenges:invite-link", args=["nope"]))
        assert response.status_code == 404


class TestExpiredOrRevokedToken:
    def test_expired_token_renders_invalid_page(self, challenge):
        link = ChallengeInviteLinkFactory(
            challenge=challenge,
            revoked_at=None,
            expires_at=timezone.now() - timedelta(seconds=1),
        )
        response = Client().get(reverse("challenges:invite-link", args=[link.token]))
        assert response.status_code == 200
        assert challenge.name.encode() in response.content

    def test_revoked_token_renders_invalid_page(self, challenge):
        link = ChallengeInviteLinkFactory(
            challenge=challenge,
            revoked_at=timezone.now(),
            expires_at=timezone.now() + timedelta(days=7),
        )
        response = Client().get(reverse("challenges:invite-link", args=[link.token]))
        assert response.status_code == 200
        assert challenge.name.encode() in response.content

    def test_expired_token_works_for_an_authenticated_visitor_too(self, challenge):
        link = ChallengeInviteLinkFactory(
            challenge=challenge,
            revoked_at=None,
            expires_at=timezone.now() - timedelta(seconds=1),
        )
        c = Client()
        c.force_login(UserFactory())
        response = c.get(reverse("challenges:invite-link", args=[link.token]))
        assert response.status_code == 200
        assert challenge.name.encode() in response.content


class TestAnonymousVisitor:
    def test_stashes_token_in_session_and_redirects_to_register(self, challenge, link):
        c = Client()
        response = c.get(reverse("challenges:invite-link", args=[link.token]))
        assert response.status_code == 302
        assert response["Location"] == reverse("accounts:register")
        assert c.session["invite_token"] == link.token

    def test_does_not_auto_create_an_account(self, challenge, link):
        from django.contrib.auth import get_user_model

        User = get_user_model()
        before = User.objects.count()
        Client().get(reverse("challenges:invite-link", args=[link.token]))
        assert User.objects.count() == before


@pytest.mark.django_db
class TestAuthenticatedFreshJoin:
    def test_join_creates_accepted_participant_and_records_provenance(
        self, challenge, link
    ):
        user = UserFactory(liftosaur_api_key="test-key")
        c = Client()
        c.force_login(user)
        response = c.get(reverse("challenges:invite-link", args=[link.token]))

        assert response.status_code == 302
        assert response["Location"] == reverse(
            "challenges:goal-setup", args=[challenge.pk]
        )
        participant = ChallengeParticipant.objects.get(challenge=challenge, user=user)
        assert participant.invite_status == InviteStatus.ACCEPTED
        assert participant.joined_via_link_id == link.pk

    def test_join_clears_the_session_token(self, challenge, link):
        user = UserFactory(liftosaur_api_key="test-key")
        c = Client()
        c.force_login(user)
        session = c.session
        session["invite_token"] = link.token
        session.save()
        c.get(reverse("challenges:invite-link", args=[link.token]))
        assert "invite_token" not in c.session

    def test_draft_challenge_is_joinable(self, link):
        challenge = link.challenge
        challenge.status = Challenge.Status.DRAFT
        challenge.save(update_fields=["status"])
        user = UserFactory(liftosaur_api_key="test-key")
        c = Client()
        c.force_login(user)
        response = c.get(reverse("challenges:invite-link", args=[link.token]))
        assert response.status_code == 302
        assert ChallengeParticipant.objects.filter(
            challenge=challenge, user=user
        ).exists()

    @pytest.mark.parametrize(
        "status", [Challenge.Status.COMPLETED, Challenge.Status.CANCELLED]
    )
    def test_terminal_challenge_is_not_joinable(self, link, status):
        challenge = link.challenge
        challenge.status = status
        challenge.save(update_fields=["status"])
        user = UserFactory(liftosaur_api_key="test-key")
        c = Client()
        c.force_login(user)
        response = c.get(reverse("challenges:invite-link", args=[link.token]))
        assert response.status_code == 400


@pytest.mark.django_db
class TestIdempotentRevisit:
    def test_existing_accepted_participant_redirected_to_detail(self, challenge, link):
        user = UserFactory()
        ChallengeParticipantFactory(
            challenge=challenge,
            user=user,
            invite_status=InviteStatus.ACCEPTED,
            joined_at=datetime.now(tz=UTC),
        )
        c = Client()
        c.force_login(user)
        response = c.get(reverse("challenges:invite-link", args=[link.token]))
        assert response.status_code == 302
        assert response["Location"] == reverse("challenges:detail", args=[challenge.pk])


@pytest.mark.django_db
class TestVoluntaryBailRejoin:
    def test_rejoin_resets_participant_and_records_new_link(self, challenge, link):
        user = UserFactory(liftosaur_api_key="test-key")
        participant = ChallengeParticipantFactory(
            challenge=challenge,
            user=user,
            invite_status=InviteStatus.ACCEPTED,
            joined_at=datetime(2025, 1, 1, tzinfo=UTC),
            is_bailed=True,
            bailed_at=datetime(2025, 1, 2, tzinfo=UTC),
            removed_by_creator=False,
        )
        c = Client()
        c.force_login(user)
        response = c.get(reverse("challenges:invite-link", args=[link.token]))
        assert response.status_code == 302

        participant.refresh_from_db()
        assert participant.is_bailed is False
        assert participant.bailed_at is None
        assert participant.invite_status == InviteStatus.ACCEPTED
        assert participant.joined_via_link_id == link.pk


@pytest.mark.django_db
class TestRemovedByCreatorBlocked:
    def test_removed_participant_cannot_rejoin_via_link(self, challenge, link):
        user = UserFactory(liftosaur_api_key="test-key")
        participant = ChallengeParticipantFactory(
            challenge=challenge,
            user=user,
            invite_status=InviteStatus.ACCEPTED,
            joined_at=datetime(2025, 1, 1, tzinfo=UTC),
            is_bailed=True,
            bailed_at=datetime(2025, 1, 2, tzinfo=UTC),
            removed_by_creator=True,
        )
        c = Client()
        c.force_login(user)
        response = c.get(reverse("challenges:invite-link", args=[link.token]))
        assert response.status_code == 400

        participant.refresh_from_db()
        assert participant.is_bailed is True
        assert participant.removed_by_creator is True


@pytest.mark.django_db
class TestLiftosaurKeyGate:
    def test_keyless_visitor_gets_inline_prompt_with_correct_action_url(
        self, challenge, link
    ):
        user = UserFactory(liftosaur_api_key=None)
        c = Client()
        c.force_login(user)
        response = c.get(reverse("challenges:invite-link", args=[link.token]))

        assert response.status_code == 200
        content = response.content.decode()
        assert "liftosaur_api_key" in content
        assert reverse("challenges:invite-link", args=[link.token]) in content
        assert not ChallengeParticipant.objects.filter(
            challenge=challenge, user=user
        ).exists()

    def test_keyless_visitor_htmx_gets_inline_prompt_partial(self, challenge, link):
        user = UserFactory(liftosaur_api_key=None)
        c = Client()
        c.force_login(user)
        response = c.get(
            reverse("challenges:invite-link", args=[link.token]),
            HTTP_HX_REQUEST="true",
        )
        assert response.status_code == 200
        assert "liftosaur_api_key" in response.content.decode()

    @patch("challenges.views.trigger_lift_history_backfill")
    @patch("challenges.views.validate_liftosaur_key", return_value=True)
    def test_submitting_a_key_completes_the_join(
        self, mock_validate, mock_backfill, challenge, link
    ):
        user = UserFactory(liftosaur_api_key=None)
        c = Client()
        c.force_login(user)
        response = c.post(
            reverse("challenges:invite-link", args=[link.token]),
            {"liftosaur_api_key": "brand-new-key"},
        )
        assert response.status_code == 302
        mock_validate.assert_called_once_with("brand-new-key")
        user.refresh_from_db()
        assert user.liftosaur_api_key == "brand-new-key"
        participant = ChallengeParticipant.objects.get(challenge=challenge, user=user)
        assert participant.joined_via_link_id == link.pk
