"""Tests for the single current bodyweight on User (TASK-343).

Covers the three surfaces that read or write it -- Settings, the onboarding
step, and tracker sync -- plus the model helper they all go through. The
goal-setup wizard's just-in-time half lives with the wizard's own tests, in
challenges/tests/test_goal_setup_bodyweight.py.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from accounts.services import anonymize_account, sync_bodyweight_from_trackers
from accounts.tests.factories import UserFactory
from core.bodyweight import TrackerBodyweight

pytestmark = pytest.mark.django_db


@pytest.fixture
def user():
    return UserFactory(unit_preference="kg")


@pytest.fixture
def authed_client(user):
    c = Client()
    c.force_login(user)
    return c


def _reading(weight, *, when):
    return TrackerBodyweight(weight_kg=Decimal(weight), measured_at=when)


class TestSetBodyweight:
    def test_clearing_drops_the_provenance_with_the_value(self, user):
        # A source and a timestamp describing a value that no longer exists
        # would read as "synced from your tracker" next to an empty field.
        user.set_bodyweight(Decimal("80"), User.BodyweightSource.TRACKER)
        user.set_bodyweight(None, User.BodyweightSource.MANUAL)

        user.refresh_from_db()
        assert user.bodyweight_kg is None
        assert user.bodyweight_source == ""
        assert user.bodyweight_updated_at is None


class TestSettingsBodyweightSection:
    def _post(self, client, value):
        return client.post(
            reverse("accounts:settings"),
            {"form_name": "bodyweight", "bodyweight": value},
        )

    def test_entry_is_stored_in_kg_from_the_display_unit(self, authed_client, user):
        user.unit_preference = "lb"
        user.save(update_fields=["unit_preference"])

        self._post(authed_client, "180")

        user.refresh_from_db()
        # 180 lb, not 180 kg -- the form carries no unit of its own and must
        # read the account's preference.
        assert user.bodyweight_kg == Decimal("81.65")
        assert user.bodyweight_source == User.BodyweightSource.MANUAL

    def test_round_trips_through_the_form_without_drift(self, authed_client, user):
        user.unit_preference = "lb"
        user.save(update_fields=["unit_preference"])

        self._post(authed_client, "183.5")
        response = authed_client.get(reverse("accounts:settings"))

        assert response.context["bodyweight"] == Decimal("183.5")

    def test_blank_submission_clears_a_stored_value(self, authed_client, user):
        user.set_bodyweight(Decimal("80"), User.BodyweightSource.MANUAL)

        self._post(authed_client, "")

        user.refresh_from_db()
        assert user.bodyweight_kg is None

    @pytest.mark.parametrize("value", ["0", "-5", "heavy"])
    def test_rejected_values_are_not_stored_and_report_an_error(
        self, authed_client, user, value
    ):
        response = self._post(authed_client, value)

        assert response.status_code == 200
        assert response.context["bodyweight_error"]
        # What was typed stays on screen next to the error rather than
        # snapping back to the stored value.
        assert response.context["bodyweight"] == value
        user.refresh_from_db()
        assert user.bodyweight_kg is None


class TestOnboardingBodyweightStep:
    def _url(self):
        return reverse("accounts:onboarding-bodyweight")

    def test_skip_stores_nothing_and_continues(self, authed_client, user):
        response = authed_client.post(self._url(), {"bodyweight": "80", "skip": "1"})

        assert response["Location"] == reverse("accounts:onboarding-very-open")
        user.refresh_from_db()
        assert user.bodyweight_kg is None

    def test_blank_submission_is_treated_as_a_skip(self, authed_client, user):
        response = authed_client.post(self._url(), {"bodyweight": ""})

        assert response["Location"] == reverse("accounts:onboarding-very-open")
        user.refresh_from_db()
        assert user.bodyweight_kg is None

    def test_entry_is_stored_and_continues(self, authed_client, user):
        response = authed_client.post(self._url(), {"bodyweight": "82.5"})

        assert response["Location"] == reverse("accounts:onboarding-very-open")
        user.refresh_from_db()
        assert user.bodyweight_kg == Decimal("82.50")

    def test_invalid_entry_re_renders_instead_of_advancing(self, authed_client):
        response = authed_client.post(self._url(), {"bodyweight": "-1"})

        assert response.status_code == 200
        assert response.context["bodyweight_error"]

    def test_get_prefills_from_a_connected_tracker(self, authed_client, user):
        user.liftosaur_api_key = "key"
        user.save(update_fields=["liftosaur_api_key"])

        with patch(
            "accounts.views.sync_bodyweight_from_trackers", return_value=True
        ) as mock_sync:
            response = authed_client.get(self._url())

        mock_sync.assert_called_once()
        assert response.context["prefilled_from_tracker"] is True

    def test_get_does_not_call_trackers_when_a_value_is_already_stored(
        self, authed_client, user
    ):
        # Revisiting this page must not re-hit three APIs to re-derive an
        # answer already on file.
        user.liftosaur_api_key = "key"
        user.save(update_fields=["liftosaur_api_key"])
        user.set_bodyweight(Decimal("80"), User.BodyweightSource.MANUAL)

        with patch("accounts.views.sync_bodyweight_from_trackers") as mock_sync:
            response = authed_client.get(self._url())

        mock_sync.assert_not_called()
        assert response.context["bodyweight"] == Decimal("80")


class TestSyncBodyweightFromTrackers:
    @pytest.fixture
    def connected_user(self):
        return UserFactory(
            liftosaur_api_key="lift-key",
            hevy_api_key="hevy-key",
            wger_instance_url="https://wger.example.com",
            wger_api_token="wger-token",
        )

    def _run(self, user, *, liftosaur=None, wger=None, hevy=None):
        with (
            patch(
                "accounts.services.liftosaur_services.fetch_latest_bodyweight",
                return_value=liftosaur,
            ),
            patch(
                "accounts.services.wger_services.fetch_latest_bodyweight",
                return_value=wger,
            ),
            patch(
                "accounts.services.hevy_services.fetch_latest_bodyweight",
                return_value=hevy,
            ),
        ):
            return sync_bodyweight_from_trackers(user)

    def test_newest_reading_wins_across_trackers_not_call_order(self, connected_user):
        # Liftosaur is asked first, so a "first answer wins" implementation
        # would store its year-old figure over yesterday's Hevy weigh-in.
        changed = self._run(
            connected_user,
            liftosaur=_reading("95", when=datetime(2025, 1, 1, tzinfo=UTC)),
            hevy=_reading("82.4", when=timezone.now() - timedelta(days=1)),
        )

        assert changed is True
        connected_user.refresh_from_db()
        assert connected_user.bodyweight_kg == Decimal("82.40")
        assert connected_user.bodyweight_source == User.BodyweightSource.TRACKER

    def test_only_connected_trackers_are_called(self, user):
        user.liftosaur_api_key = "lift-key"
        user.save(update_fields=["liftosaur_api_key"])

        with (
            patch(
                "accounts.services.liftosaur_services.fetch_latest_bodyweight",
                return_value=_reading("80", when=timezone.now()),
            ),
            patch(
                "accounts.services.wger_services.fetch_latest_bodyweight"
            ) as mock_wger,
            patch(
                "accounts.services.hevy_services.fetch_latest_bodyweight"
            ) as mock_hevy,
        ):
            assert sync_bodyweight_from_trackers(user) is True

        mock_wger.assert_not_called()
        mock_hevy.assert_not_called()

    def test_stale_tracker_reading_leaves_a_newer_stored_value_alone(
        self, connected_user
    ):
        connected_user.set_bodyweight(Decimal("84"), User.BodyweightSource.MANUAL)

        changed = self._run(
            connected_user,
            liftosaur=_reading("95", when=timezone.now() - timedelta(days=30)),
        )

        assert changed is False
        connected_user.refresh_from_db()
        assert connected_user.bodyweight_kg == Decimal("84.00")

    def test_fresh_tracker_reading_replaces_an_older_hand_entered_value(
        self, connected_user
    ):
        connected_user.set_bodyweight(Decimal("84"), User.BodyweightSource.MANUAL)

        changed = self._run(
            connected_user,
            liftosaur=_reading("82.4", when=timezone.now() + timedelta(minutes=1)),
        )

        assert changed is True
        connected_user.refresh_from_db()
        assert connected_user.bodyweight_kg == Decimal("82.40")

    def test_no_tracker_offers_anything(self, connected_user):
        assert self._run(connected_user) is False
        connected_user.refresh_from_db()
        assert connected_user.bodyweight_kg is None


class TestAccountDeletion:
    def test_bodyweight_is_erased_with_the_rest_of_the_identity(self, user):
        # The privacy policy commits to this: the figure is health-adjacent
        # personal data, not a record of anything that happened in a
        # challenge, so it goes the way the avatar does.
        user.set_bodyweight(Decimal("80"), User.BodyweightSource.MANUAL)

        anonymize_account(user)

        user.refresh_from_db()
        assert user.bodyweight_kg is None
        assert user.bodyweight_source == ""
        assert user.bodyweight_updated_at is None
