"""Tests for hevy_api.services (TASK-312)."""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from django.db import OperationalError

from accounts.tests.factories import UserFactory
from hevy_api.client import HevyAPIError
from hevy_api.models import HevySyncLog
from hevy_api.services import (
    HISTORY_BACKFILL_DAYS,
    MAX_EVENT_PAGES_PER_RUN,
    _parse_workout,
    last_synced_at,
    sync_user_lifts,
    validate_hevy_key,
)
from liftosaur.models import LiftHistory, LiftSource
from workout_imports.tests.factories import HevyLiftAliasFactory


def _events_page(events, *, page=1, page_count=1):
    return {"page": page, "page_count": page_count, "events": events}


def _updated_event(workout):
    return {"type": "updated", "workout": workout}


def _workout(
    start_time="2024-06-01T12:00:00Z",
    exercises=None,
):
    return {
        "id": "w1",
        "start_time": start_time,
        "exercises": exercises if exercises is not None else [],
    }


def _exercise(title, sets):
    return {"title": title, "sets": sets}


def _set(type_="normal", weight_kg=100, reps=5):
    return {"type": type_, "weight_kg": weight_kg, "reps": reps}


def _stub_client(events_pages=None):
    """Return a MagicMock HevyClient whose get_workout_events yields the given pages."""
    client = MagicMock()
    pages = events_pages if events_pages is not None else [_events_page([])]
    client.get_workout_events.side_effect = pages
    client.get_workouts.return_value = {"page": 1, "page_count": 1, "workouts": []}
    return client


# ---------------------------------------------------------------------------
# validate_hevy_key
# ---------------------------------------------------------------------------


class TestValidateHevyKey:
    def test_valid_key_returns_true(self):
        client = MagicMock()
        with patch("hevy_api.services.HevyClient", return_value=client):
            assert validate_hevy_key("key") is True
        client.get_workouts.assert_called_once_with(page=1, page_size=1)

    def test_api_error_returns_false(self):
        client = MagicMock()
        client.get_workouts.side_effect = HevyAPIError(403, "Pro required")
        with patch("hevy_api.services.HevyClient", return_value=client):
            assert validate_hevy_key("key") is False

    def test_url_error_returns_false(self):
        import urllib.error

        client = MagicMock()
        client.get_workouts.side_effect = urllib.error.URLError("no network")
        with patch("hevy_api.services.HevyClient", return_value=client):
            assert validate_hevy_key("key") is False

    def test_generic_exception_returns_false(self):
        client = MagicMock()
        client.get_workouts.side_effect = ValueError("boom")
        with patch("hevy_api.services.HevyClient", return_value=client):
            assert validate_hevy_key("key") is False


# ---------------------------------------------------------------------------
# _parse_workout
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestParseWorkout:
    def test_normal_set_parsed(self):
        workout = _workout(
            exercises=[_exercise("Back Squat", [_set(weight_kg=100, reps=5)])]
        )
        parsed = _parse_workout(workout, {})
        assert len(parsed) == 1
        assert parsed[0].lift == "Back Squat"
        assert parsed[0].reps == 5
        assert parsed[0].weight_kg == Decimal("100.00")
        assert parsed[0].performed_at.isoformat() == "2024-06-01"

    def test_warmup_set_excluded(self):
        workout = _workout(
            exercises=[
                _exercise(
                    "Back Squat",
                    [_set(type_="warmup", weight_kg=60, reps=8), _set()],
                )
            ]
        )
        parsed = _parse_workout(workout, {})
        assert len(parsed) == 1
        assert parsed[0].weight_kg == Decimal("100.00")

    def test_set_with_no_reps_skipped(self):
        # Cardio-style set: distance/duration tracked instead of reps.
        workout = _workout(
            exercises=[
                _exercise(
                    "Rowing", [{"type": "normal", "weight_kg": None, "reps": None}]
                )
            ]
        )
        assert _parse_workout(workout, {}) == []

    def test_exercise_title_resolved_through_alias_map(self):
        workout = _workout(exercises=[_exercise("Squat (Barbell)", [_set()])])
        parsed = _parse_workout(workout, {"squat (barbell)": "Back Squat"})
        assert parsed[0].lift == "Back Squat"

    def test_unaliased_title_passes_through_unchanged(self):
        workout = _workout(exercises=[_exercise("Some New Exercise", [_set()])])
        parsed = _parse_workout(workout, {})
        assert parsed[0].lift == "Some New Exercise"

    def test_unparseable_start_time_yields_no_sets(self):
        workout = _workout(
            start_time="not-a-date", exercises=[_exercise("Squat", [_set()])]
        )
        assert _parse_workout(workout, {}) == []

    def test_missing_start_time_yields_no_sets(self):
        workout = _workout(start_time="", exercises=[_exercise("Squat", [_set()])])
        assert _parse_workout(workout, {}) == []

    def test_exercise_with_no_title_skipped(self):
        workout = _workout(exercises=[_exercise("", [_set()])])
        assert _parse_workout(workout, {}) == []

    def test_non_numeric_weight_skipped(self):
        workout = _workout(
            exercises=[
                _exercise(
                    "Back Squat",
                    [{"type": "normal", "weight_kg": "not-a-number", "reps": 5}],
                )
            ]
        )
        assert _parse_workout(workout, {}) == []

    def test_multiple_sets_of_same_type_all_pooled(self):
        workout = _workout(
            exercises=[
                _exercise(
                    "Back Squat",
                    [_set(reps=5), _set(reps=3), _set(reps=1)],
                )
            ]
        )
        parsed = _parse_workout(workout, {})
        assert [p.reps for p in parsed] == [5, 3, 1]


