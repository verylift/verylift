"""Tests for the owner-facing point-eligible-window control (TASK-247).

Point-eligible window now defaults to FROM_START at creation and can only be
changed afterward, from the challenge's Settings page -- e.g. to admit a late
joiner fairly by switching to FROM_JOIN mid-challenge.
"""

import pytest
from django.test import Client
from django.urls import reverse

from accounts.tests.factories import UserFactory
from challenges.models import Challenge
from challenges.tests.factories import ChallengeFactory


@pytest.fixture
def challenge(db):
    return ChallengeFactory(
        status=Challenge.Status.ACTIVE,
        history_window=Challenge.HistoryWindow.FROM_START,
    )


@pytest.fixture
def creator_client(challenge):
    c = Client()
    c.force_login(challenge.creator)
    return c


class TestHistoryWindowView:
    def test_creator_can_change_window(self, creator_client, challenge):
        url = reverse("challenges:history-window", args=[challenge.pk])
        response = creator_client.post(
            url, {"history_window": Challenge.HistoryWindow.FROM_JOIN}
        )
        assert response.status_code == 302
        assert response["Location"] == reverse(
            "challenges:settings", args=[challenge.pk]
        )
        challenge.refresh_from_db()
        assert challenge.history_window == Challenge.HistoryWindow.FROM_JOIN

    def test_creator_can_change_window_htmx(self, creator_client, challenge):
        url = reverse("challenges:history-window", args=[challenge.pk])
        response = creator_client.post(
            url,
            {"history_window": Challenge.HistoryWindow.FROM_JOIN},
            HTTP_HX_REQUEST="true",
        )
        assert response.status_code == 200
        content = response.content.decode()
        assert "From join date" in content
        assert 'name="history_window"' not in content
        challenge.refresh_from_db()
        assert challenge.history_window == Challenge.HistoryWindow.FROM_JOIN

    def test_non_creator_gets_403(self, challenge):
        other = UserFactory()
        c = Client()
        c.force_login(other)
        url = reverse("challenges:history-window", args=[challenge.pk])
        response = c.post(url, {"history_window": Challenge.HistoryWindow.FROM_JOIN})
        assert response.status_code == 403
        challenge.refresh_from_db()
        assert challenge.history_window == Challenge.HistoryWindow.FROM_START

    def test_staff_non_creator_can_change_window(self, challenge):
        staff = UserFactory(is_staff=True)
        c = Client()
        c.force_login(staff)
        url = reverse("challenges:history-window", args=[challenge.pk])
        response = c.post(url, {"history_window": Challenge.HistoryWindow.FROM_JOIN})
        assert response.status_code == 302
        challenge.refresh_from_db()
        assert challenge.history_window == Challenge.HistoryWindow.FROM_JOIN

    @pytest.mark.parametrize(
        "status",
        [Challenge.Status.COMPLETED, Challenge.Status.CANCELLED],
    )
    def test_locked_challenge_rejects_change(self, db, status):
        challenge = ChallengeFactory(
            status=status, history_window=Challenge.HistoryWindow.FROM_START
        )
        c = Client()
        c.force_login(challenge.creator)
        url = reverse("challenges:history-window", args=[challenge.pk])
        response = c.post(url, {"history_window": Challenge.HistoryWindow.FROM_JOIN})
        assert response.status_code == 400
        challenge.refresh_from_db()
        assert challenge.history_window == Challenge.HistoryWindow.FROM_START

    def test_invalid_choice_rejected(self, creator_client, challenge):
        url = reverse("challenges:history-window", args=[challenge.pk])
        response = creator_client.post(
            url, {"history_window": "not_a_real_choice"}, HTTP_HX_REQUEST="true"
        )
        assert response.status_code == 200
        challenge.refresh_from_db()
        assert challenge.history_window == Challenge.HistoryWindow.FROM_START

    def test_pencil_click_returns_edit_mode(self, creator_client, challenge):
        url = reverse("challenges:history-window", args=[challenge.pk])
        response = creator_client.get(url, {"edit": "1"}, HTTP_HX_REQUEST="true")
        assert response.status_code == 200
        content = response.content.decode()
        assert 'name="history_window"' in content

    def test_cancel_returns_display_mode(self, creator_client, challenge):
        url = reverse("challenges:history-window", args=[challenge.pk])
        response = creator_client.get(url, HTTP_HX_REQUEST="true")
        assert response.status_code == 200
        content = response.content.decode()
        assert 'name="history_window"' not in content
        assert "From challenge start" in content


class TestSettingsPageShowsHistoryWindow:
    def test_settings_page_renders_current_window(self, creator_client, challenge):
        url = reverse("challenges:settings", args=[challenge.pk])
        response = creator_client.get(url)
        assert response.status_code == 200
        assert "From challenge start" in response.content.decode()


class TestWizardCreatedChallengeDefaultsWindow:
    def test_fresh_wizard_created_challenge_defaults_to_from_start(self, db):
        creator = UserFactory()
        c = Client()
        c.force_login(creator)
        from liftosaur.models import Lift

        bench = Lift.objects.get(name="Bench Press")
        url = reverse("challenges:create")
        c.post(url, {"name": "Wizard Default Test"})
        c.post(url, {"start_date": "2027-03-01", "end_date": "2027-06-01"})
        c.post(url, {"standard": "custom"})
        c.post(url, {"lifts": [str(bench.pk)]})
        c.post(url, {"invitees": []})
        challenge = Challenge.objects.get(creator=creator)
        assert challenge.history_window == Challenge.HistoryWindow.FROM_START
