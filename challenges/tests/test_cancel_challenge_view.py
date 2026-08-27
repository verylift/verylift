"""Tests for the cancel-challenge view (TASK-57)."""

import pytest
from django.test import Client
from django.urls import reverse

from accounts.tests.factories import UserFactory
from challenges.models import Challenge
from challenges.tests.factories import ChallengeFactory


@pytest.fixture
def challenge(db):
    return ChallengeFactory(status=Challenge.Status.ACTIVE)


@pytest.fixture
def creator_client(challenge):
    c = Client()
    c.force_login(challenge.creator)
    return c


class TestCancelChallengeView:
    def test_get_renders_confirmation_page(self, creator_client, challenge):
        url = reverse("challenges:cancel", args=[challenge.pk])
        response = creator_client.get(url)
        assert response.status_code == 200
        assert any(
            t.name == "challenges/confirm_action.html" for t in response.templates
        )
        assert url.encode() in response.content
        assert challenge.name.encode() in response.content
        challenge.refresh_from_db()
        assert challenge.status == Challenge.Status.ACTIVE

    def test_post_sets_status_cancelled_and_redirects_to_dashboard(
        self, creator_client, challenge
    ):
        url = reverse("challenges:cancel", args=[challenge.pk])
        response = creator_client.post(url)
        assert response.status_code == 302
        assert response["Location"] == reverse("challenges:dashboard")
        challenge.refresh_from_db()
        assert challenge.status == Challenge.Status.CANCELLED

    def test_requires_login(self, db, challenge):
        url = reverse("challenges:cancel", args=[challenge.pk])
        response = Client().get(url)
        assert response.status_code == 302
        assert "/accounts/login/" in response["Location"]

    def test_non_creator_gets_403(self, db, challenge):
        outsider = UserFactory()
        c = Client()
        c.force_login(outsider)
        url = reverse("challenges:cancel", args=[challenge.pk])
        assert c.get(url).status_code == 403
        assert c.post(url).status_code == 403

    def test_staff_can_cancel_challenge_they_did_not_create(self, db, challenge):
        staff = UserFactory(is_staff=True)
        c = Client()
        c.force_login(staff)
        url = reverse("challenges:cancel", args=[challenge.pk])
        assert c.get(url).status_code == 200
        response = c.post(url)
        assert response.status_code == 302
        assert response["Location"] == reverse("challenges:dashboard")
        challenge.refresh_from_db()
        assert challenge.status == Challenge.Status.CANCELLED

    def test_already_completed_gets_400(self, db):
        challenge = ChallengeFactory(status=Challenge.Status.COMPLETED)
        c = Client()
        c.force_login(challenge.creator)
        url = reverse("challenges:cancel", args=[challenge.pk])
        assert c.get(url).status_code == 400
        assert c.post(url).status_code == 400
        challenge.refresh_from_db()
        assert challenge.status == Challenge.Status.COMPLETED

    def test_already_cancelled_gets_400(self, db):
        challenge = ChallengeFactory(status=Challenge.Status.CANCELLED)
        c = Client()
        c.force_login(challenge.creator)
        url = reverse("challenges:cancel", args=[challenge.pk])
        assert c.get(url).status_code == 400
        assert c.post(url).status_code == 400
