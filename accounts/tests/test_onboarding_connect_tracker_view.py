"""Tests for the onboarding tracker-connect step (generalized over
Liftosaur/Wger/Hevy)."""

from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import OperationalError
from django.test import Client
from django.urls import reverse

from accounts.tests.factories import UserFactory
from liftosaur.models import LiftHistory

User = get_user_model()


@pytest.fixture
def client():
    return Client()


def _url(app):
    return reverse("accounts:onboarding-connect-tracker", args=[app])


_HEVY_CSV_HEADER = (
    "title,start_time,end_time,description,exercise_title,superset_id,"
    "exercise_notes,set_index,set_type,weight_lbs,reps,distance_km,"
    "duration_seconds,rpe\n"
)


def _hevy_csv_upload(name="export.csv", rows=None):
    if rows is None:
        rows = 'Leg day,"01 Jan 2024, 09:15",,,Squat (Barbell),,,1,normal,225,5,,,\n'
    return SimpleUploadedFile(
        name, (_HEVY_CSV_HEADER + rows).encode("utf-8"), content_type="text/csv"
    )


@pytest.mark.django_db
class TestOnboardingConnectTrackerViewRouting:
    def test_anonymous_get_redirects_to_login(self, client):
        response = client.get(_url("liftosaur"))
        assert response.status_code == 302
        assert response["Location"].startswith(reverse("accounts:login"))

    def test_unknown_app_redirects_to_tracking_method_step(self, client):
        client.force_login(UserFactory())
        response = client.get(_url("not-a-real-app"))
        assert response.status_code == 302
        assert response["Location"] == reverse("accounts:onboarding-tracking-method")


@pytest.mark.django_db
class TestOnboardingConnectTrackerViewLiftosaur:
    @patch("accounts.views.trigger_lift_history_backfill")
    @patch("accounts.views.validate_liftosaur_key", return_value=True)
    def test_valid_key_saves_triggers_backfill_and_redirects(
        self, mock_validate, mock_backfill, client
    ):
        user = UserFactory()
        client.force_login(user)

        response = client.post(_url("liftosaur"), {"liftosaur_api_key": "valid-key"})

        assert response.status_code == 302
        assert response["Location"] == reverse("accounts:onboarding-units")
        mock_validate.assert_called_once_with("valid-key")
        user.refresh_from_db()
        assert user.liftosaur_api_key == "valid-key"
        mock_backfill.assert_called_once_with(user)

    @patch("accounts.views.trigger_lift_history_backfill")
    @patch("accounts.views.validate_liftosaur_key")
    def test_blank_key_skips_validation_and_backfill_but_still_redirects(
        self, mock_validate, mock_backfill, client
    ):
        user = UserFactory()
        client.force_login(user)

        response = client.post(_url("liftosaur"), {"liftosaur_api_key": ""})

        assert response.status_code == 302
        assert response["Location"] == reverse("accounts:onboarding-units")
        mock_validate.assert_not_called()
        mock_backfill.assert_not_called()
        user.refresh_from_db()
        assert user.liftosaur_api_key is None

    @patch("accounts.views.trigger_lift_history_backfill")
    @patch("accounts.views.validate_liftosaur_key", return_value=False)
    def test_invalid_key_rerenders_with_error_and_does_not_touch_account(
        self, mock_validate, mock_backfill, client
    ):
        user = UserFactory()
        client.force_login(user)

        response = client.post(_url("liftosaur"), {"liftosaur_api_key": "bad-key"})

        assert response.status_code == 200
        assert b"Could not validate this Liftosaur API key." in response.content
        user.refresh_from_db()
        assert user.liftosaur_api_key is None
        mock_backfill.assert_not_called()

    @patch("accounts.views.trigger_lift_history_backfill")
    @patch("accounts.views.validate_liftosaur_key", return_value=True)
    def test_resubmitting_an_unchanged_key_does_not_retrigger_backfill(
        self, mock_validate, mock_backfill, client
    ):
        """had_key_before: revisiting/resubmitting this step must not re-seed
        LiftHistory that's already been pulled."""
        user = UserFactory(liftosaur_api_key="already-connected-key")
        client.force_login(user)

        response = client.post(
            _url("liftosaur"), {"liftosaur_api_key": "already-connected-key"}
        )

        assert response.status_code == 302
        mock_backfill.assert_not_called()


