"""Tests for liftosaur.services (TASK-12, TASK-24)."""

import json
import logging
import urllib.error
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from django.db import OperationalError, connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from accounts.tests.factories import UserFactory
from liftosaur.client import LiftosaurAPIError
from liftosaur.models import LB_TO_KG, LiftHistory, LiftosaurSyncLog
from liftosaur.services import (
    HISTORY_BACKFILL_DAYS,
    POOL_WRITE_RETRY_DELAYS,
    _parse_date,
    _parse_history_record,
    _parse_weight_value,
    _run_backfill_in_thread,
    _write_history_batch,
    canonical_lift_name,
    last_synced_at,
    sync_user_lifts,
    trigger_lift_history_backfill,
    validate_liftosaur_key,
)
from liftosaur.tests.factories import LiftHistoryFactory, LiftosaurSyncLogFactory


def _make_response(status, body):
    """Build a mock urllib response context manager."""
    mock_resp = MagicMock()
    mock_resp.status = status
    mock_resp.read.return_value = json.dumps(body).encode("utf-8")
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    return mock_resp


class TestValidateLiftosaurKey:
    """validate_liftosaur_key is purely a key-validation probe (TASK-248): it
    happens to hit the weight-measurements endpoint, but returns only a bool
    now — nothing about the measurement values survives the call."""

    def test_valid_key_returns_true(self):
        body = [{"value": "80kg", "date": "2024-01-01"}]
        mock_resp = _make_response(200, body)
        with patch("urllib.request.urlopen", return_value=mock_resp):
            assert validate_liftosaur_key("good-key") is True

    def test_valid_key_with_measurements_wrapper(self):
        body = {"measurements": [{"value": "80kg"}]}
        mock_resp = _make_response(200, body)
        with patch("urllib.request.urlopen", return_value=mock_resp):
            assert validate_liftosaur_key("good-key") is True

    def test_http_error_returns_false(self):
        url = "https://www.liftosaur.com/api/v1/measurements/weight"
        error = urllib.error.HTTPError(url, 401, "Unauthorized", {}, None)
        with patch("urllib.request.urlopen", side_effect=error):
            assert validate_liftosaur_key("bad-key") is False

    def test_url_error_returns_false(self):
        error = urllib.error.URLError("Network error")
        with patch("urllib.request.urlopen", side_effect=error):
            assert validate_liftosaur_key("bad-key") is False

    def test_generic_exception_returns_false(self):
        with patch("urllib.request.urlopen", side_effect=OSError("timeout")):
            assert validate_liftosaur_key("bad-key") is False

    def test_request_includes_bearer_token(self):
        body = []
        mock_resp = _make_response(200, body)
        with patch("urllib.request.urlopen", return_value=mock_resp) as mock_open:
            validate_liftosaur_key("my-secret-key")
        req = mock_open.call_args[0][0]
        assert req.get_header("Authorization") == "Bearer my-secret-key"

    def test_non_200_status_returns_false(self):
        # urllib raises HTTPError for non-2xx; simulate that behaviour.
        url = "https://www.liftosaur.com/api/v1/measurements/weight"
        error = urllib.error.HTTPError(url, 403, "Forbidden", {}, None)
        with patch("urllib.request.urlopen", side_effect=error):
            assert validate_liftosaur_key("key") is False

    def test_empty_list_response_is_valid(self):
        body = []
        mock_resp = _make_response(200, body)
        with patch("urllib.request.urlopen", return_value=mock_resp):
            assert validate_liftosaur_key("good-key") is True


