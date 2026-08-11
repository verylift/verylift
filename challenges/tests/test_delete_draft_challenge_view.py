"""Tests for deleting a draft challenge (#1)."""

import pytest
from django.test import Client
from django.urls import reverse

from accounts.tests.factories import UserFactory
from challenges.models import Challenge, ChallengeParticipant
from challenges.tests.factories import ChallengeFactory, ChallengeParticipantFactory


@pytest.fixture
def draft(db):
    return ChallengeFactory(status=Challenge.Status.DRAFT)


@pytest.fixture
def creator_client(draft):
    c = Client()
    c.force_login(draft.creator)
    return c


class TestDeleteDraftChallengeView:
    def test_post_soft_deletes_draft_and_redirects_to_find(self, creator_client, draft):
        url = reverse("challenges:delete-draft", args=[draft.pk])
        response = creator_client.post(url)
        assert response.status_code == 302
        assert response["Location"] == reverse("challenges:find")

        draft.refresh_from_db()
        assert draft.status == Challenge.Status.CANCELLED
        # Soft delete only -- the row itself is never removed.
        assert Challenge.objects.filter(pk=draft.pk).exists()

    def test_requires_login(self, db, draft):
        url = reverse("challenges:delete-draft", args=[draft.pk])
        response = Client().post(url)
        assert response.status_code == 302
        assert "/accounts/login/" in response["Location"]

    def test_non_creator_gets_403_and_challenge_untouched(self, db, draft):
        outsider = UserFactory()
        c = Client()
        c.force_login(outsider)
        url = reverse("challenges:delete-draft", args=[draft.pk])
        response = c.post(url)
        assert response.status_code == 403

        draft.refresh_from_db()
        assert draft.status == Challenge.Status.DRAFT

    def test_non_draft_challenge_rejected_with_400(self, db):
        active = ChallengeFactory(status=Challenge.Status.ACTIVE)
        c = Client()
        c.force_login(active.creator)
        url = reverse("challenges:delete-draft", args=[active.pk])
        response = c.post(url)
        assert response.status_code == 400

        active.refresh_from_db()
        assert active.status == Challenge.Status.ACTIVE

    def test_completed_challenge_rejected_with_400(self, db):
        completed = ChallengeFactory(status=Challenge.Status.COMPLETED)
        c = Client()
        c.force_login(completed.creator)
        url = reverse("challenges:delete-draft", args=[completed.pk])
        response = c.post(url)
        assert response.status_code == 400

        completed.refresh_from_db()
        assert completed.status == Challenge.Status.COMPLETED

    def test_get_is_not_allowed(self, creator_client, draft):
        url = reverse("challenges:delete-draft", args=[draft.pk])
        response = creator_client.get(url)
        assert response.status_code == 405

    def test_deleted_draft_disappears_from_find_challenges(self, creator_client, draft):
        ChallengeParticipantFactory(
            challenge=draft,
            user=draft.creator,
            invite_status=ChallengeParticipant.InviteStatus.ACCEPTED,
        )
        url = reverse("challenges:delete-draft", args=[draft.pk])
        creator_client.post(url)

        response = creator_client.get(reverse("challenges:find"))
        pks = [row["challenge"].pk for row in response.context["rows"]]
        assert draft.pk not in pks

    def test_htmx_post_returns_200_with_no_redirect(self, creator_client, draft):
        url = reverse("challenges:delete-draft", args=[draft.pk])
        response = creator_client.post(url, HTTP_HX_REQUEST="true")
        assert response.status_code == 200

        draft.refresh_from_db()
        assert draft.status == Challenge.Status.CANCELLED
