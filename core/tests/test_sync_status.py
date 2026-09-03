"""Tests for the shared "did the last sync work" read (TASK-337).

Parametrized over every tracker's own entry point rather than against a single
model: what matters is that all three answer this question identically, which
is the parity gap TASK-337 closed -- Liftosaur and Wger wrote error_detail but
no view ever read it, so only Hevy users were told their sync had failed.
"""

from datetime import UTC, datetime, timedelta

import pytest

from accounts.tests.factories import UserFactory
from hevy_api.services import latest_sync_failure as hevy_latest_sync_failure
from hevy_api.tests.factories import HevySyncLogFactory
from liftosaur.services import latest_sync_failure as liftosaur_latest_sync_failure
from liftosaur.tests.factories import LiftosaurSyncLogFactory
from wger.services import latest_sync_failure as wger_latest_sync_failure
from wger.tests.factories import WgerSyncLogFactory

TRACKERS = [
    pytest.param(
        liftosaur_latest_sync_failure, LiftosaurSyncLogFactory, id="liftosaur"
    ),
    pytest.param(wger_latest_sync_failure, WgerSyncLogFactory, id="wger"),
    pytest.param(hevy_latest_sync_failure, HevySyncLogFactory, id="hevy"),
]

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
EARLIER = NOW - timedelta(days=1)


@pytest.mark.django_db
@pytest.mark.parametrize(("latest_sync_failure", "log_factory"), TRACKERS)
class TestLatestSyncFailure:
    def test_none_when_no_sync_ever_ran(self, latest_sync_failure, log_factory):
        assert latest_sync_failure(UserFactory()) is None

    def test_none_when_the_most_recent_sync_succeeded(
        self, latest_sync_failure, log_factory
    ):
        """A recovered sync must stop reporting the old failure: filtering on
        success=False alone would nag forever once a user had ever failed."""
        user = UserFactory()
        log_factory(user=user, started_at=EARLIER, success=False, error_detail="boom")
        log_factory(user=user, started_at=NOW, success=True)

        assert latest_sync_failure(user) is None

    def test_none_while_the_most_recent_sync_is_still_in_progress(
        self, latest_sync_failure, log_factory
    ):
        """success=None is an open attempt, not a failure -- reporting it would
        show a scary banner for the duration of every normal sync."""
        user = UserFactory()
        log_factory(user=user, started_at=NOW, success=None)

        assert latest_sync_failure(user) is None

    def test_returns_the_log_when_the_most_recent_sync_failed(
        self, latest_sync_failure, log_factory
    ):
        user = UserFactory()
        log_factory(user=user, started_at=EARLIER, success=True)
        failed = log_factory(
            user=user, started_at=NOW, success=False, error_detail="API error 401"
        )

        result = latest_sync_failure(user)

        assert result.id == failed.id
        assert result.error_detail == "API error 401"

    def test_another_users_failure_is_not_reported(
        self, latest_sync_failure, log_factory
    ):
        user = UserFactory()
        log_factory(started_at=NOW, success=False, error_detail="someone else's")

        assert latest_sync_failure(user) is None