@pytest.mark.django_db
class TestCanonicalLiftName:
    """canonical_lift_name resolves via the seeded LiftAlias table (conftest
    seeds the fixture once per session)."""

    def test_squat_maps_to_back_squat(self):
        assert canonical_lift_name("Squat") == "Back Squat"

    def test_pull_up_maps_to_pullup(self):
        assert canonical_lift_name("Pull Up") == "Pull-up"

    def test_barbell_row_maps_to_pendlay_row(self):
        assert canonical_lift_name("Barbell Row") == "Pendlay Row"

    def test_unmapped_name_passes_through_unchanged(self):
        assert canonical_lift_name("Back Squat") == "Back Squat"
        assert canonical_lift_name("Some Novel Lift") == "Some Novel Lift"

    def test_chest_dip_maps_to_dip(self):
        assert canonical_lift_name("Chest Dip") == "Dip"

    def test_chest_dip_history_line_is_pooled_under_dip(self):
        """Regression: Liftosaur emits Dip as 'Chest Dip, Bodyweight'. The parser
        strips the equipment suffix before canonicalisation, so the alias must map
        the bare 'Chest Dip' — not the literal 'Chest Dip, Bodyweight' — to 'Dip',
        or the set is pooled under a name that matches no strength standard."""
        text = (
            "2026-06-18T10:00:00Z / exercises: {\n  Chest Dip, Bodyweight / 3x8 0kg\n}"
        )
        sets = _parse_history_record(text)
        assert len(sets) == 3
        assert all(s.exercise == "Chest Dip" for s in sets)
        assert all(s.equipment == "Bodyweight" for s in sets)
        assert canonical_lift_name(sets[0].exercise) == "Dip"

    def test_behind_the_neck_press_maps_to_snatch_press(self):
        assert canonical_lift_name("Behind the Neck Press") == "Snatch Press"

    def test_alias_lookup_is_case_insensitive(self):
        """Liftosaur emits 'Behind The Neck Press' (capital 'The') while the
        seeded alias reads 'Behind the Neck Press'. An exact match would miss and
        the set would be pooled under its raw name, invisible to scoring."""
        assert canonical_lift_name("Behind The Neck Press") == "Snatch Press"
        assert canonical_lift_name("BEHIND THE NECK PRESS") == "Snatch Press"
        assert canonical_lift_name("squat") == "Back Squat"
        assert canonical_lift_name("BARBELL ROW") == "Pendlay Row"

    def test_behind_the_neck_press_reported_repro_resolves(self):
        """Exact reported case (TASK-156): parsing 'Behind The Neck Press /
        1x7 60lb' — the real casing Liftosaur emitted in the user's data —
        resolves to the canonical 'Snatch Press'."""
        text = (
            "2026-06-20T10:00:00Z / exercises: {\n  Behind The Neck Press / 1x7 60lb\n}"
        )
        sets = _parse_history_record(text)
        assert len(sets) == 1
        assert sets[0].exercise == "Behind The Neck Press"
        assert canonical_lift_name(sets[0].exercise) == "Snatch Press"

    def test_behind_the_neck_press_history_line_pooled_under_snatch_press(self):
        """Regression: Liftosaur emits 'Behind the Neck Press, Barbell'. The parser
        strips the equipment suffix before canonicalisation, so the alias must map
        the bare 'Behind the Neck Press' — not the literal 'Behind the Neck Press,
        Barbell' — to 'Snatch Press', or the set is pooled under a name that
        matches no strength standard."""
        text = (
            "2026-06-18T10:00:00Z / exercises: {\n"
            "  Behind the Neck Press, Barbell / 3x8 40kg\n}"
        )
        sets = _parse_history_record(text)
        assert len(sets) == 3
        assert all(s.exercise == "Behind the Neck Press" for s in sets)
        assert all(s.equipment == "Barbell" for s in sets)
        assert canonical_lift_name(sets[0].exercise) == "Snatch Press"


