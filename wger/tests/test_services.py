"""Tests for wger.services (TASK-311)."""

from datetime import timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from django.utils import timezone

from accounts.tests.factories import UserFactory
from liftosaur.models import LiftHistory, LiftSource
from wger.client import WgerAPIError
from wger.services import (
    canonical_wger_lift_name,
    history_watermark,
    last_synced_at,
    sync_wger_lifts,
    validate_wger_credentials,
)
from wger.tests.factories import WgerLiftAliasFactory, WgerSyncLogFactory


@pytest.mark.django_db
class TestValidateWgerCredentials:
    def test_valid_credentials_return_true(self):
        with patch(
            "wger.services.WgerClient.get_workout_logs",
            return_value=([], False, 1),
        ):
            assert validate_wger_credentials("https://example.com", "tok") is True

    def test_api_error_returns_false(self):
        with patch(
            "wger.services.WgerClient.get_workout_logs",
            side_effect=WgerAPIError(401, "Unauthorized"),
        ):
            assert validate_wger_credentials("https://example.com", "bad") is False

    def test_network_error_returns_false(self):
        import urllib.error

        with patch(
            "wger.services.WgerClient.get_workout_logs",
            side_effect=urllib.error.URLError("unreachable"),
        ):
            assert validate_wger_credentials("https://bad-host", "tok") is False


@pytest.mark.django_db
class TestCanonicalWgerLiftName:
    def test_aliased_name_resolved(self):
        WgerLiftAliasFactory(from_name="Barbell Squat", to_name="Back Squat")
        assert canonical_wger_lift_name("barbell squat") == "Back Squat"

    def test_unaliased_name_passes_through(self):
        assert canonical_wger_lift_name("Some New Exercise") == "Some New Exercise"


