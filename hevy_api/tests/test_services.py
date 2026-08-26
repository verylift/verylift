"""Tests for hevy_api.services (TASK-312)."""

import json
import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from django.db import OperationalError

from accounts.tests.factories import UserFactory
from hevy_api.client import HevyAPIError
from hevy_api.models import HevySyncLog
from hevy_api.services import (
    HEVY_KEY_INVALID,
    HEVY_KEY_UNKNOWN,
    HEVY_KEY_VALID,
    HISTORY_BACKFILL_DAYS,
    MAX_EVENT_PAGES_PER_BACKGROUND_RUN,
    MAX_EVENT_PAGES_PER_INLINE_RUN,
    _parse_workout,
    _run_backfill_in_thread,
    last_synced_at,
    latest_sync_failure,
    sync_user_lifts,
    trigger_hevy_event_catchup,
    trigger_hevy_lift_history_backfill,
    validate_hevy_key,
    validate_hevy_key_status,
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
# validate_hevy_key_status
# ---------------------------------------------------------------------------


class TestValidateHevyKeyStatus:
    def test_valid_key_returns_valid(self):
        client = MagicMock()
        with patch("hevy_api.services.HevyClient", return_value=client):
            assert validate_hevy_key_status("key") == HEVY_KEY_VALID

    @pytest.mark.parametrize("status_code", [401, 403])
    def test_auth_failure_returns_invalid(self, status_code):
        client = MagicMock()
        client.get_workouts.side_effect = HevyAPIError(status_code, "Unauthorized")
        with patch("hevy_api.services.HevyClient", return_value=client):
            assert validate_hevy_key_status("key") == HEVY_KEY_INVALID

    @pytest.mark.parametrize("status_code", [429, 500, 503])
    def test_non_auth_api_error_returns_unknown_not_invalid(self, status_code):
        """A 5xx/429 says nothing about whether the key itself is good --
        treating it the same as a confirmed rejection would refuse to save a
        possibly-valid key just because Hevy was having a bad moment."""
        client = MagicMock()
        client.get_workouts.side_effect = HevyAPIError(status_code, "server error")
        with patch("hevy_api.services.HevyClient", return_value=client):
            assert validate_hevy_key_status("key") == HEVY_KEY_UNKNOWN

    def test_network_error_returns_unknown(self):
        import urllib.error

        client = MagicMock()
        client.get_workouts.side_effect = urllib.error.URLError("no network")
        with patch("hevy_api.services.HevyClient", return_value=client):
            assert validate_hevy_key_status("key") == HEVY_KEY_UNKNOWN

    def test_generic_exception_returns_unknown(self):
        client = MagicMock()
        client.get_workouts.side_effect = ValueError("boom")
        with patch("hevy_api.services.HevyClient", return_value=client):
            assert validate_hevy_key_status("key") == HEVY_KEY_UNKNOWN


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

    def test_first_sync_ignores_shared_pool_history(self):
        """TASK-319: a user who already has Liftosaur/CSV/manual history still
        gets the full backfill window on their first-ever Hevy connect, not
        since=today derived from the other source's recent row."""
        user = UserFactory(hevy_api_key="key")
        LiftHistory.objects.create(
            user=user,
            lift="Back Squat",
            performed_at=datetime.now(tz=UTC).date(),
            weight_kg=Decimal("100"),
            reps=5,
            source=LiftSource.LIFTOSAUR,
        )
        client = _stub_client()

        with patch("hevy_api.services.HevyClient", return_value=client):
            sync_user_lifts(user)

        _, kwargs = client.get_workout_events.call_args
        expected_start = (
            datetime.now(tz=UTC) - timedelta(days=HISTORY_BACKFILL_DAYS)
        ).date().isoformat() + "T00:00:00Z"
        assert kwargs["since"] == expected_start

    def test_rerun_uses_hevy_scoped_watermark(self):
        """A second Hevy sync watermarks off this connector's own pooled rows,
        not the shared pool's newest row from another source."""
        user = UserFactory(hevy_api_key="key")
        HevySyncLog.objects.create(
            user=user, started_at=datetime.now(tz=UTC), success=True
        )
        hevy_watermark_date = (datetime.now(tz=UTC) - timedelta(days=10)).date()
        LiftHistory.objects.create(
            user=user,
            lift="Back Squat",
            performed_at=hevy_watermark_date,
            weight_kg=Decimal("100"),
            reps=5,
            source=LiftSource.HEVY_API,
        )
        LiftHistory.objects.create(
            user=user,
            lift="Bench Press",
            performed_at=datetime.now(tz=UTC).date(),
            weight_kg=Decimal("80"),
            reps=5,
            source=LiftSource.LIFTOSAUR,
        )
        client = _stub_client()

        with patch("hevy_api.services.HevyClient", return_value=client):
            sync_user_lifts(user, force=True)

        _, kwargs = client.get_workout_events.call_args
        assert kwargs["since"] == f"{hevy_watermark_date.isoformat()}T00:00:00Z"

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

    def test_url_error_marks_log_failed_and_returns_zero(self):
        import urllib.error

        user = UserFactory(hevy_api_key="key")
        client = _stub_client()
        client.get_workout_events.side_effect = urllib.error.URLError("no network")

        with patch("hevy_api.services.HevyClient", return_value=client):
            pooled = sync_user_lifts(user)

        assert pooled == 0
        log = HevySyncLog.objects.get(user=user)
        assert log.success is False

    def test_timeout_error_marks_log_failed_and_returns_zero(self):
        user = UserFactory(hevy_api_key="key")
        client = _stub_client()
        client.get_workout_events.side_effect = TimeoutError("timed out")

        with patch("hevy_api.services.HevyClient", return_value=client):
            pooled = sync_user_lifts(user)

        assert pooled == 0
        log = HevySyncLog.objects.get(user=user)
        assert log.success is False

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
        # page_count always reports one more page than the inline safety cap
        # sync_user_lifts uses by default, so the walk hits the cap rather
        # than terminating naturally.
        huge_page_count = MAX_EVENT_PAGES_PER_INLINE_RUN + 1
        pages = [
            _events_page([], page=p, page_count=huge_page_count)
            for p in range(1, MAX_EVENT_PAGES_PER_INLINE_RUN + 1)
        ]
        client = _stub_client(pages)

        with (
            patch("hevy_api.services.HevyClient", return_value=client),
            patch("hevy_api.services.trigger_hevy_event_catchup"),
            caplog.at_level("WARNING"),
        ):
            sync_user_lifts(user)

        assert client.get_workout_events.call_count == MAX_EVENT_PAGES_PER_INLINE_RUN
        assert any("truncated" in message for message in caplog.messages)

    def test_truncated_walk_records_incomplete_and_since_used(self):
        """TASK-325: a truncated walk must not be indistinguishable from a
        completed one, or the next sync has no way to know it should resume
        from the same `since` instead of a partial watermark."""
        user = UserFactory(hevy_api_key="key")
        huge_page_count = MAX_EVENT_PAGES_PER_INLINE_RUN + 1
        pages = [
            _events_page([], page=p, page_count=huge_page_count)
            for p in range(1, MAX_EVENT_PAGES_PER_INLINE_RUN + 1)
        ]
        client = _stub_client(pages)

        with (
            patch("hevy_api.services.HevyClient", return_value=client),
            patch("hevy_api.services.trigger_hevy_event_catchup"),
        ):
            sync_user_lifts(user)

        log = HevySyncLog.objects.get(user=user)
        assert log.success is True
        assert log.walk_complete is False
        expected_start = (
            datetime.now(tz=UTC) - timedelta(days=HISTORY_BACKFILL_DAYS)
        ).date().isoformat() + "T00:00:00Z"
        assert log.since_used == expected_start

    def test_completed_walk_records_complete(self):
        user = UserFactory(hevy_api_key="key")
        client = _stub_client([_events_page([], page=1, page_count=1)])

        with patch("hevy_api.services.HevyClient", return_value=client):
            sync_user_lifts(user)

        log = HevySyncLog.objects.get(user=user)
        assert log.walk_complete is True

    def test_resync_after_truncation_resumes_from_same_since_not_watermark(self):
        """Pins the TASK-325 fix: if the API ever did return newest-first
        events (confirmed against Hevy's own spec, see the module docstring),
        advancing to a watermark derived from a truncated walk's pooled rows
        would skip everything older that the walk never reached. The next
        sync must re-request the exact same `since` instead."""
        user = UserFactory(hevy_api_key="key")
        # First sync: pools one recent workout, but truncates before reaching
        # every page -- as if far older events are still waiting beyond the
        # inline cap.
        recent_workout = _workout(
            start_time="2024-06-01T12:00:00Z",
            exercises=[_exercise("Back Squat", [_set()])],
        )
        huge_page_count = MAX_EVENT_PAGES_PER_INLINE_RUN + 1
        first_pages = [
            _events_page(
                [_updated_event(recent_workout)], page=1, page_count=huge_page_count
            )
        ] + [
            _events_page([], page=p, page_count=huge_page_count)
            for p in range(2, MAX_EVENT_PAGES_PER_INLINE_RUN + 1)
        ]
        client = _stub_client(first_pages)
        with (
            patch("hevy_api.services.HevyClient", return_value=client),
            patch("hevy_api.services.trigger_hevy_event_catchup"),
        ):
            sync_user_lifts(user)

        expected_start = (
            datetime.now(tz=UTC) - timedelta(days=HISTORY_BACKFILL_DAYS)
        ).date().isoformat() + "T00:00:00Z"

        # Second sync (e.g. after cooldown, or force=True): must request the
        # same original `since`, NOT a watermark derived from the pooled
        # 2024-06-01 row -- that would jump forward and never reach whatever
        # is older than 2024-06-01 but still newer than the true original
        # since.
        second_client = _stub_client()
        with patch("hevy_api.services.HevyClient", return_value=second_client):
            sync_user_lifts(user, force=True)

        _, kwargs = second_client.get_workout_events.call_args
        assert kwargs["since"] == expected_start

    def test_truncated_inline_walk_triggers_background_catchup(self):
        user = UserFactory(hevy_api_key="key")
        huge_page_count = MAX_EVENT_PAGES_PER_INLINE_RUN + 1
        pages = [
            _events_page([], page=p, page_count=huge_page_count)
            for p in range(1, MAX_EVENT_PAGES_PER_INLINE_RUN + 1)
        ]
        client = _stub_client(pages)

        with (
            patch("hevy_api.services.HevyClient", return_value=client),
            patch("hevy_api.services.trigger_hevy_event_catchup") as mock_catchup,
        ):
            sync_user_lifts(user)

        mock_catchup.assert_called_once_with(user)

    def test_completed_inline_walk_does_not_trigger_catchup(self):
        user = UserFactory(hevy_api_key="key")
        client = _stub_client([_events_page([], page=1, page_count=1)])

        with (
            patch("hevy_api.services.HevyClient", return_value=client),
            patch("hevy_api.services.trigger_hevy_event_catchup") as mock_catchup,
        ):
            sync_user_lifts(user)

        mock_catchup.assert_not_called()

    def test_truncated_background_walk_does_not_retrigger_catchup(self):
        """A run already at the background cap that still truncates must not
        spawn another background thread -- otherwise a pathological backlog
        beyond even the background cap would spawn catch-up threads forever.
        MAX_EVENT_PAGES_PER_BACKGROUND_RUN is patched down to 2 so the test
        doesn't need hundreds of stub pages to force truncation at the real
        cap."""
        user = UserFactory(hevy_api_key="key")
        pages = [_events_page([], page=p, page_count=3) for p in (1, 2)]
        client = _stub_client(pages)

        with (
            patch("hevy_api.services.HevyClient", return_value=client),
            patch("hevy_api.services.MAX_EVENT_PAGES_PER_BACKGROUND_RUN", 2),
            patch("hevy_api.services.trigger_hevy_event_catchup") as mock_catchup,
        ):
            sync_user_lifts(user, max_pages=2)

        mock_catchup.assert_not_called()

    def test_max_pages_override_bounds_the_walk(self):
        """A caller passing a smaller max_pages than the default gets stopped
        there -- this is what lets the inline sync path use a tighter cap than
        the background backfill thread without duplicating the walk logic."""
        user = UserFactory(hevy_api_key="key")
        pages = [_events_page([], page=p, page_count=5) for p in range(1, 3)]
        client = _stub_client(pages)

        with patch("hevy_api.services.HevyClient", return_value=client):
            sync_user_lifts(user, max_pages=1)

        assert client.get_workout_events.call_count == 1

    def test_background_run_permits_more_pages_than_inline_default(self):
        """The background backfill cap must be strictly larger than the
        inline default -- otherwise there is no point maintaining two
        constants, and a real multi-year backfill would get truncated at the
        same small cap as a routine in-request delta sync."""
        assert MAX_EVENT_PAGES_PER_BACKGROUND_RUN > MAX_EVENT_PAGES_PER_INLINE_RUN

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
        HevySyncLog.objects.create(
            user=user, started_at=datetime.now(tz=UTC), success=True
        )
        client = _stub_client()

        with (
            patch("hevy_api.services.HevyClient", return_value=client),
            patch(
                "hevy_api.services.history_watermark",
                side_effect=OperationalError("database is locked"),
            ),
        ):
            pooled = sync_user_lifts(user, force=True)

        assert pooled == 0
        log = HevySyncLog.objects.filter(user=user, success=False).get()
        assert "database is locked" in log.error_detail


# ---------------------------------------------------------------------------
# Background backfill trigger (TASK-320)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestTriggerHevyLiftHistoryBackfill:
    """Mirrors liftosaur.tests.test_services' equivalent coverage for
    trigger_lift_history_backfill / _run_backfill_in_thread."""

    def test_trigger_runs_backfill_off_thread(self):
        user = UserFactory(hevy_api_key="key")
        with (
            patch("hevy_api.services.sync_user_lifts") as mock_sync,
            patch("hevy_api.services.threading.Thread") as mock_thread,
        ):
            trigger_hevy_lift_history_backfill(user)

        mock_thread.assert_called_once()
        kwargs = mock_thread.call_args.kwargs
        assert kwargs["args"] == (user,)
        assert kwargs["daemon"] is True
        mock_thread.return_value.start.assert_called_once()
        mock_sync.assert_not_called()

    def test_thread_entry_point_runs_backfill_and_closes_connection(self):
        user = UserFactory(hevy_api_key="key")
        client = _stub_client()
        with (
            patch("hevy_api.services.HevyClient", return_value=client),
            patch("django.db.connection.close") as mock_close,
        ):
            _run_backfill_in_thread(user)

        client.get_workout_events.assert_called()
        mock_close.assert_called_once()

    def test_thread_entry_point_logs_unexpected_exception(self, caplog):
        user = UserFactory(hevy_api_key="key")
        with (
            patch("hevy_api.services.sync_user_lifts") as mock_sync,
            patch("django.db.connection.close") as mock_close,
        ):
            mock_sync.side_effect = RuntimeError("boom")
            with caplog.at_level(logging.ERROR, logger="hevy_api.services"):
                _run_backfill_in_thread(user)

        assert len(caplog.records) == 1
        record = caplog.records[0]
        assert record.exc_info is not None
        assert str(user.id) in record.message
        mock_close.assert_called_once()

    def test_thread_entry_point_uses_background_page_cap(self):
        """The thread entry point must call sync_user_lifts with the
        background cap explicitly -- calling it with no override would
        silently fall back to the small inline default and truncate a real
        backfill after a handful of pages."""
        user = UserFactory(hevy_api_key="key")
        with (
            patch("hevy_api.services.sync_user_lifts") as mock_sync,
            patch("django.db.connection.close"),
        ):
            _run_backfill_in_thread(user)

        mock_sync.assert_called_once_with(
            user, force=False, max_pages=MAX_EVENT_PAGES_PER_BACKGROUND_RUN
        )

    def test_catchup_thread_forces_past_cooldown(self):
        """trigger_hevy_event_catchup must bypass the sync cooldown --
        the inline call that just truncated already wrote a successful
        HevySyncLog, so without force=True the catch-up would be skipped by
        recent_pull_exists until the cooldown window passes."""
        user = UserFactory(hevy_api_key="key")
        with (
            patch("hevy_api.services.sync_user_lifts") as mock_sync,
            patch("hevy_api.services.threading.Thread") as mock_thread,
        ):
            trigger_hevy_event_catchup(user)

        mock_thread.assert_called_once()
        kwargs = mock_thread.call_args.kwargs
        assert kwargs["args"] == (user,)
        assert kwargs["kwargs"] == {"force": True}
        assert kwargs["daemon"] is True
        mock_thread.return_value.start.assert_called_once()
        mock_sync.assert_not_called()


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


# ---------------------------------------------------------------------------
# latest_sync_failure
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestLatestSyncFailure:
    def test_returns_none_when_no_sync_ever_ran(self):
        user = UserFactory(hevy_api_key="key")
        assert latest_sync_failure(user) is None

    def test_returns_none_when_most_recent_sync_succeeded(self):
        user = UserFactory(hevy_api_key="key")
        older = datetime.now(tz=UTC) - timedelta(days=1)
        newer = datetime.now(tz=UTC)
        HevySyncLog.objects.create(user=user, started_at=older, success=False)
        HevySyncLog.objects.create(user=user, started_at=newer, success=True)
        assert latest_sync_failure(user) is None

    def test_returns_none_when_most_recent_sync_still_in_progress(self):
        user = UserFactory(hevy_api_key="key")
        HevySyncLog.objects.create(
            user=user, started_at=datetime.now(tz=UTC), success=None
        )
        assert latest_sync_failure(user) is None

    def test_returns_log_when_most_recent_sync_failed(self):
        user = UserFactory(hevy_api_key="key")
        older = datetime.now(tz=UTC) - timedelta(days=1)
        newer = datetime.now(tz=UTC)
        HevySyncLog.objects.create(user=user, started_at=older, success=True)
        failed = HevySyncLog.objects.create(
            user=user,
            started_at=newer,
            success=False,
            error_detail="Hevy API error 401: Unauthorized",
        )
        result = latest_sync_failure(user)
        assert result.id == failed.id
        assert result.error_detail == "Hevy API error 401: Unauthorized"


# ---------------------------------------------------------------------------
# CSV/API weight parity (TASK-325)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestCsvApiWeightParity:
    """The CSV importer (workout_imports.importers.hevy) and this module's
    API sync both feed LiftHistory's (user, lift, performed_at, reps,
    weight_kg) unique key. A user who imports a Hevy CSV export and later
    connects live sync must have the same physical set resolve to the same
    weight_kg through both paths, or it double-counts as two rows. The CSV
    path converts weight_lbs -> weight_kg via accounts.units.LB_TO_KG; the
    API path takes weight_kg straight from Hevy with no conversion. This
    pins that the two agree for realistic loads, including ones where the
    conversion constant used to diverge from the exact pound definition
    (see accounts/units.py's TASK-325 comment)."""

    @pytest.mark.parametrize("weight_lbs", [45, 135, 185, 225, 315, 380, 405, 495, 585])
    def test_csv_and_api_paths_agree_on_weight_kg(self, weight_lbs):
        import io

        from workout_imports.importers.hevy import HevyImporter

        csv_header = (
            "title,start_time,end_time,description,exercise_title,"
            "superset_id,exercise_notes,set_index,set_type,weight_lbs,reps,"
            "distance_km,duration_seconds,rpe"
        )
        csv_row = (
            f'Leg day,"01 Jan 2024, 09:15",,,Squat (Barbell),,,1,normal,'
            f"{weight_lbs},5,,,\n"
        )
        csv_parsed = HevyImporter().parse(
            io.BytesIO((csv_header + "\n" + csv_row).encode("utf-8"))
        )

        # Hevy's API reports weight_kg directly for the same physical set --
        # simulated here via the exact pound definition, since that's the
        # only conversion an independently-implemented client and Hevy's own
        # backend can be expected to agree on.
        api_weight_kg = (Decimal(weight_lbs) * Decimal("0.45359237")).quantize(
            Decimal("0.01")
        )
        api_workout = _workout(
            exercises=[_exercise("Squat (Barbell)", [_set(weight_kg=api_weight_kg)])]
        )
        api_parsed = _parse_workout(api_workout, {})

        assert csv_parsed[0].weight_kg == api_parsed[0].weight_kg
