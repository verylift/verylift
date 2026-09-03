"""Mirrors liftosaur/tests/test_liftosaur_sync_log_model.py for HevySyncLog.

Takes unsaved instances: __str__ reads two attributes, so writing the row and
its user to Postgres buys the test nothing.
"""

import pytest
from django.utils import timezone

from accounts.tests.factories import UserFactory
from hevy_api.models import HevySyncLog


@pytest.mark.parametrize(
    ("success", "expected"),
    [
        (None, "in-progress"),
        (True, "succeeded"),
        (False, "failed"),
    ],
)
def test_str_reports_sync_outcome(success, expected):
    """success is nullable, and None is the in-progress case rather than a
    missing key -- the admin changelist renders these while a sync is open."""
    log = HevySyncLog(
        user=UserFactory.build(), started_at=timezone.now(), success=success
    )

    assert expected in str(log)
