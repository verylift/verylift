"""Unit tests for hevy_api.client (TASK-312).

All tests stub the HTTP layer via unittest.mock -- no real network calls are
made (project convention: external HTTP calls must be mocked in tests).
"""

import json
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from hevy_api.client import HevyAPIError, HevyClient


def _make_urlopen_response(body):
    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(body).encode("utf-8")
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    return mock_resp


class TestHevyAPIError:
    def test_str_includes_status_and_body(self):
        err = HevyAPIError(401, "Unauthorized")
        assert err.status_code == 401
        assert err.body == "Unauthorized"
        assert "401" in str(err)
        assert "Unauthorized" in str(err)


class TestHevyClientInit:
    def test_sets_base_url_from_settings(self, settings):
        settings.HEVY_API_BASE = "https://api.hevyapp.com"
        client = HevyClient("my-key")
        assert client._api_key == "my-key"
        assert client._base_url == "https://api.hevyapp.com"


class TestGetWorkouts:
    def test_sends_api_key_header_and_pagination_params(self):
        body = {"page": 1, "page_count": 3, "workouts": [{"id": "w1"}]}
        mock_resp = _make_urlopen_response(body)
        captured_request = {}

        def fake_urlopen(req, timeout):
            captured_request["req"] = req
            return mock_resp

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            result = HevyClient("test-key").get_workouts(page=2, page_size=5)

        assert result == body
        sent_req = captured_request["req"]
        assert sent_req.get_header("Api-key") == "test-key"
        assert "page=2" in sent_req.full_url
        assert "pageSize=5" in sent_req.full_url

    def test_non_2xx_response_raises_hevy_api_error(self):
        http_error = urllib.error.HTTPError(
            url="https://api.hevyapp.com/v1/workouts",
            code=403,
            msg="Forbidden",
            hdrs=None,
            fp=MagicMock(read=lambda: b'{"error": "Pro subscription required"}'),
        )
        with (
            patch("urllib.request.urlopen", side_effect=http_error),
            pytest.raises(HevyAPIError) as exc_info,
        ):
            HevyClient("test-key").get_workouts()
        assert exc_info.value.status_code == 403
        assert "Pro subscription required" in exc_info.value.body


class TestGetWorkoutEvents:
    def test_sends_since_param(self):
        body = {"page": 1, "page_count": 1, "events": []}
        mock_resp = _make_urlopen_response(body)
        captured_request = {}

        def fake_urlopen(req, timeout):
            captured_request["req"] = req
            return mock_resp

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            result = HevyClient("test-key").get_workout_events(
                since="2024-01-01T00:00:00Z"
            )

        assert result == body
        assert "since=2024-01-01" in captured_request["req"].full_url

    def test_network_error_propagates(self):
        with (
            patch(
                "urllib.request.urlopen",
                side_effect=urllib.error.URLError("connection refused"),
            ),
            pytest.raises(urllib.error.URLError),
        ):
            HevyClient("test-key").get_workout_events(since="1970-01-01T00:00:00Z")
