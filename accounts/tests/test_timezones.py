"""Tests for accounts.timezones (TASK-273)."""

from datetime import UTC, datetime
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

from accounts.tests.factories import UserFactory
from accounts.timezones import (
    grouped_timezones,
    is_valid_timezone,
    local_day,
    resolve_timezone,
    user_zoneinfo,
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


class TestUserZoneinfo:
    """The request-less ladder background code (cron jobs, the Hevy sync
    thread) uses in place of resolve_timezone's live-cookie step."""

    def test_pinned_timezone_wins_over_detected(self):
        user = UserFactory.build(
            timezone="America/Toronto", detected_timezone="Asia/Tokyo"
        )
        assert user_zoneinfo(user) == ZoneInfo("America/Toronto")

    def test_detected_used_when_nothing_pinned(self):
        user = UserFactory.build(timezone="", detected_timezone="Asia/Tokyo")
        assert user_zoneinfo(user) == ZoneInfo("Asia/Tokyo")

    def test_falls_back_to_utc_when_neither_is_set(self):
        user = UserFactory.build(timezone="", detected_timezone="")
        assert user_zoneinfo(user) == ZoneInfo("UTC")

    def test_invalid_pin_falls_through_to_detected(self):
        """A stale pin (a zone dropped from the tz database) must not raise
        ZoneInfoNotFoundError deep inside a background sync."""
        user = UserFactory.build(timezone="Not/AZone", detected_timezone="Asia/Tokyo")
        assert user_zoneinfo(user) == ZoneInfo("Asia/Tokyo")


class TestLocalDay:
    """The one conversion point from an external source's timestamp to the
    plain performed_at DateField every scoring/display path reads."""

    @pytest.mark.parametrize(
        "moment,tz_name,expected",
        [
            # 19:00 on the 1st in Toronto (UTC-4) is 23:00 UTC the same day.
            (datetime(2024, 6, 1, 23, 0, tzinfo=UTC), "America/Toronto", "2024-06-01"),
            # 22:00 on the 1st in Toronto has already rolled over in UTC.
            (datetime(2024, 6, 2, 2, 0, tzinfo=UTC), "America/Toronto", "2024-06-01"),
            # 08:00 on the 2nd in Tokyo (UTC+9) is still the 1st in UTC.
            (datetime(2024, 6, 1, 23, 0, tzinfo=UTC), "Asia/Tokyo", "2024-06-02"),
        ],
    )
    def test_aware_timestamp_is_converted(self, moment, tz_name, expected):
        assert local_day(moment, ZoneInfo(tz_name)).isoformat() == expected

    def test_naive_timestamp_is_taken_at_face_value(self):
        """A naive source timestamp is already the lifter's wall clock --
        converting it would reinterpret it as server-local and shift the day.
        """
        moment = datetime(2024, 6, 1, 22, 0)
        assert local_day(moment, ZoneInfo("Asia/Tokyo")).isoformat() == "2024-06-01"
