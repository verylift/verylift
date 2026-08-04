"""Tests for the /challenges/ list page (TASK-30, rescoped by TASK-272).

The page used to be a public directory with a Join action. TASK-272 removed
open challenges, so it now lists only the challenges the viewer is a member of
— including DRAFT ones, which have no dashboard bucket — and offers no join
action at all. Joining lives entirely on the invite-link path, covered by
test_invite_link_view.py.
"""

import pytest
from django.test import Client
from django.urls import reverse

from accounts.tests.factories import UserFactory
from challenges.models import Challenge, ChallengeParticipant
from challenges.tests.factories import (
    ChallengeFactory,
    ChallengeParticipantFactory,
)
from scoring.tests.factories import PointEarnEventFactory

InviteStatus = ChallengeParticipant.InviteStatus


@pytest.fixture
def user(db):
    return UserFactory(liftosaur_api_key="test-liftosaur-key")


@pytest.fixture
def auth_client(user):
    c = Client()
    c.force_login(user)
    return c


def joined(user, **kwargs):
    """A challenge ``user`` is an accepted, non-bailed member of."""
    challenge = ChallengeFactory(**kwargs)
    ChallengeParticipantFactory(
        challenge=challenge,
        user=user,
        invite_status=InviteStatus.ACCEPTED,
    )
    return challenge


def row_pks(response):
    return [row["challenge"].pk for row in response.context["rows"]]


