"""Unit tests for liftosaur.client (TASK-22).

All tests stub the HTTP layer via unittest.mock — no real network calls are made.
"""

import json
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from liftosaur.client import LiftosaurAPIError, LiftosaurClient

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_urlopen_response(body):
    """Return a mock that behaves like urllib.request.urlopen()'s context manager."""
    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(body).encode("utf-8")
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    return mock_resp


# ---------------------------------------------------------------------------
# LiftosaurAPIError
# ---------------------------------------------------------------------------


class TestLiftosaurAPIError:
    def test_attributes_accessible(self):
        err = LiftosaurAPIError(401, "Unauthorized")
        assert err.status_code == 401
        assert err.body == "Unauthorized"

    def test_str_includes_status_and_body(self):
        err = LiftosaurAPIError(403, "Forbidden")
        assert "403" in str(err)
        assert "Forbidden" in str(err)


# ---------------------------------------------------------------------------
# LiftosaurClient.__init__
# ---------------------------------------------------------------------------


class TestLiftosaurClientInit:
    def test_stores_api_key(self):
        client = LiftosaurClient("my-key")
        assert client._api_key == "my-key"

    def test_sets_base_url(self):
        client = LiftosaurClient("k")
        assert client._base_url == "https://www.liftosaur.com"


# ---------------------------------------------------------------------------
# get_weight_measurements
# ---------------------------------------------------------------------------


class TestGetWeightMeasurements:
    def _client(self):
        return LiftosaurClient("test-key")

    def test_list_response_returned_as_values(self):
        body = [
            {"value": "80kg", "date": "2024-01-01"},
            {"value": "79kg", "date": "2024-01-08"},
        ]
        mock_resp = _make_urlopen_response(body)
        with patch("urllib.request.urlopen", return_value=mock_resp):
            values, has_more, next_cursor = self._client().get_weight_measurements()
        assert values == body
        assert has_more is False
        assert next_cursor is None

    def test_wrapped_measurements_key(self):
        body = {
            "measurements": [{"value": "75kg", "date": "2024-02-01"}],
            "hasMore": False,
        }
        mock_resp = _make_urlopen_response(body)
        with patch("urllib.request.urlopen", return_value=mock_resp):
            values, has_more, next_cursor = self._client().get_weight_measurements()
        assert values == [{"value": "75kg", "date": "2024-02-01"}]
        assert has_more is False

    def test_paginated_response_returns_cursor(self):
        body = {
            "measurements": [{"value": "80kg", "date": "2024-01-01"}],
            "hasMore": True,
            "cursor": "abc123",
        }
        mock_resp = _make_urlopen_response(body)
        with patch("urllib.request.urlopen", return_value=mock_resp):
            values, has_more, next_cursor = self._client().get_weight_measurements()
        assert has_more is True
        assert next_cursor == "abc123"

    def test_cursor_passed_in_query_string(self):
        body = []
        mock_resp = _make_urlopen_response(body)
        with patch("urllib.request.urlopen", return_value=mock_resp) as mock_open:
            self._client().get_weight_measurements(limit=10, cursor="tok")
        url = mock_open.call_args[0][0].full_url
        assert "cursor=tok" in url
        assert "limit=10" in url

    def test_bearer_token_sent(self):
        body = []
        mock_resp = _make_urlopen_response(body)
        with patch("urllib.request.urlopen", return_value=mock_resp) as mock_open:
            LiftosaurClient("secret").get_weight_measurements()
        req = mock_open.call_args[0][0]
        assert req.get_header("Authorization") == "Bearer secret"

    def test_401_raises_liftosaur_api_error(self):
        error = urllib.error.HTTPError(
            "https://www.liftosaur.com/api/v1/measurements/weight",
            401,
            "Unauthorized",
            {},
            None,
        )
        with (
            patch("urllib.request.urlopen", side_effect=error),
            pytest.raises(LiftosaurAPIError) as exc_info,
        ):
            self._client().get_weight_measurements()
        assert exc_info.value.status_code == 401

    def test_403_raises_liftosaur_api_error(self):
        error = urllib.error.HTTPError(
            "https://www.liftosaur.com/api/v1/measurements/weight",
            403,
            "Forbidden",
            {},
            None,
        )
        with (
            patch("urllib.request.urlopen", side_effect=error),
            pytest.raises(LiftosaurAPIError) as exc_info,
        ):
            self._client().get_weight_measurements()
        assert exc_info.value.status_code == 403

    def test_network_error_propagates(self):
        error = urllib.error.URLError("Connection refused")
        with (
            patch("urllib.request.urlopen", side_effect=error),
            pytest.raises(urllib.error.URLError),
        ):
            self._client().get_weight_measurements()

    def test_oserror_propagates(self):
        with (
            patch("urllib.request.urlopen", side_effect=OSError("timeout")),
            pytest.raises(OSError),
        ):
            self._client().get_weight_measurements()

    def test_empty_list_response(self):
        body = []
        mock_resp = _make_urlopen_response(body)
        with patch("urllib.request.urlopen", return_value=mock_resp):
            values, has_more, next_cursor = self._client().get_weight_measurements()
        assert values == []
        assert has_more is False
        assert next_cursor is None

    def test_single_value_dict_wrapped(self):
        body = {"value": "70kg", "date": "2024-03-01"}
        mock_resp = _make_urlopen_response(body)
        with patch("urllib.request.urlopen", return_value=mock_resp):
            values, has_more, next_cursor = self._client().get_weight_measurements()
        assert values == [{"value": "70kg", "date": "2024-03-01"}]

    def test_data_envelope_unwrapped(self):
        # The real Liftosaur REST API wraps the payload in a top-level "data" key.
        body = {
            "data": {
                "key": "weight",
                "values": [
                    {"value": "192lb", "date": "2025-12-05T05:19:08.000Z"},
                ],
                "hasMore": True,
                "nextCursor": 1763012746300,
            }
        }
        mock_resp = _make_urlopen_response(body)
        with patch("urllib.request.urlopen", return_value=mock_resp):
            values, has_more, next_cursor = self._client().get_weight_measurements()
        assert values == [{"value": "192lb", "date": "2025-12-05T05:19:08.000Z"}]
        assert has_more is True
        assert next_cursor == 1763012746300


