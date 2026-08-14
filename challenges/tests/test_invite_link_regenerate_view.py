"""Tests for the owner-facing invite-link regenerate action (TASK-249, TASK-300)."""

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


class TestRegenerateInviteLinkView:
    def test_creator_can_regenerate(self, creator_client, challenge):
        old = ChallengeInviteLinkFactory(
            challenge=challenge,
            revoked_at=None,
            expires_at=timezone.now() + timezone.timedelta(days=7),
        )
        url = reverse("challenges:regenerate-invite-link", args=[challenge.pk])
        response = creator_client.post(url)

        assert response.status_code == 302
        assert response["Location"] == reverse(
            "challenges:settings", args=[challenge.pk]
        )
        old.refresh_from_db()
        assert old.revoked_at is not None
        new_link = current_invite_link(challenge)
        assert new_link is not None
        assert new_link.pk != old.pk

    def test_creator_can_regenerate_htmx(self, creator_client, challenge):
        url = reverse("challenges:regenerate-invite-link", args=[challenge.pk])
        response = creator_client.post(url, HTTP_HX_REQUEST="true")

        assert response.status_code == 200
        content = response.content.decode()
        current = current_invite_link(challenge)
        assert current is not None
        assert current.token in content

    def test_non_creator_gets_403(self, challenge):
        other = UserFactory()
        c = Client()
        c.force_login(other)
        url = reverse("challenges:regenerate-invite-link", args=[challenge.pk])
        response = c.post(url)
        assert response.status_code == 403

    def test_staff_non_creator_gets_403(self, challenge):
        """No staff override — matches share_challenge_view (a social action,
        not moderation)."""
        staff = UserFactory(is_staff=True)
        c = Client()
        c.force_login(staff)
        url = reverse("challenges:regenerate-invite-link", args=[challenge.pk])
        response = c.post(url)
        assert response.status_code == 403

    @pytest.mark.parametrize(
        "status", [Challenge.Status.COMPLETED, Challenge.Status.CANCELLED]
    )
    def test_terminal_challenge_rejects_regenerate(self, db, status):
        challenge = ChallengeFactory(status=status)
        c = Client()
        c.force_login(challenge.creator)
        url = reverse("challenges:regenerate-invite-link", args=[challenge.pk])
        response = c.post(url)
        assert response.status_code == 400

    def test_get_not_allowed(self, creator_client, challenge):
        url = reverse("challenges:regenerate-invite-link", args=[challenge.pk])
        response = creator_client.get(url)
        assert response.status_code == 405

    def test_regenerate_uses_defaults_when_there_is_no_incumbent_link(
        self, creator_client, challenge
    ):
        url = reverse("challenges:regenerate-invite-link", args=[challenge.pk])
        response = creator_client.post(url)
        assert response.status_code == 302
        link = current_invite_link(challenge)
        assert link.max_uses is None

    def test_regenerate_carries_forward_the_incumbents_expiry_and_max_uses(
        self, creator_client, challenge
    ):
        """Regenerating is "give me a new URL", not "reset my settings" --
        a custom expiry/max-uses set via the edit pencil survives a
        regenerate, only the token and use_count are actually fresh."""
        custom_expiry = timezone.now() + timezone.timedelta(days=3)
        old = ChallengeInviteLinkFactory(
            challenge=challenge,
            revoked_at=None,
            expires_at=custom_expiry,
            max_uses=5,
            use_count=2,
        )
        url = reverse("challenges:regenerate-invite-link", args=[challenge.pk])
        response = creator_client.post(url)

        assert response.status_code == 302
        old.refresh_from_db()
        assert old.revoked_at is not None
        new_link = current_invite_link(challenge)
        assert new_link.pk != old.pk
        assert new_link.max_uses == 5
        assert new_link.expires_at == custom_expiry
        assert new_link.use_count == 0
