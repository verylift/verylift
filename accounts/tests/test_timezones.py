"""Tests for accounts.timezones (TASK-273)."""

from unittest.mock import MagicMock

import pytest

from accounts.tests.factories import UserFactory
from accounts.timezones import (
    grouped_timezones,
    is_valid_timezone,
    resolve_timezone,
    with_detect_param,
)


class TestIsValidTimezone:
    def test_known_zone_is_valid(self):
        assert is_valid_timezone("America/Toronto") is True

    def test_unknown_zone_is_invalid(self):
        assert is_valid_timezone("Nowhere/Fake") is False

    def test_empty_string_is_invalid(self):
        assert is_valid_timezone("") is False

    def test_path_traversal_shaped_input_is_invalid(self):
        assert is_valid_timezone("../../etc/passwd") is False


class TestGroupedTimezones:
    def test_regions_are_sorted(self):
        regions = [region for region, _names in grouped_timezones()]
        non_other = [r for r in regions if r != "Other"]
        assert non_other == sorted(non_other)

    def test_america_region_present(self):
        regions = dict(grouped_timezones())
        assert "America" in regions
        assert "America/Toronto" in regions["America"]

    def test_other_group_is_trailing_and_holds_single_segment_names(self):
        groups = grouped_timezones()
        assert groups[-1][0] == "Other"
        assert "UTC" in groups[-1][1]

    def test_every_zone_appears_exactly_once(self):
        from zoneinfo import available_timezones

        seen = [name for _region, names in grouped_timezones() for name in names]
        assert sorted(seen) == sorted(available_timezones())
        assert len(seen) == len(set(seen))


@pytest.mark.django_db
class TestResolveTimezone:
    def _request(self, user=None, cookies=None):
        req = MagicMock()
        if user is None:
            req.user = MagicMock(is_authenticated=False)
        else:
            req.user = user
        req.COOKIES = cookies or {}
        return req

    def test_pinned_user_timezone_wins(self):
        user = UserFactory(timezone="America/Toronto")
        request = self._request(user=user, cookies={"pp_timezone": "Asia/Tokyo"})
        assert resolve_timezone(request) == "America/Toronto"

    def test_cookie_used_when_no_pinned_timezone(self):
        user = UserFactory(timezone="")
        request = self._request(user=user, cookies={"pp_timezone": "Asia/Tokyo"})
        assert resolve_timezone(request) == "Asia/Tokyo"

    def test_anonymous_request_uses_cookie(self):
        request = self._request(user=None, cookies={"pp_timezone": "Asia/Tokyo"})
        assert resolve_timezone(request) == "Asia/Tokyo"

    def test_no_pin_no_cookie_returns_none(self):
        user = UserFactory(timezone="")
        request = self._request(user=user, cookies={})
        assert resolve_timezone(request) is None

    def test_invalid_pinned_timezone_falls_through_to_cookie(self):
        user = UserFactory(timezone="Nowhere/Fake")
        request = self._request(user=user, cookies={"pp_timezone": "Asia/Tokyo"})
        assert resolve_timezone(request) == "Asia/Tokyo"

    def test_invalid_cookie_returns_none(self):
        user = UserFactory(timezone="")
        request = self._request(user=user, cookies={"pp_timezone": "Nowhere/Fake"})
        assert resolve_timezone(request) is None

    def test_url_encoded_cookie_value_is_decoded(self):
        """Regression: request.COOKIES holds the raw Cookie-header value, not
        a URL-decoded one -- unlike request.GET/POST. The detection script
        writes the cookie via encodeURIComponent, so a real browser sends
        "America%2FEdmonton", not "America/Edmonton", on every subsequent
        request. Every other cookie test in this file sets
        cookies={"pp_timezone": "Asia/Tokyo"} directly, which never exercises
        this -- it bypasses the wire format entirely and stores the decoded
        value as if Django had already unescaped it, which it does not."""
        user = UserFactory(timezone="")
        request = self._request(
            user=user, cookies={"pp_timezone": "America%2FEdmonton"}
        )
        assert resolve_timezone(request) == "America/Edmonton"


class TestWithDetectParam:
    def test_bare_path(self):
        assert with_detect_param("/dashboard/") == "/dashboard/?tzdetect=1"

    def test_path_with_existing_query_string(self):
        result = with_detect_param("/dashboard/?a=1")
        assert result.startswith("/dashboard/?")
        assert "a=1" in result
        assert "tzdetect=1" in result

    def test_path_already_carrying_tzdetect_is_not_duplicated(self):
        result = with_detect_param("/dashboard/?tzdetect=1")
        assert result.count("tzdetect=1") == 1