# ---------------------------------------------------------------------------
# sync_user_lifts
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestSyncUserLifts:
    def test_no_api_key_is_noop(self):
        user = UserFactory(hevy_api_key=None)
        with patch("hevy_api.services.HevyClient") as mock_client:
            pooled = sync_user_lifts(user)
        assert pooled == 0
        mock_client.assert_not_called()
        assert not HevySyncLog.objects.filter(user=user).exists()

    def test_seeds_pool_and_records_success_log(self):
        user = UserFactory(hevy_api_key="key")
        workout = _workout(exercises=[_exercise("Back Squat", [_set()])])
        client = _stub_client([_events_page([_updated_event(workout)])])

        with patch("hevy_api.services.HevyClient", return_value=client):
            pooled = sync_user_lifts(user)

        assert pooled == 1
        assert (
            LiftHistory.objects.filter(
                user=user, lift="Back Squat", source=LiftSource.HEVY_API
            ).count()
            == 1
        )
        log = HevySyncLog.objects.get(user=user)
        assert log.success is True
        assert json.loads(log.result_summary) == {"sets_pooled": 1}

    def test_first_sync_uses_backfill_window(self):
        user = UserFactory(hevy_api_key="key")
        client = _stub_client()

        with patch("hevy_api.services.HevyClient", return_value=client):
            sync_user_lifts(user)

        _, kwargs = client.get_workout_events.call_args
        expected_start = (
            datetime.now(tz=UTC) - timedelta(days=HISTORY_BACKFILL_DAYS)
        ).date().isoformat() + "T00:00:00Z"
        assert kwargs["since"] == expected_start

    def test_rerun_uses_shared_pool_watermark(self):
        user = UserFactory(hevy_api_key="key")
        watermark_date = (datetime.now(tz=UTC) - timedelta(days=10)).date()
        LiftHistory.objects.create(
            user=user,
            lift="Back Squat",
            performed_at=watermark_date,
            weight_kg=Decimal("100"),
            reps=5,
        )
        client = _stub_client()

        with patch("hevy_api.services.HevyClient", return_value=client):
            sync_user_lifts(user, force=True)

        _, kwargs = client.get_workout_events.call_args
        assert kwargs["since"] == f"{watermark_date.isoformat()}T00:00:00Z"

    def test_deleted_event_does_not_remove_pooled_rows(self):
        user = UserFactory(hevy_api_key="key")
        workout = _workout(exercises=[_exercise("Back Squat", [_set()])])
        client = _stub_client(
            [
                _events_page(
                    [
                        _updated_event(workout),
                        {"type": "deleted", "id": "w1", "deleted_at": "now"},
                    ]
                )
            ]
        )

        with patch("hevy_api.services.HevyClient", return_value=client):
            pooled = sync_user_lifts(user)

        assert pooled == 1
        assert LiftHistory.objects.filter(user=user).count() == 1

    def test_walks_multiple_pages(self):
        user = UserFactory(hevy_api_key="key")
        w1 = _workout(exercises=[_exercise("Back Squat", [_set(reps=5)])])
        w2 = _workout(exercises=[_exercise("Bench Press", [_set(reps=3)])])
        client = _stub_client(
            [
                _events_page([_updated_event(w1)], page=1, page_count=2),
                _events_page([_updated_event(w2)], page=2, page_count=2),
            ]
        )

        with patch("hevy_api.services.HevyClient", return_value=client):
            pooled = sync_user_lifts(user)

        assert pooled == 2
        assert client.get_workout_events.call_count == 2
        assert LiftHistory.objects.filter(user=user).count() == 2

    def test_within_cooldown_skips_sync(self):
        user = UserFactory(hevy_api_key="key")
        HevySyncLog.objects.create(
            user=user,
            started_at=datetime.now(tz=UTC) - timedelta(minutes=1),
            completed_at=datetime.now(tz=UTC) - timedelta(minutes=1),
            success=True,
        )
        with patch("hevy_api.services.HevyClient") as mock_client:
            pooled = sync_user_lifts(user)

        assert pooled == 0
        mock_client.assert_not_called()
        assert HevySyncLog.objects.filter(user=user).count() == 1

    def test_api_error_marks_log_failed_and_returns_zero(self):
        user = UserFactory(hevy_api_key="key")
        client = _stub_client()
        client.get_workout_events.side_effect = HevyAPIError(500, "boom")

        with patch("hevy_api.services.HevyClient", return_value=client):
            pooled = sync_user_lifts(user)

        assert pooled == 0
        log = HevySyncLog.objects.get(user=user)
        assert log.success is False
        assert "boom" in log.error_detail

    def test_resync_same_set_updates_not_duplicates(self):
        user = UserFactory(hevy_api_key="key")
        workout = _workout(exercises=[_exercise("Back Squat", [_set()])])

        with patch(
            "hevy_api.services.HevyClient",
            return_value=_stub_client([_events_page([_updated_event(workout)])]),
        ):
            sync_user_lifts(user)
        with patch(
            "hevy_api.services.HevyClient",
            return_value=_stub_client([_events_page([_updated_event(workout)])]),
        ):
            sync_user_lifts(user, force=True)

        assert LiftHistory.objects.filter(user=user).count() == 1

    def test_alias_applied_during_sync(self):
        HevyLiftAliasFactory(from_name="Squat (Barbell)", to_name="Back Squat")
        user = UserFactory(hevy_api_key="key")
        workout = _workout(exercises=[_exercise("Squat (Barbell)", [_set()])])

        with patch(
            "hevy_api.services.HevyClient",
            return_value=_stub_client([_events_page([_updated_event(workout)])]),
        ):
            sync_user_lifts(user)

        assert LiftHistory.objects.filter(user=user, lift="Back Squat").exists()

    def test_performs_no_scoring(self):
        from scoring.models import PointEarnEvent

        user = UserFactory(hevy_api_key="key")
        workout = _workout(exercises=[_exercise("Back Squat", [_set()])])

        with patch(
            "hevy_api.services.HevyClient",
            return_value=_stub_client([_events_page([_updated_event(workout)])]),
        ):
            pooled = sync_user_lifts(user)

        assert pooled == 1
        assert not PointEarnEvent.objects.exists()

    def test_pagination_beyond_safety_cap_logs_warning_and_stops(self, caplog):
        user = UserFactory(hevy_api_key="key")
        # page_count always reports one more page than MAX_EVENT_PAGES_PER_RUN,
        # so the walk hits the safety cap rather than terminating naturally.
        huge_page_count = MAX_EVENT_PAGES_PER_RUN + 1
        pages = [
            _events_page([], page=p, page_count=huge_page_count)
            for p in range(1, MAX_EVENT_PAGES_PER_RUN + 1)
        ]
        client = _stub_client(pages)

        with (
            patch("hevy_api.services.HevyClient", return_value=client),
            caplog.at_level("WARNING"),
        ):
            sync_user_lifts(user)

        assert client.get_workout_events.call_count == MAX_EVENT_PAGES_PER_RUN
        assert any("truncated" in message for message in caplog.messages)

    def test_write_contention_retried_then_succeeds(self):
        user = UserFactory(hevy_api_key="key")
        workout = _workout(exercises=[_exercise("Back Squat", [_set()])])
        client = _stub_client([_events_page([_updated_event(workout)])])

        call_count = {"n": 0}
        real_bulk_create = LiftHistory.objects.bulk_create

        def flaky_bulk_create(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise OperationalError("database is locked")
            return real_bulk_create(*args, **kwargs)

        with (
            patch("hevy_api.services.HevyClient", return_value=client),
            patch.object(LiftHistory.objects, "bulk_create", flaky_bulk_create),
            patch("hevy_api.services.time.sleep"),
        ):
            pooled = sync_user_lifts(user)

        assert pooled == 1
        assert LiftHistory.objects.filter(user=user).count() == 1

    def test_db_contention_marks_log_failed_and_returns_zero(self):
        user = UserFactory(hevy_api_key="key")
        client = _stub_client()

        with (
            patch("hevy_api.services.HevyClient", return_value=client),
            patch(
                "hevy_api.services.history_watermark",
                side_effect=OperationalError("database is locked"),
            ),
        ):
            pooled = sync_user_lifts(user)

        assert pooled == 0
        log = HevySyncLog.objects.get(user=user)
        assert log.success is False
        assert "database is locked" in log.error_detail


# ---------------------------------------------------------------------------
# last_synced_at
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestLastSyncedAt:
    def test_returns_none_when_no_successful_sync(self):
        user = UserFactory(hevy_api_key="key")
        assert last_synced_at(user) is None

    def test_ignores_failed_syncs(self):
        user = UserFactory(hevy_api_key="key")
        HevySyncLog.objects.create(
            user=user, started_at=datetime.now(tz=UTC), success=False
        )
        assert last_synced_at(user) is None

    def test_returns_latest_successful_started_at(self):
        user = UserFactory(hevy_api_key="key")
        older = datetime.now(tz=UTC) - timedelta(days=1)
        newer = datetime.now(tz=UTC)
        HevySyncLog.objects.create(user=user, started_at=older, success=True)
        HevySyncLog.objects.create(user=user, started_at=newer, success=True)
        assert last_synced_at(user) == newer