@pytest.mark.django_db
class TestOnboardingConnectTrackerViewWger:
    @patch("accounts.views.trigger_wger_lift_history_backfill")
    @patch("accounts.views.validate_wger_credentials", return_value=True)
    def test_valid_credentials_save_trigger_backfill_and_redirect(
        self, mock_validate, mock_backfill, client
    ):
        user = UserFactory()
        client.force_login(user)

        response = client.post(
            _url("wger"),
            {
                "wger_instance_url": "https://example.com",
                "wger_api_token": "tok",
            },
        )

        assert response.status_code == 302
        assert response["Location"] == reverse("accounts:onboarding-units")
        mock_validate.assert_called_once_with("https://example.com", "tok")
        user.refresh_from_db()
        assert user.wger_instance_url == "https://example.com"
        assert user.wger_api_token == "tok"
        mock_backfill.assert_called_once_with(user)

    @patch("accounts.views.trigger_wger_lift_history_backfill")
    @patch("accounts.views.validate_wger_credentials")
    def test_blank_credentials_skip_validation_and_backfill_but_still_redirect(
        self, mock_validate, mock_backfill, client
    ):
        user = UserFactory()
        client.force_login(user)

        response = client.post(
            _url("wger"), {"wger_instance_url": "", "wger_api_token": ""}
        )

        assert response.status_code == 302
        assert response["Location"] == reverse("accounts:onboarding-units")
        mock_validate.assert_not_called()
        mock_backfill.assert_not_called()
        user.refresh_from_db()
        assert user.wger_instance_url is None
        assert user.wger_api_token is None

    @patch("accounts.views.trigger_wger_lift_history_backfill")
    @patch("accounts.views.validate_wger_credentials", return_value=False)
    def test_invalid_credentials_rerender_with_error_and_do_not_touch_account(
        self, mock_validate, mock_backfill, client
    ):
        user = UserFactory()
        client.force_login(user)

        response = client.post(
            _url("wger"),
            {"wger_instance_url": "https://example.com", "wger_api_token": "bad"},
        )

        assert response.status_code == 200
        assert (
            b"Could not validate this Wger instance URL or API token."
            in response.content
        )
        user.refresh_from_db()
        assert user.wger_instance_url is None
        assert user.wger_api_token is None
        mock_backfill.assert_not_called()

    @patch("accounts.views.trigger_wger_lift_history_backfill")
    @patch("accounts.views.validate_wger_credentials", return_value=False)
    def test_only_one_field_filled_is_treated_as_incomplete_and_errors(
        self, mock_validate, mock_backfill, client
    ):
        user = UserFactory()
        client.force_login(user)

        response = client.post(
            _url("wger"),
            {"wger_instance_url": "https://example.com", "wger_api_token": ""},
        )

        assert response.status_code == 200
        mock_validate.assert_called_once_with("https://example.com", "")
        user.refresh_from_db()
        assert user.wger_instance_url is None
        mock_backfill.assert_not_called()

    @patch("accounts.views.trigger_wger_lift_history_backfill")
    @patch("accounts.views.validate_wger_credentials", return_value=True)
    def test_resubmitting_unchanged_credentials_does_not_retrigger_backfill(
        self, mock_validate, mock_backfill, client
    ):
        user = UserFactory(
            wger_instance_url="https://example.com", wger_api_token="already-connected"
        )
        client.force_login(user)

        response = client.post(
            _url("wger"),
            {
                "wger_instance_url": "https://example.com",
                "wger_api_token": "already-connected",
            },
        )

        assert response.status_code == 302
        mock_backfill.assert_not_called()