class TestParseHelpers:
    def test_parse_weight_value_kg(self):
        assert _parse_weight_value("80kg") == (Decimal("80"), "kg")

    def test_parse_weight_value_lb(self):
        assert _parse_weight_value("180lb") == (Decimal("180"), "lb")

    def test_parse_weight_value_rejects_garbage(self):
        assert _parse_weight_value("heavy") is None

    def test_parse_weight_value_rejects_bare_dot(self):
        assert _parse_weight_value(".kg") is None

    def test_parse_weight_value_empty(self):
        assert _parse_weight_value("") is None

    def test_parse_date_iso_datetime(self):
        parsed = _parse_date("2024-01-15T10:30:00")
        assert parsed == datetime(2024, 1, 15, 10, 30, tzinfo=UTC)

    def test_parse_date_iso_millis_zulu(self):
        # Liftosaur bodyweight measurements use ISO millis + Z, e.g.
        # "2025-12-05T05:19:08.000Z".
        parsed = _parse_date("2025-12-05T05:19:08.000Z")
        assert parsed == datetime(2025, 12, 5, 5, 19, 8, tzinfo=UTC)

    def test_parse_date_invalid(self):
        assert _parse_date("nonsense") is None

    def test_history_real_format_full_record(self):
        text = (
            '2026-06-18 18:00:06 +00:00 / program: "GZCLP" / dayName: "Day 1" '
            "/ week: 1 / dayInWeek: 1 / duration: 3443s / exercises: {\n"
            "  Squat / 4x3 165lb, 1x10 165lb / warmup: 1x5 45lb, 1x5 75lb "
            "/ target: 4x3 165lb, 1x3+ 165lb\n"
            "  Bench Press / 2x10 105lb, 1x8 155lb, 1x5 135lb "
            "/ warmup: 1x5 45lb, 1x5 75lb / target: 2x10 105lb, 2x10 155lb\n"
            "  Lat Pulldown / 3x15 40lb / warmup: 1x5 20lb, 1x5 30lb "
            "/ target: 2x15 40lb 90s, 1x15+ 40lb 90s\n"
            "}"
        )
        sets = _parse_history_record(text)

        squat = [s for s in sets if s.exercise == "Squat"]
        bench = [s for s in sets if s.exercise == "Bench Press"]
        pulldown = [s for s in sets if s.exercise == "Lat Pulldown"]

        assert len(squat) == 5  # 4x3 + 1x10
        assert len(bench) == 4  # 2x10 + 1x8 + 1x5
        assert len(pulldown) == 3  # 3x15
        assert {s.reps for s in squat} == {3, 10}
        assert squat[0].weight_kg == Decimal("165") * LB_TO_KG
        assert squat[0].performed_at == datetime(2026, 6, 18, 18, 0, 6, tzinfo=UTC)

    def test_history_splits_equipment_suffix_from_name(self):
        text = (
            "2026-06-18T10:00:00Z / exercises: {\n  Bench Press, Barbell / 3x8 185lb\n}"
        )
        sets = _parse_history_record(text)
        assert all(s.exercise == "Bench Press" for s in sets)
        assert all(s.equipment == "Barbell" for s in sets)
        assert len(sets) == 3

    def test_history_retains_leverage_machine_equipment(self):
        text = (
            "2026-06-18T10:00:00Z / exercises: {\n"
            "  Pull Up, Leverage Machine / 2x8 152lb\n"
            "}"
        )
        sets = _parse_history_record(text)
        assert len(sets) == 2
        assert all(s.exercise == "Pull Up" for s in sets)
        assert all(s.equipment == "Leverage Machine" for s in sets)
        assert sets[0].weight_kg == Decimal("152") * LB_TO_KG

    def test_history_without_equipment_suffix_has_empty_equipment(self):
        text = "2026-06-18T10:00:00Z / exercises: {\n  Squat / 1x5 100kg\n}"
        sets = _parse_history_record(text)
        assert sets[0].equipment == ""

    def test_history_negative_weight_parses_with_sign(self):
        """Assisted/custom-script sets may report a negative value; the minus
        sign must survive parsing rather than being dropped or rejected."""
        text = "2026-06-18T10:00:00Z / exercises: {\n  Pull Up / 3x8 -40lb\n}"
        sets = _parse_history_record(text)
        assert len(sets) == 3
        assert sets[0].weight_kg == Decimal("-40") * LB_TO_KG

    def test_history_excludes_warmup_and_target(self):
        text = (
            "2026-06-18T10:00:00Z / exercises: {\n"
            "  OHP / 3x10 95lb / warmup: 1x10 45lb / target: 3x10 95lb 60s\n"
            "}"
        )
        sets = _parse_history_record(text)
        assert len(sets) == 3
        assert all(s.weight_kg == Decimal("95") * LB_TO_KG for s in sets)

    def test_history_exercise_with_only_target_is_skipped(self):
        text = "2026-06-18T10:00:00Z / exercises: {\n  OHP / target: 3x10 95lb\n}"
        assert _parse_history_record(text) == []

    def test_history_amrap_marker_treated_as_base_reps(self):
        text = "2026-06-18T10:00:00Z / exercises: {\n  Squat / 1x3+ 165lb\n}"
        sets = _parse_history_record(text)
        assert len(sets) == 1
        assert sets[0].reps == 3

    def test_history_unilateral_takes_first_count(self):
        text = "2026-06-18T10:00:00Z / exercises: {\n  Pull Ups / 3x8|7 0lb\n}"
        sets = _parse_history_record(text)
        assert len(sets) == 3
        assert all(s.reps == 8 for s in sets)

    def test_history_kg_weight_not_converted(self):
        text = "2026-06-18T10:00:00Z / exercises: {\n  Squat / 1x5 100kg\n}"
        sets = _parse_history_record(text)
        assert sets[0].weight_kg == Decimal("100")

    def test_history_rpe_suffix_ignored(self):
        text = "2026-06-18T10:00:00Z / exercises: {\n  Bench Press / 3x8 185lb @7\n}"
        sets = _parse_history_record(text)
        assert len(sets) == 3
        assert sets[0].reps == 8

    def test_history_ignores_note_lines(self):
        text = (
            "// Felt strong today\n"
            "2026-06-18T10:00:00Z / exercises: {\n"
            "  // grippy bar\n"
            "  Squat / 1x5 100kg\n"
            "}"
        )
        sets = _parse_history_record(text)
        assert len(sets) == 1

    def test_history_unparseable_date_yields_no_sets(self):
        text = "not-a-date / exercises: {\n  Squat / 1x5 100kg\n}"
        assert _parse_history_record(text) == []

    def test_history_line_without_sections_is_ignored(self):
        text = "2026-06-18T10:00:00Z / exercises: {\n  Squat\n}"
        assert _parse_history_record(text) == []

    def test_history_set_group_with_unparseable_reps_skipped(self):
        text = "2026-06-18T10:00:00Z / exercises: {\n  Squat / 3x|7 100kg\n}"
        assert _parse_history_record(text) == []

    def test_history_set_group_with_unparseable_weight_skipped(self):
        text = "2026-06-18T10:00:00Z / exercises: {\n  Squat / 1x5 .lb\n}"
        assert _parse_history_record(text) == []


def _stub_client(*, measurements=None, history=None):
    """Build a stubbed LiftosaurClient.

    measurements / history are lists of (values, has_more, next_cursor) tuples,
    returned in order across paginated calls.
    """
    client = MagicMock()
    measure_pages = measurements or [([], False, None)]
    history_pages = history or [([], False, None)]
    client.get_weight_measurements.side_effect = measure_pages
    client.get_history.side_effect = history_pages
    return client


def _history_record(performed_date, exercise, completed):
    """Build a real-format Liftohistory record for a single exercise."""
    return (
        f'{performed_date}T12:00:00Z / program: "P" / exercises: {{\n'
        f"  {exercise} / {completed}\n"
        "}"
    )


