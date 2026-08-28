"""Tests for wger.services (TASK-311)."""

import logging
from datetime import timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

import httpx
import pytest
from django.utils import timezone
from wger_api_client.models.repetition_unit import RepetitionUnit
from wger_api_client.models.workout_log import WorkoutLog
from wger_api_client.types import UNSET

from accounts.tests.factories import UserFactory
from liftosaur.models import LiftHistory, LiftSource
from wger.client import WgerAPIError
from wger.services import (
    MAX_LOG_PAGES_PER_INLINE_RUN,
    canonical_wger_lift_name,
    history_watermark,
    last_synced_at,
    sync_wger_lifts,
    validate_wger_credentials,
)
from wger.tests.factories import WgerLiftAliasFactory, WgerSyncLogFactory

# Standard Wger fixture-shaped unit maps, matching a default install.
STANDARD_WEIGHT_UNITS = {1: "kg", 2: "lb"}
STANDARD_REPETITION_UNITS = {
    1: RepetitionUnit(id=1, name="Repetitions", unit_type="REPETITIONS")
}

# A self-hosted instance where the reference tables were renumbered -- proves
# resolution happens by name/unit_type, not by assumed id.
RENUMBERED_WEIGHT_UNITS = {5: "kg", 6: "lb"}
RENUMBERED_REPETITION_UNITS = {
    9: RepetitionUnit(id=9, name="Repetitions", unit_type="REPETITIONS")
}


def _log_entry(
    *,
    exercise=42,
    date="2026-01-01",
    weight="100",
    weight_unit=1,
    repetitions="5",
    repetitions_unit=1,
):
    from datetime import datetime as dt

    return WorkoutLog(
        exercise=exercise,
        date=dt.fromisoformat(date) if date is not None else UNSET,
        weight=weight,
        weight_unit=weight_unit,
        repetitions=repetitions,
        repetitions_unit=repetitions_unit,
    )


