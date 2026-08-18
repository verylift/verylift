"""Unit tests for wger.client (TASK-311).

All tests stub the HTTP layer via unittest.mock -- no real network calls are made.
"""

import json
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from wger.client import WgerAPIError, WgerClient


def _make_response(body):
    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.read.return_value = json.dumps(body).encode("utf-8")
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    return mock_resp


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

    def test_stores_token(self):
        client = WgerClient("https://example.com", "tok")
        assert client._api_token == "tok"


class TestGetWorkoutLogs:
    def _client(self):
        return WgerClient("https://example.com", "test-token")

    def test_results_and_pagination_returned(self):
        body = {
            "count": 2,
            "next": "https://example.com/api/v2/workoutlog/?limit=1&offset=1",
            "previous": None,
            "results": [{"id": 1, "exercise": 42, "date": "2026-01-01"}],
        }
        with patch("urllib.request.urlopen", return_value=_make_response(body)):
            entries, has_more, next_offset = self._client().get_workout_logs(
                limit=1, offset=0
            )
        assert entries == body["results"]
        assert has_more is True
        assert next_offset == 1

    def test_no_next_page_stops(self):
        body = {"count": 1, "next": None, "previous": None, "results": []}
        with patch("urllib.request.urlopen", return_value=_make_response(body)):
            entries, has_more, _next_offset = self._client().get_workout_logs()
        assert entries == []
        assert has_more is False

    def test_token_header_sent(self):
        body = {"count": 0, "next": None, "previous": None, "results": []}
        with patch(
            "urllib.request.urlopen", return_value=_make_response(body)
        ) as mock_open:
            self._client().get_workout_logs()
        req = mock_open.call_args[0][0]
        assert req.get_header("Authorization") == "Token test-token"

    def test_date_gte_and_pagination_sent_in_query_string(self):
        body = {"count": 0, "next": None, "previous": None, "results": []}
        with patch(
            "urllib.request.urlopen", return_value=_make_response(body)
        ) as mock_open:
            self._client().get_workout_logs(date_gte="2026-01-01", limit=50, offset=100)
        url = mock_open.call_args[0][0].full_url
        assert "date__gte=2026-01-01" in url
        assert "limit=50" in url
        assert "offset=100" in url

    def test_401_raises_wger_api_error(self):
        error = urllib.error.HTTPError(
            "https://example.com/api/v2/workoutlog/", 401, "Unauthorized", {}, None
        )
        with (
            patch("urllib.request.urlopen", side_effect=error),
            pytest.raises(WgerAPIError) as exc_info,
        ):
            self._client().get_workout_logs()
        assert exc_info.value.status_code == 401

    def test_network_error_propagates(self):
        error = urllib.error.URLError("Connection refused")
        with (
            patch("urllib.request.urlopen", side_effect=error),
            pytest.raises(urllib.error.URLError),
        ):
            self._client().get_workout_logs()

    def test_non_dict_response_returns_empty(self):
        with patch("urllib.request.urlopen", return_value=_make_response([1, 2, 3])):
            entries, has_more, _next_offset = self._client().get_workout_logs()
        assert entries == []
        assert has_more is False


class TestGetExerciseName:
    def _client(self):
        return WgerClient("https://example.com", "test-token")

    def test_english_translation_preferred(self):
        body = {
            "translations": [
                {"language": 3, "name": "Kniebeuge"},
                {"language": 2, "name": "Squat"},
            ]
        }
        with patch("urllib.request.urlopen", return_value=_make_response(body)):
            name = self._client().get_exercise_name(42)
        assert name == "Squat"

    def test_falls_back_to_first_translation_when_no_english(self):
        body = {"translations": [{"language": 3, "name": "Kniebeuge"}]}
        with patch("urllib.request.urlopen", return_value=_make_response(body)):
            name = self._client().get_exercise_name(42)
        assert name == "Kniebeuge"

    def test_no_translations_returns_none(self):
        body = {"translations": []}
        with patch("urllib.request.urlopen", return_value=_make_response(body)):
            assert self._client().get_exercise_name(42) is None

    def test_404_returns_none(self):
        error = urllib.error.HTTPError(
            "https://example.com/api/v2/exerciseinfo/42/", 404, "Not Found", {}, None
        )
        with patch("urllib.request.urlopen", side_effect=error):
            assert self._client().get_exercise_name(42) is None

    def test_language_param_sent(self):
        body = {"translations": [{"language": 2, "name": "Squat"}]}
        with patch(
            "urllib.request.urlopen", return_value=_make_response(body)
        ) as mock_open:
            self._client().get_exercise_name(42)
        url = mock_open.call_args[0][0].full_url
        assert "exerciseinfo/42/" in url
        assert "language=2" in url