@pytest.mark.django_db
class TestSyncUserLiftsFallbackResolutionStages:
    """The live-sync pull now runs the full six-stage resolution chain
    (core.lift_resolution) rather than a bare alias-map lookup -- these
    fallback stages used to only apply to CSV imports. Consequence: on the
    next sync, a raw exercise name that previously landed unmapped can now
    silently resolve and start counting toward standings (accepted -- see
    the PR description)."""

    def test_barbell_qualifier_is_stripped_without_an_explicit_alias(self):
        user = UserFactory(liftosaur_api_key="key")
        performed = (datetime.now(tz=UTC) - timedelta(days=1)).date().isoformat()
        history = [
            (
                [_history_record(performed, "Pendlay Row (Barbell)", "1x5 60kg")],
                False,
                None,
            ),
        ]
        client = _stub_client(history=history)

        with patch("liftosaur.services.LiftosaurClient", return_value=client):
            sync_user_lifts(user)

        assert LiftHistory.objects.get(user=user).lift == "Pendlay Row"

    def test_fallback_stage_hit_logs_a_warning_naming_liftosaur_sync(self, caplog):
        user = UserFactory(liftosaur_api_key="key")
        performed = (datetime.now(tz=UTC) - timedelta(days=1)).date().isoformat()
        history = [
            ([_history_record(performed, "TBar Row", "1x5 60kg")], False, None),
        ]
        client = _stub_client(history=history)

        with (
            patch("liftosaur.services.LiftosaurClient", return_value=client),
            caplog.at_level(logging.WARNING, logger="liftosaur.services"),
        ):
            sync_user_lifts(user)

        assert LiftHistory.objects.get(user=user).lift == "T Bar Row"
        fuzzy_warnings = [
            r for r in caplog.records if "separator-insensitive fallback" in r.message
        ]
        assert len(fuzzy_warnings) == 1
        assert "Liftosaur sync" in fuzzy_warnings[0].message