def _patch_units(weight_units, repetition_units):
    return (
        patch("wger.services.WgerClient.get_weight_units", return_value=weight_units),
        patch(
            "wger.services.WgerClient.get_repetition_units",
            return_value=repetition_units,
        ),
    )


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
        with patch(
            "wger.services.WgerClient.get_workout_logs",
            side_effect=httpx.ConnectError("unreachable"),
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
        entries = [_log_entry()]
        p1, p2 = _patch_units(STANDARD_WEIGHT_UNITS, STANDARD_REPETITION_UNITS)
        with (
            p1,
            p2,
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

    def test_utc_served_timestamp_pooled_under_the_lifters_own_day(self):
        """A Wger instance left on TIME_ZONE="UTC" serves a 22:00 Toronto
        session as 02:00Z the next day; performed_at must still be the day
        the lifter trained, since every downstream reader treats it as a
        civil date."""
        user = self._user(timezone="America/Toronto")
        entries = [_log_entry(date="2026-01-02T02:00:00+00:00")]
        p1, p2 = _patch_units(STANDARD_WEIGHT_UNITS, STANDARD_REPETITION_UNITS)
        with (
            p1,
            p2,
            patch(
                "wger.services.WgerClient.get_workout_logs",
                return_value=(entries, False, 100),
            ),
            patch("wger.services.WgerClient.get_exercise_name", return_value="Squat"),
        ):
            sync_wger_lifts(user, force=True)

        row = LiftHistory.objects.get(user=user)
        assert row.performed_at.isoformat() == "2026-01-01"

    def test_decimal_formatted_reps_from_real_api_are_parsed(self):
        """Wger's real API returns repetitions as a decimal-formatted string
        (e.g. "7.00"), not a bare integer string. int("7.00") raises
        ValueError, which the code used to swallow and silently drop the row
        -- this is the exact shape that caused every synced set to vanish
        against a real instance despite the sync reporting success.
        """
        user = self._user()
        entries = [_log_entry(repetitions="7.00")]
        p1, p2 = _patch_units(STANDARD_WEIGHT_UNITS, STANDARD_REPETITION_UNITS)
        with (
            p1,
            p2,
            patch(
                "wger.services.WgerClient.get_workout_logs",
                return_value=(entries, False, 100),
            ),
            patch("wger.services.WgerClient.get_exercise_name", return_value="Squat"),
        ):
            pooled = sync_wger_lifts(user, force=True)

        assert pooled == 1
        assert LiftHistory.objects.get(user=user).reps == 7

    def test_fractional_reps_skipped(self):
        user = self._user()
        entries = [_log_entry(repetitions="7.50")]
        p1, p2 = _patch_units(STANDARD_WEIGHT_UNITS, STANDARD_REPETITION_UNITS)
        with (
            p1,
            p2,
            patch(
                "wger.services.WgerClient.get_workout_logs",
                return_value=(entries, False, 100),
            ),
            patch("wger.services.WgerClient.get_exercise_name", return_value="Squat"),
        ):
            pooled = sync_wger_lifts(user, force=True)

        assert pooled == 0
        assert not LiftHistory.objects.filter(user=user).exists()

    def test_alias_applied_to_resolved_exercise_name(self):
        user = self._user()
        WgerLiftAliasFactory(from_name="Squat", to_name="Back Squat")
        entries = [_log_entry()]
        p1, p2 = _patch_units(STANDARD_WEIGHT_UNITS, STANDARD_REPETITION_UNITS)
        with (
            p1,
            p2,
            patch(
                "wger.services.WgerClient.get_workout_logs",
                return_value=(entries, False, 100),
            ),
            patch("wger.services.WgerClient.get_exercise_name", return_value="Squat"),
        ):
            sync_wger_lifts(user, force=True)

        assert LiftHistory.objects.get(user=user).lift == "Back Squat"

    def test_barbell_qualifier_is_stripped_without_an_explicit_alias(self):
        # The live-sync pull now runs the full six-stage resolution chain
        # (core.lift_resolution), not just a bare alias-map lookup -- this
        # fallback stage used to only apply to CSV imports.
        user = self._user()
        entries = [_log_entry()]
        p1, p2 = _patch_units(STANDARD_WEIGHT_UNITS, STANDARD_REPETITION_UNITS)
        with (
            p1,
            p2,
            patch(
                "wger.services.WgerClient.get_workout_logs",
                return_value=(entries, False, 100),
            ),
            patch(
                "wger.services.WgerClient.get_exercise_name",
                return_value="Pendlay Row (Barbell)",
            ),
        ):
            sync_wger_lifts(user, force=True)

        assert LiftHistory.objects.get(user=user).lift == "Pendlay Row"

    def test_fallback_stage_hit_logs_a_warning_naming_wger_sync(self, caplog):
        user = self._user()
        entries = [_log_entry()]
        p1, p2 = _patch_units(STANDARD_WEIGHT_UNITS, STANDARD_REPETITION_UNITS)
        with (
            p1,
            p2,
            patch(
                "wger.services.WgerClient.get_workout_logs",
                return_value=(entries, False, 100),
            ),
            patch(
                "wger.services.WgerClient.get_exercise_name",
                return_value="TBar Row",
            ),
            caplog.at_level(logging.WARNING, logger="wger.services"),
        ):
            sync_wger_lifts(user, force=True)

        assert LiftHistory.objects.get(user=user).lift == "T Bar Row"
        fuzzy_warnings = [
            r for r in caplog.records if "separator-insensitive fallback" in r.message
        ]
        assert len(fuzzy_warnings) == 1
        assert "Wger sync" in fuzzy_warnings[0].message

    def test_lb_weight_converted_to_kg(self):
        user = self._user()
        entries = [_log_entry(weight_unit=2)]
        p1, p2 = _patch_units(STANDARD_WEIGHT_UNITS, STANDARD_REPETITION_UNITS)
        with (
            p1,
            p2,
            patch(
                "wger.services.WgerClient.get_workout_logs",
                return_value=(entries, False, 100),
            ),
            patch("wger.services.WgerClient.get_exercise_name", return_value="Squat"),
        ):
            sync_wger_lifts(user, force=True)

        row = LiftHistory.objects.get(user=user)
        assert row.weight_kg == Decimal("45.36")

    def test_renumbered_instance_units_resolved_by_name_and_type(self):
        """A self-hosted instance whose weightunit/repetitionunit ids don't
        match the standard 1=kg/2=lb/1=Repetitions fixtures must still
        convert correctly -- resolution is by name/unit_type, not id.

        This would fail against the old hardcoded-ID implementation, which
        would treat weight_unit=6 as an unrecognized unit (falling back to
        "already kg", i.e. no conversion) and skip repetitions_unit=9
        entirely as "not plain reps".
        """
        user = self._user()
        entries = [_log_entry(weight_unit=6, repetitions_unit=9)]
        p1, p2 = _patch_units(RENUMBERED_WEIGHT_UNITS, RENUMBERED_REPETITION_UNITS)
        with (
            p1,
            p2,
            patch(
                "wger.services.WgerClient.get_workout_logs",
                return_value=(entries, False, 100),
            ),
            patch("wger.services.WgerClient.get_exercise_name", return_value="Squat"),
        ):
            pooled = sync_wger_lifts(user, force=True)

        assert pooled == 1
        row = LiftHistory.objects.get(user=user)
        assert row.weight_kg == Decimal("45.36")  # lb -> kg, resolved by name
        assert row.reps == 5

    def test_non_repetitions_unit_skipped(self):
        user = self._user()
        entries = [_log_entry(repetitions="1", repetitions_unit=2)]
        weight_units = STANDARD_WEIGHT_UNITS
        repetition_units = {
            1: RepetitionUnit(id=1, name="Repetitions", unit_type="REPETITIONS"),
            2: RepetitionUnit(id=2, name="Until Failure", unit_type="TIME"),
        }
        p1, p2 = _patch_units(weight_units, repetition_units)
        with (
            p1,
            p2,
            patch(
                "wger.services.WgerClient.get_workout_logs",
                return_value=(entries, False, 100),
            ),
            patch("wger.services.WgerClient.get_exercise_name", return_value="Squat"),
        ):
            pooled = sync_wger_lifts(user, force=True)

        assert pooled == 0
        assert not LiftHistory.objects.filter(user=user).exists()

    def test_no_repetitions_unit_on_entry_defaults_to_plain_reps(self):
        user = self._user()
        entries = [_log_entry(repetitions_unit=None)]
        p1, p2 = _patch_units(STANDARD_WEIGHT_UNITS, STANDARD_REPETITION_UNITS)
        with (
            p1,
            p2,
            patch(
                "wger.services.WgerClient.get_workout_logs",
                return_value=(entries, False, 100),
            ),
            patch("wger.services.WgerClient.get_exercise_name", return_value="Squat"),
        ):
            pooled = sync_wger_lifts(user, force=True)

        assert pooled == 1

    def test_unresolvable_exercise_name_skipped(self):
        user = self._user()
        entries = [_log_entry()]
        p1, p2 = _patch_units(STANDARD_WEIGHT_UNITS, STANDARD_REPETITION_UNITS)
        with (
            p1,
            p2,
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
        p1, p2 = _patch_units(STANDARD_WEIGHT_UNITS, STANDARD_REPETITION_UNITS)
        with (
            p1,
            p2,
            patch(
                "wger.services.WgerClient.get_workout_logs",
                side_effect=WgerAPIError(401, "Unauthorized"),
            ),
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
        mock_client.get_weight_units.return_value = STANDARD_WEIGHT_UNITS
        mock_client.get_repetition_units.return_value = STANDARD_REPETITION_UNITS
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
        mock_client.get_weight_units.return_value = STANDARD_WEIGHT_UNITS
        mock_client.get_repetition_units.return_value = STANDARD_REPETITION_UNITS
        with patch("wger.services.WgerClient", return_value=mock_client):
            sync_wger_lifts(user, force=True, full_backfill=True)

        recent_date = (timezone.now() - timedelta(days=10)).date().isoformat()
        call_kwargs = mock_client.get_workout_logs.call_args.kwargs
        assert call_kwargs["date_gte"] != recent_date

    def test_second_page_paginates(self):
        user = self._user()
        page1 = [_log_entry(date="2026-01-01", weight="100")]
        page2 = [_log_entry(date="2026-01-02", weight="105")]
        p1, p2 = _patch_units(STANDARD_WEIGHT_UNITS, STANDARD_REPETITION_UNITS)
        with (
            p1,
            p2,
            patch(
                "wger.services.WgerClient.get_workout_logs",
                side_effect=[(page1, True, 100), (page2, False, 200)],
            ),
            patch("wger.services.WgerClient.get_exercise_name", return_value="Squat"),
        ):
            pooled = sync_wger_lifts(user, force=True)

        assert pooled == 2
        assert LiftHistory.objects.filter(user=user).count() == 2

    @pytest.mark.parametrize(
        "exc",
        [
            httpx.ConnectError("connection refused"),
            httpx.ReadTimeout("timed out"),
            TimeoutError("bare read timeout"),
        ],
        ids=["connect_refused", "read_timeout", "bare_timeout_error"],
    )
    def test_unreachable_instance_degrades_instead_of_raising(self, exc):
        # Wger is self-hostable, so an offline box or stale DNS record is
        # routine. httpx.HTTPError is not an OSError subclass, so this escaped
        # sync_wger_lifts entirely before -- and sync_and_score runs on the
        # request path inside a loop over every challenge participant, so one
        # member's dead instance 500'd the shared detail page.
        user = self._user()
        p1, p2 = _patch_units(STANDARD_WEIGHT_UNITS, STANDARD_REPETITION_UNITS)
        with (
            p1,
            p2,
            patch("wger.services.WgerClient.get_workout_logs", side_effect=exc),
        ):
            pooled = sync_wger_lifts(user, force=True)

        assert pooled == 0
        log = user.wger_sync_logs.get()
        assert log.success is False
        assert "Network error" in log.error_detail

    def test_inline_walk_stops_at_the_page_cap_and_hands_off_to_background(self):
        # An uncapped walk over a first-time 365-day backfill can hold a worker
        # for minutes; the detail view's sync budget is only checked between
        # participants, so it cannot interrupt one long pull.
        user = self._user()
        endless_pages = [
            ([_log_entry(date=f"2026-01-{day:02d}")], True, day * 100)
            for day in range(1, MAX_LOG_PAGES_PER_INLINE_RUN + 5)
        ]
        p1, p2 = _patch_units(STANDARD_WEIGHT_UNITS, STANDARD_REPETITION_UNITS)
        with (
            p1,
            p2,
            patch(
                "wger.services.WgerClient.get_workout_logs",
                side_effect=endless_pages,
            ) as mock_get,
            patch("wger.services.WgerClient.get_exercise_name", return_value="Squat"),
            patch("wger.services.trigger_wger_lift_history_catchup") as mock_catchup,
        ):
            pooled = sync_wger_lifts(user, force=True)

        assert mock_get.call_count == MAX_LOG_PAGES_PER_INLINE_RUN
        assert pooled == MAX_LOG_PAGES_PER_INLINE_RUN
        # The pages it didn't reach still have to land, off the request path.
        mock_catchup.assert_called_once_with(user)

    def test_background_run_walks_past_the_inline_cap(self):
        user = self._user()
        pages = [
            ([_log_entry(date=f"2026-01-{day:02d}")], True, day * 100)
            for day in range(1, MAX_LOG_PAGES_PER_INLINE_RUN + 2)
        ]
        pages.append(([_log_entry(date="2026-02-01")], False, 9999))
        p1, p2 = _patch_units(STANDARD_WEIGHT_UNITS, STANDARD_REPETITION_UNITS)
        with (
            p1,
            p2,
            patch("wger.services.WgerClient.get_workout_logs", side_effect=pages),
            patch("wger.services.WgerClient.get_exercise_name", return_value="Squat"),
            patch("wger.services.trigger_wger_lift_history_catchup") as mock_catchup,
        ):
            pooled = sync_wger_lifts(user, force=True, max_pages=500)

        assert pooled == len(pages)
        # A walk that reached the end of the feed needs no catch-up pass.
        mock_catchup.assert_not_called()