class TestFindChallengesView:
    def test_requires_login(self, db):
        response = Client().get(reverse("challenges:find"))
        assert response.status_code == 302
        assert "/accounts/login/" in response["Location"]

    def test_lists_active_and_completed(self, auth_client, user):
        active = joined(user, status=Challenge.Status.ACTIVE)
        completed = joined(user, status=Challenge.Status.COMPLETED)
        response = auth_client.get(reverse("challenges:find"))
        assert active.pk in row_pks(response)
        assert completed.pk in row_pks(response)

    def test_includes_draft_the_user_joined(self, auth_client, user):
        """A DRAFT challenge joined by link has no dashboard bucket, so this
        page is its only in-app route (step 4a)."""
        draft = joined(user, status=Challenge.Status.DRAFT)
        response = auth_client.get(reverse("challenges:find"))
        assert draft.pk in row_pks(response)
        assert b"Draft" in response.content

    def test_excludes_challenge_the_user_is_not_in(self, auth_client):
        outsider = ChallengeFactory(status=Challenge.Status.ACTIVE)
        response = auth_client.get(reverse("challenges:find"))
        assert outsider.pk not in row_pks(response)

    def test_excludes_challenge_the_user_bailed_from(self, auth_client, user):
        challenge = ChallengeFactory(status=Challenge.Status.ACTIVE)
        ChallengeParticipantFactory(
            challenge=challenge,
            user=user,
            invite_status=InviteStatus.ACCEPTED,
            is_bailed=True,
        )
        response = auth_client.get(reverse("challenges:find"))
        assert challenge.pk not in row_pks(response)

    def test_excludes_legacy_invited_row(self, auth_client, user):
        """Membership is ACCEPTED-and-not-bailed everywhere else in the app; a
        leftover INVITED row would link to a detail page that 403s."""
        challenge = ChallengeFactory(status=Challenge.Status.ACTIVE)
        ChallengeParticipantFactory(
            challenge=challenge,
            user=user,
            invite_status=InviteStatus.INVITED,
        )
        response = auth_client.get(reverse("challenges:find"))
        assert challenge.pk not in row_pks(response)

    def test_excludes_cancelled(self, auth_client, user):
        cancelled = joined(user, status=Challenge.Status.CANCELLED)
        response = auth_client.get(reverse("challenges:find"))
        assert cancelled.pk not in row_pks(response)

    def test_appears_once(self, auth_client, user):
        challenge = joined(user, status=Challenge.Status.ACTIVE)
        response = auth_client.get(reverse("challenges:find"))
        assert row_pks(response).count(challenge.pk) == 1

    def test_hide_completed_shows_only_active(self, auth_client, user):
        active = joined(user, status=Challenge.Status.ACTIVE)
        completed = joined(user, status=Challenge.Status.COMPLETED)
        draft = joined(user, status=Challenge.Status.DRAFT)
        response = auth_client.get(reverse("challenges:find") + "?hide_completed=1")
        assert active.pk in row_pks(response)
        assert completed.pk not in row_pks(response)
        assert draft.pk not in row_pks(response)
        assert response.context["hide_completed"] is True

    def test_default_does_not_hide_completed(self, auth_client):
        response = auth_client.get(reverse("challenges:find"))
        assert response.context["hide_completed"] is False

    def test_no_join_action_rendered(self, auth_client, user):
        """Joining only happens through an invite link now."""
        joined(user, status=Challenge.Status.ACTIVE)
        content = auth_client.get(reverse("challenges:find")).content.decode()
        assert ">Join<" not in content
        assert ">Accept<" not in content
        assert ">Decline<" not in content

    def test_detail_link_carries_loading_message(self, auth_client, user):
        # The challenge name links to the detail page via a plain GET
        # navigation, so it is tagged for the TASK-141 loading overlay.
        joined(user, status=Challenge.Status.ACTIVE)
        content = auth_client.get(reverse("challenges:find")).content.decode()
        assert 'data-loading-message="Loading challenge…"' in content

    def test_pagination_preserves_hide_completed(self, auth_client, user):
        for _ in range(25):
            joined(user, status=Challenge.Status.ACTIVE)
        response = auth_client.get(reverse("challenges:find") + "?hide_completed=1")
        assert b"?page=2&hide_completed=1" in response.content

    def test_ordered_by_end_date_desc(self, auth_client, user):
        early = joined(user, status=Challenge.Status.ACTIVE, end_date="2026-01-01")
        late = joined(user, status=Challenge.Status.ACTIVE, end_date="2026-12-01")
        ordered = row_pks(auth_client.get(reverse("challenges:find")))
        assert ordered.index(late.pk) < ordered.index(early.pk)

    def test_leader_column_populated(self, auth_client, user):
        challenge = joined(user, status=Challenge.Status.ACTIVE)
        leader = UserFactory(display_name="Top Lifter")
        ChallengeParticipantFactory(
            challenge=challenge,
            user=leader,
            invite_status=InviteStatus.ACCEPTED,
        )
        PointEarnEventFactory(
            user=leader,
            challenge=challenge,
            points_earned=9,
            is_current_best=True,
        )
        response = auth_client.get(reverse("challenges:find"))
        row = next(
            r for r in response.context["rows"] if r["challenge"].pk == challenge.pk
        )
        assert row["leader_name"] == "Top Lifter"
        assert row["leader_points"] == 9

    def test_leader_column_no_scores(self, auth_client, user):
        challenge = joined(user, status=Challenge.Status.ACTIVE)
        response = auth_client.get(reverse("challenges:find"))
        row = next(
            r for r in response.context["rows"] if r["challenge"].pk == challenge.pk
        )
        assert row["leader_name"] is None
        assert row["leader_points"] is None
        assert b"No scores yet" in response.content

    def test_pagination_twenty_per_page(self, auth_client, user):
        for _ in range(25):
            joined(user, status=Challenge.Status.ACTIVE)
        response = auth_client.get(reverse("challenges:find"))
        assert len(response.context["rows"]) == 20
        assert response.context["page_obj"].has_next() is True

        page2 = auth_client.get(reverse("challenges:find") + "?page=2")
        assert len(page2.context["rows"]) == 5


class TestKeylessUserCanBrowse:
    """A keyless user can browse the platform (TASK-250, AC#2)."""

    def test_keyless_user_can_view_find_challenges(self, db):
        user = UserFactory(liftosaur_api_key=None)
        c = Client()
        c.force_login(user)
        joined(user, status=Challenge.Status.ACTIVE)
        response = c.get(reverse("challenges:find"))
        assert response.status_code == 200
