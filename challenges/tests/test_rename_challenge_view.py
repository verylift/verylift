"""Tests for the creator-only challenge rename control (TASK-201)."""

import pytest
from django.test import Client
from django.urls import reverse

from accounts.tests.factories import UserFactory
from challenges.models import Challenge
from challenges.tests.factories import ChallengeFactory


@pytest.fixture
def challenge(db):
    return ChallengeFactory(status=Challenge.Status.ACTIVE, name="Original Name")


@pytest.fixture
def creator_client(challenge):
    c = Client()
    c.force_login(challenge.creator)
    return c


class TestRenameChallengeView:
    def test_creator_can_rename(self, creator_client, challenge):
        url = reverse("challenges:rename", args=[challenge.pk])
        response = creator_client.post(url, {"name": "New Name"})
        assert response.status_code == 302
        assert response["Location"] == reverse(
            "challenges:settings", args=[challenge.pk]
        )
        challenge.refresh_from_db()
        assert challenge.name == "New Name"

    def test_creator_can_rename_htmx(self, creator_client, challenge):
        url = reverse("challenges:rename", args=[challenge.pk])
        response = creator_client.post(
            url, {"name": "HTMX Name"}, HTTP_HX_REQUEST="true"
        )
        assert response.status_code == 200
        content = response.content.decode()
        assert "HTMX Name" in content
        assert 'name="name"' not in content
        challenge.refresh_from_db()
        assert challenge.name == "HTMX Name"

    def test_non_creator_gets_403(self, challenge):
        other = UserFactory()
        c = Client()
        c.force_login(other)
        url = reverse("challenges:rename", args=[challenge.pk])
        response = c.post(url, {"name": "Hijacked"})
        assert response.status_code == 403
        challenge.refresh_from_db()
        assert challenge.name == "Original Name"

    def test_staff_non_creator_can_rename(self, challenge):
        staff = UserFactory(is_staff=True)
        c = Client()
        c.force_login(staff)
        url = reverse("challenges:rename", args=[challenge.pk])
        response = c.post(url, {"name": "Staff Rename"})
        assert response.status_code == 302
        challenge.refresh_from_db()
        assert challenge.name == "Staff Rename"

    @pytest.mark.parametrize(
        "status",
        [Challenge.Status.COMPLETED, Challenge.Status.CANCELLED],
    )
    def test_locked_challenge_rejects_rename(self, db, status):
        challenge = ChallengeFactory(status=status, name="Locked Name")
        c = Client()
        c.force_login(challenge.creator)
        url = reverse("challenges:rename", args=[challenge.pk])
        response = c.post(url, {"name": "Attempted Rename"})
        assert response.status_code == 400
        challenge.refresh_from_db()
        assert challenge.name == "Locked Name"

    def test_empty_name_rejected(self, creator_client, challenge):
        url = reverse("challenges:rename", args=[challenge.pk])
        response = creator_client.post(url, {"name": "   "}, HTTP_HX_REQUEST="true")
        assert response.status_code == 200
        content = response.content.decode()
        assert 'name="name"' in content
        assert "This field is required." in content
        challenge.refresh_from_db()
        assert challenge.name == "Original Name"

    def test_pencil_click_returns_edit_mode(self, creator_client, challenge):
        url = reverse("challenges:rename", args=[challenge.pk])
        response = creator_client.get(url, {"edit": "1"}, HTTP_HX_REQUEST="true")
        assert response.status_code == 200
        content = response.content.decode()
        assert 'name="name"' in content
        assert f'value="{challenge.name}"' in content

    def test_cancel_returns_display_mode(self, creator_client, challenge):
        url = reverse("challenges:rename", args=[challenge.pk])
        response = creator_client.get(url, HTTP_HX_REQUEST="true")
        assert response.status_code == 200
        content = response.content.decode()
        assert 'name="name"' not in content
        assert challenge.name in content
