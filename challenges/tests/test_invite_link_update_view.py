"""Tests for adjusting a live invite link's expiry/max-uses in place (TASK-300)."""

from datetime import timedelta

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from accounts.tests.factories import UserFactory
from challenges.models import Challenge
from challenges.services import current_invite_link
from challenges.tests.factories import ChallengeFactory, ChallengeInviteLinkFactory


@pytest.fixture
def challenge(db):
    return ChallengeFactory(status=Challenge.Status.ACTIVE)


@pytest.fixture
def creator_client(challenge):
    c = Client()
    c.force_login(challenge.creator)
    return c


class TestUpdateInviteLinkView:
    def test_creator_can_update_in_place(self, creator_client, challenge):
        link = ChallengeInviteLinkFactory(
            challenge=challenge,
            revoked_at=None,
            expires_at=timezone.now() + timezone.timedelta(days=7),
            max_uses=None,
        )
        new_expiry = timezone.now() + timedelta(days=2)
        url = reverse("challenges:update-invite-link", args=[challenge.pk])
        response = creator_client.post(
            url,
            {"expires_at": new_expiry.strftime("%Y-%m-%dT%H:%M"), "max_uses": "5"},
        )

        assert response.status_code == 302
        link.refresh_from_db()
        assert link.max_uses == 5
        assert abs((link.expires_at - new_expiry).total_seconds()) < 60
        # Same row, same token -- unlike regenerate, the link itself is untouched.
        assert current_invite_link(challenge).pk == link.pk
        assert current_invite_link(challenge).token == link.token

    def test_update_htmx_returns_section(self, creator_client, challenge):
        link = ChallengeInviteLinkFactory(challenge=challenge, revoked_at=None)
        url = reverse("challenges:update-invite-link", args=[challenge.pk])
        response = creator_client.post(
            url, {"expires_at": "", "max_uses": "3"}, HTTP_HX_REQUEST="true"
        )

        assert response.status_code == 200
        content = response.content.decode()
        assert link.token in content
        link.refresh_from_db()
        assert link.max_uses == 3

    def test_no_live_link_404s(self, creator_client, challenge):
        url = reverse("challenges:update-invite-link", args=[challenge.pk])
        response = creator_client.post(url, {"expires_at": "", "max_uses": ""})
        assert response.status_code == 404

    def test_non_creator_gets_403(self, challenge):
        ChallengeInviteLinkFactory(challenge=challenge, revoked_at=None)
        other = UserFactory()
        c = Client()
        c.force_login(other)
        url = reverse("challenges:update-invite-link", args=[challenge.pk])
        response = c.post(url, {"expires_at": "", "max_uses": ""})
        assert response.status_code == 403

    @pytest.mark.parametrize(
        "status", [Challenge.Status.COMPLETED, Challenge.Status.CANCELLED]
    )
    def test_terminal_challenge_rejects_update(self, db, status):
        challenge = ChallengeFactory(status=status)
        ChallengeInviteLinkFactory(challenge=challenge, revoked_at=None)
        c = Client()
        c.force_login(challenge.creator)
        url = reverse("challenges:update-invite-link", args=[challenge.pk])
        response = c.post(url, {"expires_at": "", "max_uses": ""})
        assert response.status_code == 400

    def test_ended_but_still_active_challenge_rejects_update(self, db):
        """end_date has passed but close_challenges hasn't flipped status yet
        (a real window: the scheduler runs on a ~30-minute cadence) -- the
        guard must reject on its own live instant check, not on is_terminal."""
        challenge = ChallengeFactory(
            status=Challenge.Status.ACTIVE,
            end_date=(timezone.now() - timedelta(days=1)).date(),
        )
        link = ChallengeInviteLinkFactory(challenge=challenge, revoked_at=None)
        c = Client()
        c.force_login(challenge.creator)
        url = reverse("challenges:update-invite-link", args=[challenge.pk])
        response = c.post(url, {"expires_at": "", "max_uses": "5"})
        assert response.status_code == 400
        link.refresh_from_db()
        assert link.max_uses is None

    def test_get_without_edit_param_renders_display_mode(
        self, creator_client, challenge
    ):
        link = ChallengeInviteLinkFactory(challenge=challenge, revoked_at=None)
        url = reverse("challenges:update-invite-link", args=[challenge.pk])
        response = creator_client.get(url, HTTP_HX_REQUEST="true")

        assert response.status_code == 200
        content = response.content.decode()
        assert link.token in content
        # Display mode shows the pencil trigger, not the editable inputs.
        assert 'id="id_expires_at"' not in content
        assert "?edit=1" in content

    def test_get_with_edit_param_renders_edit_mode(self, creator_client, challenge):
        ChallengeInviteLinkFactory(challenge=challenge, revoked_at=None)
        url = reverse("challenges:update-invite-link", args=[challenge.pk])
        response = creator_client.get(url, {"edit": "1"}, HTTP_HX_REQUEST="true")

        assert response.status_code == 200
        assert 'id="id_expires_at"' in response.content.decode()

    def test_get_non_htmx_redirects_to_settings(self, creator_client, challenge):
        ChallengeInviteLinkFactory(challenge=challenge, revoked_at=None)
        url = reverse("challenges:update-invite-link", args=[challenge.pk])
        response = creator_client.get(url, {"edit": "1"})
        assert response.status_code == 302
        assert response["Location"] == reverse(
            "challenges:settings", args=[challenge.pk]
        )

    def test_successful_save_returns_to_display_mode(self, creator_client, challenge):
        ChallengeInviteLinkFactory(challenge=challenge, revoked_at=None)
        url = reverse("challenges:update-invite-link", args=[challenge.pk])
        response = creator_client.post(
            url, {"expires_at": "", "max_uses": "2"}, HTTP_HX_REQUEST="true"
        )

        assert response.status_code == 200
        assert 'id="id_expires_at"' not in response.content.decode()

    def test_invalid_save_stays_in_edit_mode_with_errors(
        self, creator_client, challenge
    ):
        ChallengeInviteLinkFactory(challenge=challenge, revoked_at=None)
        url = reverse("challenges:update-invite-link", args=[challenge.pk])
        past = timezone.now() - timedelta(days=1)
        response = creator_client.post(
            url,
            {"expires_at": past.strftime("%Y-%m-%dT%H:%M"), "max_uses": ""},
            HTTP_HX_REQUEST="true",
        )

        assert response.status_code == 200
        content = response.content.decode()
        assert 'id="id_expires_at"' in content
        assert b"future" in response.content

    def test_zero_max_uses_is_rejected(self, creator_client, challenge):
        link = ChallengeInviteLinkFactory(
            challenge=challenge, revoked_at=None, max_uses=None
        )
        url = reverse("challenges:update-invite-link", args=[challenge.pk])
        response = creator_client.post(
            url, {"expires_at": "", "max_uses": "0"}, HTTP_HX_REQUEST="true"
        )

        assert response.status_code == 200
        assert 'id="id_expires_at"' in response.content.decode()
        link.refresh_from_db()
        assert link.max_uses is None

    def test_past_expiry_is_rejected(self, creator_client, challenge):
        link = ChallengeInviteLinkFactory(
            challenge=challenge,
            revoked_at=None,
            expires_at=timezone.now() + timezone.timedelta(days=7),
        )
        url = reverse("challenges:update-invite-link", args=[challenge.pk])
        past = timezone.now() - timedelta(days=1)
        response = creator_client.post(
            url, {"expires_at": past.strftime("%Y-%m-%dT%H:%M"), "max_uses": ""}
        )
        assert response.status_code == 302
        link.refresh_from_db()
        assert link.expires_at > timezone.now()

    def test_expiry_past_the_challenge_end_date_is_rejected(self, db):
        challenge = ChallengeFactory(
            status=Challenge.Status.ACTIVE,
            end_date=(timezone.now() + timedelta(days=3)).date(),
        )
        link = ChallengeInviteLinkFactory(
            challenge=challenge,
            revoked_at=None,
            expires_at=timezone.now() + timedelta(days=1),
        )
        c = Client()
        c.force_login(challenge.creator)
        url = reverse("challenges:update-invite-link", args=[challenge.pk])
        past_end = timezone.now() + timedelta(days=10)
        response = c.post(
            url,
            {"expires_at": past_end.strftime("%Y-%m-%dT%H:%M"), "max_uses": ""},
            HTTP_HX_REQUEST="true",
        )

        assert response.status_code == 200
        assert b"ends" in response.content
        link.refresh_from_db()
        assert link.expires_at < past_end