@pytest.mark.django_db
class TestBackfillLiftHistory:
    def test_no_api_key_is_noop(self):
        user = UserFactory(liftosaur_api_key=None)
        with patch("liftosaur.services.LiftosaurClient") as mock_client:
            pooled = sync_user_lifts(user)
        assert pooled == 0
        mock_client.assert_not_called()
        assert not LiftosaurSyncLog.objects.filter(user=user).exists()

    def test_seeds_pool_from_twelve_month_window(self):
        user = UserFactory(liftosaur_api_key="key")
        performed = (datetime.now(tz=UTC) - timedelta(days=30)).date().isoformat()
        history = [
            ([_history_record(performed, "Back Squat", "1x5 100kg")], False, None),
        ]
        client = _stub_client(history=history)

        with patch("liftosaur.services.LiftosaurClient", return_value=client):
            pooled = sync_user_lifts(user)

        assert pooled == 1
        assert LiftHistory.objects.filter(user=user, lift="Back Squat").count() == 1
        _, kwargs = client.get_history.call_args
        expected_start = (
            (datetime.now(tz=UTC) - timedelta(days=HISTORY_BACKFILL_DAYS))
            .date()
            .isoformat()
        )
        assert kwargs["start_date"] == expected_start
        log = LiftosaurSyncLog.objects.get(user=user)
        assert log.success is True
        assert json.loads(log.result_summary) == {"sets_pooled": 1}

    def test_rerun_uses_delta_watermark_not_full_year(self):
        user = UserFactory(liftosaur_api_key="key")
        watermark_date = (datetime.now(tz=UTC) - timedelta(days=10)).date()
        LiftHistory.objects.create(
            user=user,
            lift="Back Squat",
            performed_at=watermark_date,
            weight_kg=Decimal("100"),
            reps=5,
        )
        client = _stub_client()

        with patch("liftosaur.services.LiftosaurClient", return_value=client):
            sync_user_lifts(user, force=True)

        _, kwargs = client.get_history.call_args
        assert kwargs["start_date"] == watermark_date.isoformat()

    def test_same_day_record_pooled_without_end_date(self):
        """A workout completed earlier today (same UTC day) is pooled, and the
        fetch is open-ended — no end_date is sent, which would otherwise be
        truncated to midnight UTC and silently drop the record."""
        user = UserFactory(liftosaur_api_key="key")
        today = datetime.now(tz=UTC).date().isoformat()
        history = [
            ([_history_record(today, "Back Squat", "1x10 79.38kg")], False, None),
        ]
        client = _stub_client(history=history)

        with patch("liftosaur.services.LiftosaurClient", return_value=client):
            pooled = sync_user_lifts(user, force=True)

        assert pooled == 1
        row = LiftHistory.objects.get(user=user, lift="Back Squat")
        assert row.performed_at.isoformat() == today
        assert row.reps == 10
        _, kwargs = client.get_history.call_args
        assert kwargs.get("end_date") is None

    def test_pagination_stall_guard_breaks_on_repeated_cursor(self):
        """Liftosaur ignores the cursor when startDate is set, returning the same
        page with has_more still true. The stall guard must terminate the loop
        rather than looping forever; the upsert keeps the set deduped to one row."""
        user = UserFactory(liftosaur_api_key="key")
        performed = (datetime.now(tz=UTC) - timedelta(days=5)).date().isoformat()
        record = _history_record(performed, "Back Squat", "1x5 100kg")
        client = _stub_client(
            history=[
                ([record], True, "stuck-cursor"),
                ([record], True, "stuck-cursor"),
                ([record], True, "stuck-cursor"),
            ]
        )

        with patch("liftosaur.services.LiftosaurClient", return_value=client):
            sync_user_lifts(user, force=True)

        # Second fetch repeats the cursor -> break; a third would loop forever.
        assert client.get_history.call_count == 2
        assert LiftHistory.objects.filter(user=user, lift="Back Squat").count() == 1

    def test_within_cooldown_skips_backfill(self):
        user = UserFactory(liftosaur_api_key="key")
        LiftosaurSyncLog.objects.create(
            user=user,
            started_at=datetime.now(tz=UTC) - timedelta(minutes=1),
            completed_at=datetime.now(tz=UTC) - timedelta(minutes=1),
            success=True,
        )
        with patch("liftosaur.services.LiftosaurClient") as mock_client:
            pooled = sync_user_lifts(user)

        assert pooled == 0
        mock_client.assert_not_called()
        assert LiftosaurSyncLog.objects.filter(user=user).count() == 1

    def test_recent_sync_gates_backfill(self):
        # Under the per-user cooldown any recent successful sync log suppresses a
        # re-pull: backfill is a no-op that creates no new log and returns 0.
        user = UserFactory(liftosaur_api_key="key")
        LiftosaurSyncLog.objects.create(
            user=user,
            started_at=datetime.now(tz=UTC) - timedelta(minutes=1),
            completed_at=datetime.now(tz=UTC) - timedelta(minutes=1),
            success=True,
        )
        with patch("liftosaur.services.LiftosaurClient") as mock_client:
            pooled = sync_user_lifts(user)

        assert pooled == 0
        mock_client.assert_not_called()
        assert LiftosaurSyncLog.objects.filter(user=user).count() == 1

    def test_api_error_marks_log_failed_and_returns_zero(self):
        user = UserFactory(liftosaur_api_key="key")
        client = _stub_client()
        client.get_history.side_effect = LiftosaurAPIError(500, "boom")

        with patch("liftosaur.services.LiftosaurClient", return_value=client):
            pooled = sync_user_lifts(user)

        assert pooled == 0
        log = LiftosaurSyncLog.objects.get(user=user)
        assert log.success is False
        assert "boom" in log.error_detail

    def test_network_timeout_marks_log_failed_and_returns_zero(self):
        """A read-phase timeout surfaces as a bare TimeoutError, not
        urllib.error.URLError -- this must degrade the same way an API error
        does, not escape as an unhandled 500 reaching the user."""
        user = UserFactory(liftosaur_api_key="key")
        client = _stub_client()
        client.get_history.side_effect = TimeoutError("The read operation timed out")

        with patch("liftosaur.services.LiftosaurClient", return_value=client):
            pooled = sync_user_lifts(user)

        assert pooled == 0
        log = LiftosaurSyncLog.objects.get(user=user)
        assert log.success is False
        assert "timed out" in log.error_detail

    def test_performs_no_scoring(self):
        # The pure pull primitive keeps the pool fresh and does zero scoring:
        # no PointEarnEvent is ever created as a side effect of syncing lifts.
        from scoring.models import PointEarnEvent

        user = UserFactory(liftosaur_api_key="key")
        performed = (datetime.now(tz=UTC) - timedelta(days=5)).date().isoformat()
        client = _stub_client(
            history=[
                ([_history_record(performed, "Back Squat", "1x5 100kg")], False, None)
            ]
        )

        with patch("liftosaur.services.LiftosaurClient", return_value=client):
            pooled = sync_user_lifts(user)

        assert pooled == 1
        assert not PointEarnEvent.objects.exists()

    def test_resync_same_set_updates_not_duplicates(self):
        user = UserFactory(liftosaur_api_key="key")
        performed = (datetime.now(tz=UTC) - timedelta(days=5)).date().isoformat()
        record = _history_record(performed, "Back Squat", "1x5 120kg")

        with patch(
            "liftosaur.services.LiftosaurClient",
            return_value=_stub_client(history=[([record], False, None)]),
        ):
            sync_user_lifts(user)
        with patch(
            "liftosaur.services.LiftosaurClient",
            return_value=_stub_client(history=[([record], False, None)]),
        ):
            sync_user_lifts(user, force=True)

        assert LiftHistory.objects.filter(user=user).count() == 1

    def test_pooled_set_stamped_with_equipment(self):
        user = UserFactory(liftosaur_api_key="key")
        performed = (datetime.now(tz=UTC) - timedelta(days=5)).date().isoformat()
        client = _stub_client(
            history=[
                (
                    [
                        _history_record(
                            performed, "Pull Up, Leverage Machine", "2x8 152lb"
                        )
                    ],
                    False,
                    None,
                )
            ]
        )

        with patch("liftosaur.services.LiftosaurClient", return_value=client):
            sync_user_lifts(user)

        row = LiftHistory.objects.get(user=user)
        assert row.lift == "Pull-up"
        assert row.equipment == "Leverage Machine"

    def test_full_backfill_ignores_watermark_and_restamps_equipment(self):
        """full_backfill re-pulls the whole window so pre-equipment rows are
        restamped in place (upsert key excludes equipment)."""
        user = UserFactory(liftosaur_api_key="key")
        performed = (datetime.now(tz=UTC) - timedelta(days=5)).date()
        LiftHistoryFactory(
            user=user,
            lift="Pull-up",
            performed_at=performed,
            weight_kg=Decimal("68.95"),
            reps=8,
            equipment="",
        )
        record = _history_record(
            performed.isoformat(), "Pull Up, Leverage Machine", "1x8 152lb"
        )
        client = _stub_client(history=[([record], False, None)])

        with patch("liftosaur.services.LiftosaurClient", return_value=client):
            sync_user_lifts(user, force=True, full_backfill=True)

        _, kwargs = client.get_history.call_args
        assert kwargs["start_date"] != performed.isoformat()  # not the watermark
        row = LiftHistory.objects.get(user=user, lift="Pull-up")
        assert row.equipment == "Leverage Machine"

    def test_thread_entry_point_runs_backfill_and_closes_connection(self):
        user = UserFactory(liftosaur_api_key="key")
        client = _stub_client()
        with (
            patch("liftosaur.services.LiftosaurClient", return_value=client),
            patch("django.db.connection.close") as mock_close,
        ):
            _run_backfill_in_thread(user)

        client.get_history.assert_called()
        mock_close.assert_called_once()

    def test_trigger_runs_backfill_off_thread(self):
        user = UserFactory(liftosaur_api_key="key")
        with (
            patch("liftosaur.services.sync_user_lifts") as mock_backfill,
            patch("liftosaur.services.threading.Thread") as mock_thread,
        ):
            trigger_lift_history_backfill(user)

        mock_thread.assert_called_once()
        kwargs = mock_thread.call_args.kwargs
        assert kwargs["args"] == (user,)
        assert kwargs["daemon"] is True
        mock_thread.return_value.start.assert_called_once()
        mock_backfill.assert_not_called()

    def test_thread_entry_point_logs_unexpected_exception(self, caplog):
        user = UserFactory(liftosaur_api_key="key")
        with (
            patch("liftosaur.services.sync_user_lifts") as mock_sync,
            patch("django.db.connection.close") as mock_close,
        ):
            mock_sync.side_effect = RuntimeError("boom")
            with caplog.at_level(logging.ERROR, logger="liftosaur.services"):
                _run_backfill_in_thread(user)

        # Assert the exception was logged with the user id
        assert len(caplog.records) == 1
        record = caplog.records[0]
        assert record.exc_info is not None
        assert str(user.id) in record.message

        # Assert connection.close was still called
        mock_close.assert_called_once()


