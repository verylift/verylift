"""Unit tests for wger.client (TASK-311).

Most tests stub the generated ``wger_api_client`` endpoint functions
(``*.sync_detailed``) via unittest.mock -- no real network calls are made.
A couple of tests wire a real AuthenticatedClient to an httpx.MockTransport
to prove request construction (auth header, query params) end-to-end, since
that's exactly the kind of thing hand-mocking sync_detailed would paper over.
"""

from http import HTTPStatus
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest
from wger_api_client.models.repetition_unit import RepetitionUnit
from wger_api_client.models.routine_weight_unit import RoutineWeightUnit
from wger_api_client.models.workout_log import WorkoutLog
from wger_api_client.types import UNSET

from wger.client import ENGLISH_LANGUAGE_ID, WgerAPIError, WgerClient


def _response(status_code, parsed, content=b""):
    return SimpleNamespace(
        status_code=HTTPStatus(status_code), parsed=parsed, content=content
    )


class TestWgerAPIError:
    def test_attributes_accessible(self):
        err = WgerAPIError(401, "Unauthorized")
        assert err.status_code == 401
        assert err.body == "Unauthorized"

    def test_str_includes_status_and_body(self):
        err = WgerAPIError(403, "Forbidden")
        assert "403" in str(err)
        assert "Forbidden" in str(err)


class TestWgerClientInit:
    def test_strips_trailing_slash_from_base_url(self):
        client = WgerClient("https://example.com/", "tok")
        assert client._base_url == "https://example.com"


