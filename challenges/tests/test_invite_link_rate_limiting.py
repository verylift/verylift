"""Tests for invite-link join rate limiting (TASK-300).

Mirrors accounts/tests/test_rate_limiting.py's approach: the project-wide
autouse fixture in the root conftest disables django-ratelimit so ordinary
suites can hit invite_link_view repeatedly, so this re-enables it and pins
django-ratelimit's fixed-window clock (TASK-210) to avoid a burst straddling
a window rollover mid-test.
"""

from unittest.mock import patch

import pytest
from django.core.cache import caches
from django.test import Client, override_settings
from django.urls import reverse

from challenges.tests.factories import ChallengeFactory, ChallengeInviteLinkFactory


@pytest.fixture
def _enable_ratelimit(settings):
    settings.RATELIMIT_ENABLE = True
    caches["ratelimit"].clear()
    with patch("django_ratelimit.core.time") as mock_ratelimit_time:
        mock_ratelimit_time.time.return_value = 1_700_000_000
        yield
    caches["ratelimit"].clear()


@pytest.mark.django_db
@pytest.mark.usefixtures("_enable_ratelimit")
class TestInviteLinkJoinThrottling:
    @override_settings(RATELIMIT_INVITE_LINK_IP="3/m")
    def test_repeated_requests_from_one_ip_are_blocked(self):
        challenge = ChallengeFactory()
        link = ChallengeInviteLinkFactory(challenge=challenge, revoked_at=None)
        client = Client()
        url = reverse("challenges:invite-link", args=[link.token])
        for _ in range(3):
            assert client.get(url).status_code == 200
        assert client.get(url).status_code == 429

    @override_settings(RATELIMIT_INVITE_LINK_IP="3/m")
    def test_guessing_many_different_tokens_is_also_capped(self):
        """The limit is per-IP, not per-token -- unknown tokens count too, since
        those are exactly what a brute-force scan looks like."""
        client = Client()
        for i in range(3):
            url = reverse("challenges:invite-link", args=[f"guess-{i}"])
            assert client.get(url).status_code == 404
        url = reverse("challenges:invite-link", args=["guess-3"])
        assert client.get(url).status_code == 429