@pytest.mark.django_db
class TestDistinctSameDaySameRepSets:
    """TASK-116: two genuinely different sets performed on the same day with the
    same rep count must pool as two rows, not silently overwrite each other."""

    def test_bodyweight_and_assisted_pullup_pool_as_two_rows(self):
        user = UserFactory(liftosaur_api_key="key")
        performed = (datetime.now(tz=UTC) - timedelta(days=5)).date().isoformat()
        # One raw record, same day, both sets 5 reps: a free bodyweight Pull Up
        # (0 added) and an assisted Leverage Machine Pull Up (152lb net load).
        record = (
            f'{performed}T12:00:00Z / program: "P" / exercises: {{\n'
            "  Pull Up / 1x5 0lb\n"
            "  Pull Up, Leverage Machine / 1x5 152lb\n"
            "}"
        )
        client = _stub_client(
            measurements=[([{"value": "87.09kg", "date": performed}], False, None)],
            history=[([record], False, None)],
        )

        with patch("liftosaur.services.LiftosaurClient", return_value=client):
            pooled = sync_user_lifts(user)

        assert pooled == 2
        rows = LiftHistory.objects.filter(user=user, lift="Pull-up")
        assert rows.count() == 2
        equipments = set(rows.values_list("equipment", flat=True))
        assert equipments == {"", "Leverage Machine"}
        # The bodyweight set survives with weight_kg 0; the assisted set carries
        # its net load. Neither overwrote the other.
        assert rows.get(equipment="").weight_kg == Decimal("0.00")
        assert rows.get(equipment="Leverage Machine").weight_kg == (
            Decimal("152") * LB_TO_KG
        ).quantize(Decimal("0.01"))

    def test_resync_updates_in_place_without_duplicating(self):
        # A true re-sync of the identical workout upserts both rows in place
        # rather than creating four.
        user = UserFactory(liftosaur_api_key="key")
        performed = (datetime.now(tz=UTC) - timedelta(days=5)).date().isoformat()
        record = (
            f'{performed}T12:00:00Z / program: "P" / exercises: {{\n'
            "  Pull Up / 1x5 0lb\n"
            "  Pull Up, Leverage Machine / 1x5 152lb\n"
            "}"
        )

        def run_sync():
            client = _stub_client(
                measurements=[([{"value": "87.09kg", "date": performed}], False, None)],
                history=[([record], False, None)],
            )
            with patch("liftosaur.services.LiftosaurClient", return_value=client):
                sync_user_lifts(user, force=True, full_backfill=True)

        run_sync()
        run_sync()

        assert LiftHistory.objects.filter(user=user, lift="Pull-up").count() == 2