class TestGetWorkoutLogs:
    def _client(self):
        return WgerClient("https://example.com", "test-token")

    def test_results_and_pagination_returned(self):
        entry = WorkoutLog(exercise=42, date=UNSET, weight="100", weight_unit=1)
        parsed = SimpleNamespace(
            results=[entry],
            next_="https://example.com/api/v2/workoutlog/?limit=1&offset=1",
        )
        with patch(
            "wger.client.workoutlog_list.sync_detailed",
            return_value=_response(200, parsed),
        ):
            entries, has_more, next_offset = self._client().get_workout_logs(
                limit=1, offset=0
            )
        assert entries == [entry]
        assert has_more is True
        assert next_offset == 1

    def test_no_next_page_stops(self):
        parsed = SimpleNamespace(results=[], next_=None)
        with patch(
            "wger.client.workoutlog_list.sync_detailed",
            return_value=_response(200, parsed),
        ):
            entries, has_more, _next_offset = self._client().get_workout_logs()
        assert entries == []
        assert has_more is False

    def test_date_gte_converted_to_datetime_kwarg(self):
        parsed = SimpleNamespace(results=[], next_=None)
        with patch(
            "wger.client.workoutlog_list.sync_detailed",
            return_value=_response(200, parsed),
        ) as mock_sync:
            self._client().get_workout_logs(date_gte="2026-01-01", limit=50, offset=100)
        call_kwargs = mock_sync.call_args.kwargs
        assert call_kwargs["date_gte"].isoformat().startswith("2026-01-01")
        assert call_kwargs["limit"] == 50
        assert call_kwargs["offset"] == 100

    def test_401_raises_wger_api_error(self):
        with (
            patch(
                "wger.client.workoutlog_list.sync_detailed",
                return_value=_response(401, None, content=b'{"detail": "bad token"}'),
            ),
            pytest.raises(WgerAPIError) as exc_info,
        ):
            self._client().get_workout_logs()
        assert exc_info.value.status_code == 401
        assert "bad token" in exc_info.value.body

    def test_network_error_propagates(self):
        with (
            patch(
                "wger.client.workoutlog_list.sync_detailed",
                side_effect=httpx.ConnectError("Connection refused"),
            ),
            pytest.raises(httpx.ConnectError),
        ):
            self._client().get_workout_logs()

    def test_real_client_sends_token_header_and_date_gte(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["auth"] = request.headers.get("authorization")
            captured["url"] = str(request.url)
            body = {"count": 0, "next": None, "previous": None, "results": []}
            return httpx.Response(200, json=body)

        client = WgerClient("https://example.com", "test-token")
        client._client = client._client.set_httpx_client(
            httpx.Client(
                base_url="https://example.com",
                transport=httpx.MockTransport(handler),
                headers={"Authorization": "Token test-token"},
            )
        )
        client.get_workout_logs(date_gte="2026-01-01", limit=50, offset=100)

        assert captured["auth"] == "Token test-token"
        assert "date__gte=2026-01-01" in captured["url"]
        assert "limit=50" in captured["url"]
        assert "offset=100" in captured["url"]


class TestGetExerciseName:
    def _client(self):
        return WgerClient("https://example.com", "test-token")

    def test_english_translation_preferred(self):
        translations = [
            SimpleNamespace(language=3, name="Kniebeuge"),
            SimpleNamespace(language=ENGLISH_LANGUAGE_ID, name="Squat"),
        ]
        parsed = SimpleNamespace(translations=translations)
        with patch(
            "wger.client.exerciseinfo_retrieve.sync_detailed",
            return_value=_response(200, parsed),
        ):
            name = self._client().get_exercise_name(42)
        assert name == "Squat"

    def test_falls_back_to_first_translation_when_no_english(self):
        translations = [SimpleNamespace(language=3, name="Kniebeuge")]
        parsed = SimpleNamespace(translations=translations)
        with patch(
            "wger.client.exerciseinfo_retrieve.sync_detailed",
            return_value=_response(200, parsed),
        ):
            name = self._client().get_exercise_name(42)
        assert name == "Kniebeuge"

    def test_no_translations_returns_none(self):
        parsed = SimpleNamespace(translations=[])
        with patch(
            "wger.client.exerciseinfo_retrieve.sync_detailed",
            return_value=_response(200, parsed),
        ):
            assert self._client().get_exercise_name(42) is None

    def test_404_returns_none(self):
        with patch(
            "wger.client.exerciseinfo_retrieve.sync_detailed",
            return_value=_response(404, None, content=b"not found"),
        ):
            assert self._client().get_exercise_name(42) is None

    def test_exercise_id_passed_through(self):
        parsed = SimpleNamespace(translations=[])
        with patch(
            "wger.client.exerciseinfo_retrieve.sync_detailed",
            return_value=_response(200, parsed),
        ) as mock_sync:
            self._client().get_exercise_name(42)
        assert mock_sync.call_args.kwargs["id"] == 42


class TestGetWeightUnits:
    def _client(self):
        return WgerClient("https://example.com", "test-token")

    def test_resolves_id_to_name_map(self):
        parsed = SimpleNamespace(
            results=[
                RoutineWeightUnit(id=5, name="kg"),
                RoutineWeightUnit(id=6, name="lb"),
            ]
        )
        with patch(
            "wger.client.setting_weightunit_list.sync_detailed",
            return_value=_response(200, parsed),
        ):
            units = self._client().get_weight_units()
        assert units == {5: "kg", 6: "lb"}

    def test_non_2xx_raises(self):
        with (
            patch(
                "wger.client.setting_weightunit_list.sync_detailed",
                return_value=_response(500, None, content=b"server error"),
            ),
            pytest.raises(WgerAPIError) as exc_info,
        ):
            self._client().get_weight_units()
        assert exc_info.value.status_code == 500


class TestGetRepetitionUnits:
    def _client(self):
        return WgerClient("https://example.com", "test-token")

    def test_resolves_id_to_repetition_unit(self):
        unit = RepetitionUnit(id=9, name="Repetitions", unit_type="REPETITIONS")
        parsed = SimpleNamespace(results=[unit])
        with patch(
            "wger.client.setting_repetitionunit_list.sync_detailed",
            return_value=_response(200, parsed),
        ):
            units = self._client().get_repetition_units()
        assert units == {9: unit}
        assert units[9].unit_type == "REPETITIONS"

    def test_non_2xx_raises(self):
        with (
            patch(
                "wger.client.setting_repetitionunit_list.sync_detailed",
                return_value=_response(403, None, content=b"forbidden"),
            ),
            pytest.raises(WgerAPIError) as exc_info,
        ):
            self._client().get_repetition_units()
        assert exc_info.value.status_code == 403
