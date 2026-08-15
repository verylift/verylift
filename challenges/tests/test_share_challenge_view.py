"""Tests for the post-creation share screen (TASK-272 AC#4).

The screen a new owner lands on straight out of the create wizard: it renders
the challenge's brand-new invite link so the link is actually seen, now that
there is no invitee step and no Find Users page to reach people through.
"""

import uuid

import pytest
from django.test import Client
from django.urls import reverse

from accounts.tests.factories import UserFactory
from challenges.models import Challenge
from challenges.services import current_invite_link, regenerate_invite_link
from challenges.tests.factories import ChallengeFactory


@pytest.fixture
def challenge(db):
    return ChallengeFactory(status=Challenge.Status.DRAFT)


@pytest.fixture
def creator_client(challenge):
    c = Client()
    c.force_login(challenge.creator)
    return c


def share_url(challenge):
    return reverse("challenges:share", args=[challenge.pk])


class TestShareChallengeViewAccess:
    def test_anonymous_redirected_to_login(self, challenge):
        response = Client().get(share_url(challenge))
        assert response.status_code == 302
        assert "/accounts/login/" in response["Location"]

    def test_creator_gets_200(self, creator_client, challenge):
        assert creator_client.get(share_url(challenge)).status_code == 200

    def test_non_creator_gets_403(self, challenge):
        c = Client()
        c.force_login(UserFactory())
        assert c.get(share_url(challenge)).status_code == 403

    def test_staff_non_creator_gets_403(self, challenge):
        """No staff override — matches regenerate_invite_link_view: handing out
        a join capability is a social action, not moderation."""
        c = Client()
        c.force_login(UserFactory(is_staff=True))
        assert c.get(share_url(challenge)).status_code == 403

    def test_unknown_pk_404(self, db):
        c = Client()
        c.force_login(UserFactory())
        url = reverse("challenges:share", args=[uuid.uuid4()])
        assert c.get(url).status_code == 404

    def test_post_not_allowed(self, creator_client, challenge):
        assert creator_client.post(share_url(challenge)).status_code == 405


class TestShareChallengeViewContent:
    def test_renders_the_live_invite_link(self, creator_client, challenge):
        link = regenerate_invite_link(challenge, challenge.creator)

        response = creator_client.get(share_url(challenge))

        assert response.context["current_invite_link"] == link
        content = response.content.decode()
        assert reverse("challenges:invite-link", args=[link.token]) in content
        assert "Copy" in content

    def test_offers_generation_when_no_live_link(self, creator_client, challenge):
        assert current_invite_link(challenge) is None

        content = creator_client.get(share_url(challenge)).content.decode()

        assert "No active invite link" in content
        assert reverse("challenges:regenerate-invite-link", args=[challenge.pk]) in (
            content
        )

    def test_links_on_to_goal_setup(self, creator_client, challenge):
        content = creator_client.get(share_url(challenge)).content.decode()
        assert reverse("challenges:goal-setup", args=[challenge.pk]) in content
        assert "Continue to your chart" in content

    def test_includes_open_graph_title_with_challenge_name(
        self, creator_client, challenge
    ):
        content = creator_client.get(share_url(challenge)).content.decode()
        assert (
            f'property="og:title" content="Join {challenge.name} on very lift"'
            in content
        )