@pytest.mark.django_db
class TestLastSyncedAt:
    """last_synced_at returns the latest successful pull's started_at (TASK-144)."""

    def test_returns_none_when_no_successful_sync(self):
        user = UserFactory()
        LiftosaurSyncLogFactory(user=user, success=False)
        assert last_synced_at(user) is None

    def test_returns_latest_successful_started_at(self):
        user = UserFactory()
        older = timezone.now() - timedelta(hours=2)
        newer = timezone.now() - timedelta(minutes=5)
        LiftosaurSyncLogFactory(user=user, success=True, started_at=older)
        latest = LiftosaurSyncLogFactory(user=user, success=True, started_at=newer)
        assert last_synced_at(user) == latest.started_at

    def test_ignores_failed_syncs(self):
        user = UserFactory()
        success_at = timezone.now() - timedelta(hours=1)
        LiftosaurSyncLogFactory(user=user, success=True, started_at=success_at)
        LiftosaurSyncLogFactory(user=user, success=False, started_at=timezone.now())
        assert last_synced_at(user) == success_at

    def test_scoped_to_user(self):
        user = UserFactory()
        other = UserFactory()
        LiftosaurSyncLogFactory(user=other, success=True)
        assert last_synced_at(user) is None


def _multi_set_record(performed_date, exercise, count):
    """One record whose exercise line carries ``count`` distinct 1-rep sets.

    Weights differ per set so each one is a genuinely distinct row under the
    (user, lift, performed_at, reps, weight_kg) key — the point is to vary how
    many rows a page produces without varying anything else.
    """
    completed = ", ".join(f"1x5 {100 + i}kg" for i in range(count))
    return _history_record(performed_date, exercise, completed)


