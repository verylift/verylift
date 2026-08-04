import pytest
from django.utils import timezone

from liftosaur.tests.factories import LiftosaurSyncLogFactory


@pytest.mark.django_db
class TestLiftosaurSyncLogModel:
    def test_has_uuid_pk(self):
        log = LiftosaurSyncLogFactory()
        assert log.pk is not None
        assert len(str(log.pk)) == 36

    def test_in_progress_state(self):
        log = LiftosaurSyncLogFactory(success=None, completed_at=None)
        assert log.success is None
        assert log.completed_at is None

    def test_success_state(self):
        now = timezone.now()
        log = LiftosaurSyncLogFactory(
            success=True,
            completed_at=now,
            result_summary="3 new point events",
        )
        assert log.success is True
        assert log.completed_at == now
        assert log.result_summary == "3 new point events"

    def test_failure_state(self):
        now = timezone.now()
        log = LiftosaurSyncLogFactory(
            success=False,
            completed_at=now,
            error_detail="Connection timeout",
        )
        assert log.success is False
        assert log.error_detail == "Connection timeout"

    def test_str_in_progress(self):
        log = LiftosaurSyncLogFactory(success=None)
        assert "in-progress" in str(log)

    def test_str_succeeded(self):
        log = LiftosaurSyncLogFactory(success=True)
        assert "succeeded" in str(log)

    def test_str_failed(self):
        log = LiftosaurSyncLogFactory(success=False)
        assert "failed" in str(log)
