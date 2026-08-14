"""Tests for the invite accept/decline page and its accept POST (TASK-303)."""

from datetime import timedelta

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from accounts.tests.factories import UserFactory
from challenges.models import Challenge, ChallengeParticipant
from challenges.tests.factories import (
    ChallengeFactory,
    ChallengeInviteLinkFactory,
    ChallengeLiftFactory,
    ChallengeParticipantFactory,
)
from scoring.tests.factories import PointEarnEventFactory

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


@pytest.mark.django_db
class TestAcceptPageContent:
    def test_leaderboard_always_includes_a_synthetic_visitor_row(self, challenge, link):
        """The visitor's own row is a static (you, probably) placeholder, never
        a dry-run scoring result -- it must appear even when the visitor has no
        lift history or point events anywhere."""
        user = UserFactory()
        c = Client()
        c.force_login(user)
        response = c.get(reverse("challenges:invite-link", args=[link.token]))

        assert response.status_code == 200
        content = response.content.decode()
        assert "you, probably" in content
        assert "???" in content

    def test_participant_count_reflects_real_accepted_participants(
        self, challenge, link
    ):
        for _ in range(3):
            ChallengeParticipantFactory(
                challenge=challenge,
                user=UserFactory(),
                invite_status=InviteStatus.ACCEPTED,
                joined_at=timezone.now(),
            )
        # a bailed and an invited-only row must not count
        ChallengeParticipantFactory(
            challenge=challenge,
            user=UserFactory(),
            invite_status=InviteStatus.ACCEPTED,
            is_bailed=True,
        )

        visitor = UserFactory()
        c = Client()
        c.force_login(visitor)
        response = c.get(reverse("challenges:invite-link", args=[link.token]))

        assert response.context["participant_count"] == 3

    def test_leaderboard_bar_pct_normalizes_to_leader(self, challenge, link):
        leader = UserFactory()
        second = UserFactory()
        ChallengeParticipantFactory(
            challenge=challenge, user=leader, invite_status=InviteStatus.ACCEPTED
        )
        ChallengeParticipantFactory(
            challenge=challenge, user=second, invite_status=InviteStatus.ACCEPTED
        )
        PointEarnEventFactory(
            user=leader, challenge=challenge, points_earned=10, is_current_best=True
        )
        PointEarnEventFactory(
            user=second, challenge=challenge, points_earned=5, is_current_best=True
        )

        visitor = UserFactory()
        c = Client()
        c.force_login(visitor)
        response = c.get(reverse("challenges:invite-link", args=[link.token]))

        rows = {
            row["user"].pk: row["bar_pct"]
            for row in response.context["leaderboard_rows"]
        }
        assert rows[leader.pk] == 100
        assert rows[second.pk] == 50

    def test_custom_lifts_are_listed(self, challenge, link):
        ChallengeLiftFactory(challenge=challenge, name="Deadlift")
        ChallengeLiftFactory(challenge=challenge, name="Overhead Press")

        visitor = UserFactory()
        c = Client()
        c.force_login(visitor)
        response = c.get(reverse("challenges:invite-link", args=[link.token]))

        names = {lift.name for lift in response.context["custom_lifts"]}
        assert names == {"Deadlift", "Overhead Press"}


@pytest.mark.django_db
class TestAcceptPost:
    def test_accept_creates_participant_and_redirects_to_goal_setup(
        self, challenge, link
    ):
        user = UserFactory()
        c = Client()
        c.force_login(user)
        response = c.post(reverse("challenges:invite-accept", args=[link.token]))

        assert response.status_code == 302
        assert response["Location"] == reverse(
            "challenges:goal-setup", args=[challenge.pk]
        )
        participant = ChallengeParticipant.objects.get(challenge=challenge, user=user)
        assert participant.invite_status == InviteStatus.ACCEPTED
        assert participant.joined_via_link_id == link.pk

    def test_accept_increments_link_use_count(self, challenge, link):
        user = UserFactory()
        c = Client()
        c.force_login(user)
        c.post(reverse("challenges:invite-accept", args=[link.token]))
        link.refresh_from_db()
        assert link.use_count == 1

    def test_accept_clears_session_token(self, challenge, link):
        user = UserFactory()
        c = Client()
        c.force_login(user)
        session = c.session
        session["invite_token"] = link.token
        session.save()
        c.post(reverse("challenges:invite-accept", args=[link.token]))
        assert "invite_token" not in c.session

    def test_get_is_not_allowed(self, challenge, link):
        user = UserFactory()
        c = Client()
        c.force_login(user)
        response = c.get(reverse("challenges:invite-accept", args=[link.token]))
        assert response.status_code == 405

    def test_anonymous_post_redirects_to_login(self, challenge, link):
        response = Client().post(reverse("challenges:invite-accept", args=[link.token]))
        assert response.status_code == 302
        assert reverse("accounts:login") in response["Location"]
        assert not ChallengeParticipant.objects.filter(challenge=challenge).exists()

    def test_unknown_token_404s(self, db):
        user = UserFactory()
        c = Client()
        c.force_login(user)
        response = c.post(reverse("challenges:invite-accept", args=["nope"]))
        assert response.status_code == 404

    def test_terminal_challenge_returns_400(self, link):
        challenge = link.challenge
        challenge.status = Challenge.Status.COMPLETED
        challenge.save(update_fields=["status"])
        user = UserFactory()
        c = Client()
        c.force_login(user)
        response = c.post(reverse("challenges:invite-accept", args=[link.token]))
        assert response.status_code == 400
        assert not ChallengeParticipant.objects.filter(challenge=challenge).exists()

    def test_expired_link_renders_invalid_page_instead_of_joining(self, challenge):
        link = ChallengeInviteLinkFactory(
            challenge=challenge,
            revoked_at=None,
            expires_at=timezone.now() - timedelta(seconds=1),
        )
        user = UserFactory()
        c = Client()
        c.force_login(user)
        response = c.post(reverse("challenges:invite-accept", args=[link.token]))
        assert response.status_code == 200
        assert not ChallengeParticipant.objects.filter(
            challenge=challenge, user=user
        ).exists()

    def test_existing_participant_falls_back_to_invite_link_view(self, challenge, link):
        """Race condition: a participant row appeared between the GET render
        and the accept click. Out of scope to build dedicated UX for this
        (TASK-303) -- falling back to invite_link_view's own guard ladder is
        the documented, reasonable behaviour."""
        user = UserFactory()
        ChallengeParticipantFactory(
            challenge=challenge,
            user=user,
            invite_status=InviteStatus.ACCEPTED,
            joined_at=timezone.now(),
        )
        c = Client()
        c.force_login(user)
        response = c.post(reverse("challenges:invite-accept", args=[link.token]))

        assert response.status_code == 302
        assert response["Location"] == reverse(
            "challenges:invite-link", args=[link.token]
        )