@pytest.mark.django_db
class TestPoolWriteBatching:
    """TASK-274: one sync writes the pool in one transaction per API page.

    Previously every parsed set was its own auto-committed
    ``update_or_create`` — N independent write transactions for what is
    conceptually one operation, which is what produced the
    "database is locked" 500 caught on the UAT server.
    """

    def test_query_count_is_independent_of_set_count(self):
        """The strongest statement of AC#1: pooling 30 sets costs exactly the
        same number of queries as pooling 3, and the pool is touched by one
        INSERT plus the single delta-watermark SELECT — nothing per set."""
        performed = (datetime.now(tz=UTC) - timedelta(days=5)).date().isoformat()
        counts = {}
        for n in (3, 30):
            user = UserFactory(liftosaur_api_key="key")
            client = _stub_client(
                history=[([_multi_set_record(performed, "Back Squat", n)], False, None)]
            )
            with (
                patch("liftosaur.services.LiftosaurClient", return_value=client),
                CaptureQueriesContext(connection) as queries,
            ):
                pooled = sync_user_lifts(user)

            assert pooled == n
            assert LiftHistory.objects.filter(user=user).count() == n
            counts[n] = len(queries.captured_queries)

            pool_sql = [
                q["sql"].strip()
                for q in queries.captured_queries
                if "liftosaur_lifthistory" in q["sql"]
            ]
            inserts = [s for s in pool_sql if s.upper().startswith("INSERT")]
            selects = [s for s in pool_sql if s.upper().startswith("SELECT")]
            assert len(inserts) == 1, pool_sql
            # Only the delta watermark aggregate reads the pool; the old
            # update_or_create path issued one SELECT per set here.
            assert len(selects) == 1, pool_sql
            assert "MAX" in selects[0].upper()

        assert counts[3] == counts[30]

    def test_intra_page_duplicate_sets_collapse_to_one_row(self):
        """ "3x5 100kg" expands to three identical ParsedSets. They must be
        deduplicated before the write: PostgreSQL rejects an INSERT ... ON
        CONFLICT DO UPDATE that names the same conflict target twice
        ("cannot affect row a second time"), while SQLite tolerates it — so
        dropping the dedupe would ship a Postgres-only crash. ``pooled``
        still counts parsed sets, not rows."""
        user = UserFactory(liftosaur_api_key="key")
        performed = (datetime.now(tz=UTC) - timedelta(days=5)).date().isoformat()
        client = _stub_client(
            history=[
                ([_history_record(performed, "Back Squat", "3x5 100kg")], False, None)
            ]
        )

        with patch("liftosaur.services.LiftosaurClient", return_value=client):
            pooled = sync_user_lifts(user)

        assert pooled == 3
        assert LiftHistory.objects.filter(user=user, lift="Back Squat").count() == 1

    def test_retries_transient_contention_then_succeeds(self):
        """A lost write-lock race is retried rather than surfaced: two
        failures then a real write still pools the page and logs success."""
        user = UserFactory(liftosaur_api_key="key")
        performed = (datetime.now(tz=UTC) - timedelta(days=5)).date().isoformat()
        client = _stub_client(
            history=[
                ([_history_record(performed, "Back Squat", "1x5 100kg")], False, None)
            ]
        )
        attempts = []

        def flaky(rows):
            attempts.append(rows)
            if len(attempts) <= 2:
                raise OperationalError("database is locked")
            _write_history_batch(rows)

        with (
            patch("liftosaur.services.LiftosaurClient", return_value=client),
            patch("liftosaur.services._write_history_batch", side_effect=flaky),
            patch("liftosaur.services.time.sleep") as mock_sleep,
        ):
            pooled = sync_user_lifts(user)

        assert pooled == 1
        assert LiftHistory.objects.filter(user=user, lift="Back Squat").count() == 1
        assert mock_sleep.call_args_list == [
            ((POOL_WRITE_RETRY_DELAYS[0],),),
            ((POOL_WRITE_RETRY_DELAYS[1],),),
        ]
        assert LiftosaurSyncLog.objects.get(user=user).success is True

    def test_exhausted_retries_degrade_without_raising(self, caplog):
        """Once the backoff schedule is spent the sync degrades exactly like an
        API failure — returns 0, marks the log failed, logs the cause — instead
        of letting OperationalError escape as a 500."""
        user = UserFactory(liftosaur_api_key="key")
        performed = (datetime.now(tz=UTC) - timedelta(days=5)).date().isoformat()
        client = _stub_client(
            history=[
                ([_history_record(performed, "Back Squat", "1x5 100kg")], False, None)
            ]
        )

        with (
            patch("liftosaur.services.LiftosaurClient", return_value=client),
            patch(
                "liftosaur.services._write_history_batch",
                side_effect=OperationalError("database is locked"),
            ),
            patch("liftosaur.services.time.sleep") as mock_sleep,
            caplog.at_level(logging.ERROR, logger="liftosaur.services"),
        ):
            pooled = sync_user_lifts(user)

        assert pooled == 0
        assert not LiftHistory.objects.filter(user=user).exists()
        assert mock_sleep.call_count == len(POOL_WRITE_RETRY_DELAYS)
        log = LiftosaurSyncLog.objects.get(user=user)
        assert log.success is False
        assert "DB contention" in log.error_detail
        assert "database is locked" in log.error_detail
        logged = [r.message for r in caplog.records if r.exc_info]
        assert len(logged) == 1
        assert "aborted by DB contention" in logged[0]
        assert str(user.id) in logged[0]

    def test_failure_bookkeeping_write_may_itself_be_locked(self, caplog):
        """The save that records the failure can lose the write lock too. It
        must be logged and swallowed — resurrecting the exception here would
        undo the whole point of degrading gracefully."""
        user = UserFactory(liftosaur_api_key="key")
        performed = (datetime.now(tz=UTC) - timedelta(days=5)).date().isoformat()
        client = _stub_client(
            history=[
                ([_history_record(performed, "Back Squat", "1x5 100kg")], False, None)
            ]
        )
        real_save = LiftosaurSyncLog.save

        def flaky_save(self, *args, **kwargs):
            # The initial log row (no update_fields) must still be created; only
            # the failure stamp loses the lock.
            if kwargs.get("update_fields"):
                raise OperationalError("database is locked")
            return real_save(self, *args, **kwargs)

        with (
            patch("liftosaur.services.LiftosaurClient", return_value=client),
            patch(
                "liftosaur.services._write_history_batch",
                side_effect=OperationalError("database is locked"),
            ),
            patch("liftosaur.services.time.sleep"),
            patch.object(LiftosaurSyncLog, "save", flaky_save),
            caplog.at_level(logging.ERROR, logger="liftosaur.services"),
        ):
            pooled = sync_user_lifts(user)

        assert pooled == 0
        # The log row stays success=None (in-flight) because the stamp never
        # landed; that is strictly better than a 500.
        assert LiftosaurSyncLog.objects.get(user=user).success is None
        logged = [r.message for r in caplog.records if r.exc_info]
        assert any(
            "Could not record the failed Liftosaur sync log" in m for m in logged
        )
        assert any("aborted by DB contention" in m for m in logged)
        assert all(str(user.id) in m for m in logged)

    def test_earlier_pages_persist_when_a_later_page_fails(self):
        """AC#3 — the deliberate partial-sync decision, pinned.

        The transaction is per API page, NOT per pull. Wrapping the whole
        pagination loop would hold a write transaction open across every HTTP
        round-trip, which under SQLite means holding the write lock across
        network latency and makes contention worse, not better. The tradeoff
        accepted in exchange: when a genuinely multi-page pull dies on page 2,
        page 1's rows stay committed and the run reports failure — the same
        "truncated window persists" semantics the pagination stall guard
        already documents. This test exists so a future reader sees that
        behavior was chosen, not inherited.
        """
        user = UserFactory(liftosaur_api_key="key")
        performed = (datetime.now(tz=UTC) - timedelta(days=5)).date().isoformat()
        page_one = _history_record(performed, "Back Squat", "1x5 100kg")
        client = _stub_client()
        client.get_history.side_effect = [
            ([page_one], True, "page-2"),
            LiftosaurAPIError(500, "boom"),
        ]

        with patch("liftosaur.services.LiftosaurClient", return_value=client):
            pooled = sync_user_lifts(user)

        assert pooled == 0
        assert client.get_history.call_count == 2
        assert LiftHistory.objects.filter(user=user, lift="Back Squat").count() == 1
        log = LiftosaurSyncLog.objects.get(user=user)
        assert log.success is False
        assert "boom" in log.error_detail