@pytest.mark.django_db
class TestSyncWgerLifts:
    def _user(self, **kwargs):
        return UserFactory(
            wger_instance_url="https://example.com", wger_api_token="tok", **kwargs
        )

    def test_no_credentials_is_noop(self):
        user = UserFactory(wger_instance_url=None, wger_api_token=None)
        assert sync_wger_lifts(user) == 0

    def test_successful_sync_writes_pooled_rows(self):
        user = self._user()
        entries = [
            {
                "exercise": 42,
                "date": "2026-01-01",
                "weight": "100",
                "weight_unit": 1,
                "repetitions": 5,
                "repetitions_unit": 1,
            }
        ]
        with (
            patch(
                "wger.services.WgerClient.get_workout_logs",
                return_value=(entries, False, 100),
            ),
            patch("wger.services.WgerClient.get_exercise_name", return_value="Squat"),
        ):
            pooled = sync_wger_lifts(user, force=True)

        assert pooled == 1
        row = LiftHistory.objects.get(user=user)
        assert row.lift == "Squat"
        assert row.reps == 5
        assert row.weight_kg == Decimal("100.00")
        assert row.source == LiftSource.WGER
        assert last_synced_at(user) is not None

    def test_alias_applied_to_resolved_exercise_name(self):
        user = self._user()
        WgerLiftAliasFactory(from_name="Squat", to_name="Back Squat")
        entries = [
            {
                "exercise": 42,
                "date": "2026-01-01",
                "weight": "100",
                "weight_unit": 1,
                "repetitions": 5,
                "repetitions_unit": 1,
            }
        ]
        with (
            patch(
                "wger.services.WgerClient.get_workout_logs",
                return_value=(entries, False, 100),
            ),
            patch("wger.services.WgerClient.get_exercise_name", return_value="Squat"),
        ):
            sync_wger_lifts(user, force=True)

        assert LiftHistory.objects.get(user=user).lift == "Back Squat"

    def test_lb_weight_converted_to_kg(self):
        user = self._user()
        entries = [
            {
                "exercise": 42,
                "date": "2026-01-01",
                "weight": "100",
                "weight_unit": 2,  # lb
                "repetitions": 5,
                "repetitions_unit": 1,
            }
        ]
        with (
            patch(
                "wger.services.WgerClient.get_workout_logs",
                return_value=(entries, False, 100),
            ),
            patch("wger.services.WgerClient.get_exercise_name", return_value="Squat"),
        ):
            sync_wger_lifts(user, force=True)

        row = LiftHistory.objects.get(user=user)
        assert row.weight_kg == Decimal("45.36")

    def test_non_repetitions_unit_skipped(self):
        user = self._user()
        entries = [
            {
                "exercise": 42,
                "date": "2026-01-01",
                "weight": "100",
                "weight_unit": 1,
                "repetitions": 1,
                "repetitions_unit": 2,  # "Until Failure" -- not plain reps
            }
        ]
        with (
            patch(
                "wger.services.WgerClient.get_workout_logs",
                return_value=(entries, False, 100),
            ),
            patch("wger.services.WgerClient.get_exercise_name", return_value="Squat"),
        ):
            pooled = sync_wger_lifts(user, force=True)

        assert pooled == 0
        assert not LiftHistory.objects.filter(user=user).exists()

    def test_unresolvable_exercise_name_skipped(self):
        user = self._user()
        entries = [
            {
                "exercise": 42,
                "date": "2026-01-01",
                "weight": "100",
                "weight_unit": 1,
                "repetitions": 5,
                "repetitions_unit": 1,
            }
        ]
        with (
            patch(
                "wger.services.WgerClient.get_workout_logs",
                return_value=(entries, False, 100),
            ),
            patch("wger.services.WgerClient.get_exercise_name", return_value=None),
        ):
            pooled = sync_wger_lifts(user, force=True)

        assert pooled == 0
        assert not LiftHistory.objects.filter(user=user).exists()

    def test_api_error_marks_sync_log_failed(self):
        user = self._user()
        with patch(
            "wger.services.WgerClient.get_workout_logs",
            side_effect=WgerAPIError(401, "Unauthorized"),
        ):
            pooled = sync_wger_lifts(user, force=True)

        assert pooled == 0
        log = user.wger_sync_logs.get()
        assert log.success is False
        assert "401" in log.error_detail

    def test_cooldown_skips_sync_without_force(self):
        user = self._user()
        WgerSyncLogFactory(user=user, success=True, started_at=timezone.now())
        with patch("wger.services.WgerClient.get_workout_logs") as mock_get:
            pooled = sync_wger_lifts(user)
        assert pooled == 0
        mock_get.assert_not_called()

    def test_incremental_sync_uses_watermark(self):
        user = self._user()
        LiftHistory.objects.create(
            user=user,
            lift="Back Squat",
            performed_at=(timezone.now() - timedelta(days=10)).date(),
            weight_kg=Decimal("100.00"),
            reps=5,
            source=LiftSource.WGER,
            synced_at=timezone.now(),
        )
        watermark = history_watermark(user)
        assert watermark is not None

        mock_client = MagicMock()
        mock_client.get_workout_logs.return_value = ([], False, 100)
        with patch("wger.services.WgerClient", return_value=mock_client):
            sync_wger_lifts(user, force=True)

        call_kwargs = mock_client.get_workout_logs.call_args.kwargs
        assert call_kwargs["date_gte"] == watermark.isoformat()

    def test_full_backfill_ignores_watermark(self):
        user = self._user()
        LiftHistory.objects.create(
            user=user,
            lift="Back Squat",
            performed_at=(timezone.now() - timedelta(days=10)).date(),
            weight_kg=Decimal("100.00"),
            reps=5,
            source=LiftSource.WGER,
            synced_at=timezone.now(),
        )
        mock_client = MagicMock()
        mock_client.get_workout_logs.return_value = ([], False, 100)
        with patch("wger.services.WgerClient", return_value=mock_client):
            sync_wger_lifts(user, force=True, full_backfill=True)

        recent_date = (timezone.now() - timedelta(days=10)).date().isoformat()
        call_kwargs = mock_client.get_workout_logs.call_args.kwargs
        assert call_kwargs["date_gte"] != recent_date

    def test_second_page_paginates(self):
        user = self._user()
        page1 = [
            {
                "exercise": 42,
                "date": "2026-01-01",
                "weight": "100",
                "weight_unit": 1,
                "repetitions": 5,
                "repetitions_unit": 1,
            }
        ]
        page2 = [
            {
                "exercise": 42,
                "date": "2026-01-02",
                "weight": "105",
                "weight_unit": 1,
                "repetitions": 5,
                "repetitions_unit": 1,
            }
        ]
        with (
            patch(
                "wger.services.WgerClient.get_workout_logs",
                side_effect=[(page1, True, 100), (page2, False, 200)],
            ),
            patch("wger.services.WgerClient.get_exercise_name", return_value="Squat"),
        ):
            pooled = sync_wger_lifts(user, force=True)

        assert pooled == 2
        assert LiftHistory.objects.filter(user=user).count() == 2
