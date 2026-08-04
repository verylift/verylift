"""Unit tests for fitnessvolt.client (TASK-104).

All tests stub the HTTP layer via unittest.mock — no real network calls are
made, mirroring liftosaur/tests/test_client.py.
"""

import json
import urllib.error
from email.message import Message
from unittest.mock import MagicMock, patch

import pytest

from fitnessvolt.client import FitnessVoltAPIError, FitnessVoltClient


def _make_urlopen_response(body):
    """Return a mock behaving like urllib.request.urlopen()'s context manager."""
    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(body).encode("utf-8")
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    return mock_resp


def _http_error(code, headers=None, body=b"{}"):
    hdrs = Message()
    for key, value in (headers or {}).items():
        hdrs[key] = value
    return urllib.error.HTTPError(
        url="https://fitnessvolt.com/wp-json/fvss/v1/standards",
        code=code,
        msg="err",
        hdrs=hdrs,
        fp=None,
    )


class TestFitnessVoltAPIError:
    def test_attributes_accessible(self):
        err = FitnessVoltAPIError(500, "boom")
        assert err.status_code == 500
        assert err.body == "boom"
        assert err.retry_after is None

    def test_str_includes_status_and_body(self):
        err = FitnessVoltAPIError(429, "slow down", retry_after=30)
        assert "429" in str(err)
        assert "slow down" in str(err)
        assert err.retry_after == 30


class TestGetCapabilities:
    def test_returns_parsed_capability_doc(self):
        body = {
            "success": True,
            "api_version": "1.0.0",
            "data_version": "2026-06-09",
            "sources": {
                "verified": {
                    "population": "verified_challenge",
                    "lifts": [
                        {"lift": "squat", "label": "Back Squat"},
                        {"lift": "bench-press", "label": "Bench Press"},
                    ],
                    "sexes": ["male", "female"],
                }
            },
        }
        with patch(
            "urllib.request.urlopen", return_value=_make_urlopen_response(body)
        ) as mock_urlopen:
            result = FitnessVoltClient().get_capabilities()
        assert result == body
        url = mock_urlopen.call_args[0][0].full_url
        assert url == "https://fitnessvolt.com/wp-json/fvss/v1/standards"

    def test_non_2xx_raises_fitnessvolt_api_error(self):
        with (
            patch("urllib.request.urlopen", side_effect=_http_error(500)),
            pytest.raises(FitnessVoltAPIError) as exc_info,
        ):
            FitnessVoltClient().get_capabilities()
        assert exc_info.value.status_code == 500
        assert exc_info.value.retry_after is None

    def test_429_carries_parsed_retry_after(self):
        error = _http_error(429, headers={"Retry-After": "42"})
        with (
            patch("urllib.request.urlopen", side_effect=error),
            pytest.raises(FitnessVoltAPIError) as exc_info,
        ):
            FitnessVoltClient().get_capabilities()
        assert exc_info.value.status_code == 429
        assert exc_info.value.retry_after == 42

    def test_429_with_unparseable_retry_after_is_none(self):
        error = _http_error(429, headers={"Retry-After": "later"})
        with (
            patch("urllib.request.urlopen", side_effect=error),
            pytest.raises(FitnessVoltAPIError) as exc_info,
        ):
            FitnessVoltClient().get_capabilities()
        assert exc_info.value.retry_after is None

    def test_network_error_propagates_as_urlerror(self):
        with (
            patch(
                "urllib.request.urlopen",
                side_effect=urllib.error.URLError("unreachable"),
            ),
            pytest.raises(urllib.error.URLError),
        ):
            FitnessVoltClient().get_capabilities()


class TestGetLiftStandards:
    def test_builds_lift_url_with_required_query_params(self):
        # sex and source are both required by the real API; format=table and
        # unit=kg are always requested so cached rows are kg percentile tables.
        body = {"data_version": "2026-06-09", "weight_classes": []}
        with patch(
            "urllib.request.urlopen", return_value=_make_urlopen_response(body)
        ) as mock_urlopen:
            result = FitnessVoltClient().get_lift_standards(
                "bench-press", "verified", "male"
            )
        assert result == body
        url = mock_urlopen.call_args[0][0].full_url
        assert url == (
            "https://fitnessvolt.com/wp-json/fvss/v1/standards/bench-press"
            "?source=verified&sex=male&format=table&unit=kg"
        )

    def test_requests_are_unauthenticated(self):
        body = {"weight_classes": []}
        with patch(
            "urllib.request.urlopen", return_value=_make_urlopen_response(body)
        ) as mock_urlopen:
            FitnessVoltClient().get_lift_standards("deadlift", "gym", "female")
        request = mock_urlopen.call_args[0][0]
        assert not request.has_header("Authorization")