# ---------------------------------------------------------------------------
# get_history
# ---------------------------------------------------------------------------


class TestGetHistory:
    def _client(self):
        return LiftosaurClient("test-key")

    def test_list_response_converted_to_strings(self):
        body = [
            "# 2024-01-01\nBench Press\n3x5x100kg",
            "# 2024-01-08\nSquat\n5x5x120kg",
        ]
        mock_resp = _make_urlopen_response(body)
        with patch("urllib.request.urlopen", return_value=mock_resp):
            records, has_more, next_cursor = self._client().get_history()
        assert records == body
        assert has_more is False
        assert next_cursor is None

    def test_wrapped_history_key(self):
        body = {"history": ["# 2024-01-01\nDeadlift\n1x3x200kg"], "hasMore": False}
        mock_resp = _make_urlopen_response(body)
        with patch("urllib.request.urlopen", return_value=mock_resp):
            records, has_more, next_cursor = self._client().get_history()
        assert records == ["# 2024-01-01\nDeadlift\n1x3x200kg"]

    def test_records_key_extracts_text_field(self):
        body = {
            "records": [
                {"id": 1781805606413, "text": "2026-06-18 18:00:06 +00:00 / ..."},
                {"id": 1781805606414, "text": "2026-06-19 18:00:06 +00:00 / ..."},
            ],
            "hasMore": True,
            "nextCursor": "page2",
        }
        mock_resp = _make_urlopen_response(body)
        with patch("urllib.request.urlopen", return_value=mock_resp):
            records, has_more, next_cursor = self._client().get_history()
        assert records == [
            "2026-06-18 18:00:06 +00:00 / ...",
            "2026-06-19 18:00:06 +00:00 / ...",
        ]
        assert has_more is True
        assert next_cursor == "page2"

    def test_paginated_response_returns_cursor(self):
        body = {
            "history": ["# 2024-01-01\nBench\n3x5x100kg"],
            "hasMore": True,
            "nextCursor": "page2",
        }
        mock_resp = _make_urlopen_response(body)
        with patch("urllib.request.urlopen", return_value=mock_resp):
            records, has_more, next_cursor = self._client().get_history()
        assert has_more is True
        assert next_cursor == "page2"

    def test_date_filters_sent_in_query_string(self):
        body = []
        mock_resp = _make_urlopen_response(body)
        with patch("urllib.request.urlopen", return_value=mock_resp) as mock_open:
            self._client().get_history(start_date="2024-01-01", end_date="2024-12-31")
        url = mock_open.call_args[0][0].full_url
        assert "startDate=2024-01-01" in url
        assert "endDate=2024-12-31" in url

    def test_limit_sent_and_end_date_omitted_when_none(self):
        body = []
        mock_resp = _make_urlopen_response(body)
        with patch("urllib.request.urlopen", return_value=mock_resp) as mock_open:
            self._client().get_history(start_date="2024-01-01", limit=200)
        url = mock_open.call_args[0][0].full_url
        assert "limit=200" in url
        assert "endDate" not in url

    def test_bearer_token_sent(self):
        body = []
        mock_resp = _make_urlopen_response(body)
        with patch("urllib.request.urlopen", return_value=mock_resp) as mock_open:
            LiftosaurClient("secret").get_history()
        req = mock_open.call_args[0][0]
        assert req.get_header("Authorization") == "Bearer secret"

    def test_401_raises_liftosaur_api_error(self):
        error = urllib.error.HTTPError(
            "https://www.liftosaur.com/api/v1/history",
            401,
            "Unauthorized",
            {},
            None,
        )
        with (
            patch("urllib.request.urlopen", side_effect=error),
            pytest.raises(LiftosaurAPIError) as exc_info,
        ):
            self._client().get_history()
        assert exc_info.value.status_code == 401

    def test_network_error_propagates(self):
        error = urllib.error.URLError("Unreachable")
        with (
            patch("urllib.request.urlopen", side_effect=error),
            pytest.raises(urllib.error.URLError),
        ):
            self._client().get_history()

    def test_no_params_no_query_string(self):
        body = []
        mock_resp = _make_urlopen_response(body)
        with patch("urllib.request.urlopen", return_value=mock_resp) as mock_open:
            self._client().get_history()
        url = mock_open.call_args[0][0].full_url
        assert "?" not in url

    def test_data_envelope_unwrapped(self):
        # The real Liftosaur REST API nests records under a top-level "data" key.
        body = {
            "data": {
                "records": [
                    {"id": 1781805606413, "text": "2026-06-18 18:00:06 +00:00 / ..."},
                ],
                "hasMore": True,
                "nextCursor": "page2",
            }
        }
        mock_resp = _make_urlopen_response(body)
        with patch("urllib.request.urlopen", return_value=mock_resp):
            records, has_more, next_cursor = self._client().get_history()
        assert records == ["2026-06-18 18:00:06 +00:00 / ..."]
        assert has_more is True
        assert next_cursor == "page2"
