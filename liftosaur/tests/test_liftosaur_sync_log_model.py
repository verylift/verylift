import pytest

from liftosaur.tests.factories import LiftosaurSyncLogFactory


@pytest.mark.django_db
class TestLiftosaurSyncLogModel:
    @pytest.mark.parametrize(
        ("success", "expected"),
        [
            (None, "in-progress"),
            (True, "succeeded"),
            (False, "failed"),
        ],
    )
    def test_str_reports_sync_outcome(self, success, expected):
        log = LiftosaurSyncLogFactory(success=success)
        assert expected in str(log)