@pytest.mark.django_db
class TestOnboardingConnectTrackerViewLiftosaurCsv:
    """Liftosaur's connect page offers a CSV upload alongside the API key --
    the two are independently optional and processed independently."""

    def test_valid_csv_pools_sets_and_redirects(self, client):
        user = UserFactory()
        client.force_login(user)

        response = client.post(_url("liftosaur"), {"csv_file": _hevy_csv_upload()})

        assert response.status_code == 302
        assert response["Location"] == reverse("accounts:onboarding-units")
        assert LiftHistory.objects.filter(user=user).exists()

    def test_unrecognized_csv_format_rerenders_with_error(self, client):
        user = UserFactory()
        client.force_login(user)
        bogus = SimpleUploadedFile(
            "export.csv", b"not,the,right,columns\na,b,c\n", content_type="text/csv"
        )

        response = client.post(_url("liftosaur"), {"csv_file": bogus})

        assert response.status_code == 200
        assert not LiftHistory.objects.filter(user=user).exists()

    def test_db_contention_reports_back_instead_of_500(self, client):
        user = UserFactory()
        client.force_login(user)

        with patch(
            "accounts.views.import_workout_csv",
            side_effect=OperationalError("database is locked"),
        ):
            response = client.post(_url("liftosaur"), {"csv_file": _hevy_csv_upload()})

        assert response.status_code == 200

    @patch("accounts.views.trigger_lift_history_backfill")
    @patch("accounts.views.validate_liftosaur_key", return_value=True)
    def test_key_and_csv_both_processed_independently(
        self, mock_validate, mock_backfill, client
    ):
        user = UserFactory()
        client.force_login(user)

        response = client.post(
            _url("liftosaur"),
            {"liftosaur_api_key": "valid-key", "csv_file": _hevy_csv_upload()},
        )

        assert response.status_code == 302
        user.refresh_from_db()
        assert user.liftosaur_api_key == "valid-key"
        assert LiftHistory.objects.filter(user=user).exists()

    @patch("accounts.views.validate_liftosaur_key", return_value=False)
    def test_bad_key_error_does_not_block_a_valid_csv_from_pooling(
        self, mock_validate, client
    ):
        """Independent fields: a bad key still errors and blocks the
        redirect, but the CSV side effect (already applied) isn't undone."""
        user = UserFactory()
        client.force_login(user)

        response = client.post(
            _url("liftosaur"),
            {"liftosaur_api_key": "bad-key", "csv_file": _hevy_csv_upload()},
        )

        assert response.status_code == 200
        user.refresh_from_db()
        assert user.liftosaur_api_key is None
        assert LiftHistory.objects.filter(user=user).exists()


@pytest.mark.django_db
class TestOnboardingConnectTrackerViewHevy:
    """Hevy has no live-sync integration merged yet, so its connect page is
    CSV-only -- no credential fields at all."""

    def test_valid_csv_pools_sets_and_redirects(self, client):
        user = UserFactory()
        client.force_login(user)

        response = client.post(_url("hevy"), {"csv_file": _hevy_csv_upload()})

        assert response.status_code == 302
        assert response["Location"] == reverse("accounts:onboarding-units")
        assert LiftHistory.objects.filter(user=user).exists()

    def test_blank_submission_skips_and_still_redirects(self, client):
        user = UserFactory()
        client.force_login(user)

        response = client.post(_url("hevy"), {})

        assert response.status_code == 302
        assert response["Location"] == reverse("accounts:onboarding-units")
        assert not LiftHistory.objects.filter(user=user).exists()

    def test_invalid_file_shows_friendly_error(self, client):
        user = UserFactory()
        client.force_login(user)
        bogus = SimpleUploadedFile(
            "notes.txt", b"not a csv at all", content_type="text/plain"
        )

        response = client.post(_url("hevy"), {"csv_file": bogus})

        assert response.status_code == 200
        assert "Please upload a .csv file." in response.content.decode()
