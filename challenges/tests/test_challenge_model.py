from datetime import UTC, date, datetime, timedelta

import pytest

from challenges.models import Challenge
from challenges.tests.factories import (
    ChallengeFactory,
    ChallengeParticipantFactory,
)


@pytest.mark.django_db
class TestChallengeModel:
    def test_has_uuid_pk(self):
        comp = ChallengeFactory()
        assert comp.pk is not None
        assert len(str(comp.pk)) == 36

    def test_default_status_is_draft(self):
        comp = ChallengeFactory()
        assert comp.status == Challenge.Status.DRAFT

    def test_str_returns_name(self):
        comp = ChallengeFactory(name="Winter Showdown")
        assert str(comp) == "Winter Showdown"

    def test_creator_fk_resolves(self):
        comp = ChallengeFactory()
        assert comp.creator.pk is not None

    def test_status_can_be_set_to_active(self):
        comp = ChallengeFactory(status=Challenge.Status.ACTIVE)
        assert comp.status == Challenge.Status.ACTIVE

    def test_status_can_be_set_to_completed(self):
        comp = ChallengeFactory(status=Challenge.Status.COMPLETED)
        assert comp.status == Challenge.Status.COMPLETED

    def test_default_history_window_is_from_join(self):
        comp = ChallengeFactory()
        assert comp.history_window == Challenge.HistoryWindow.FROM_JOIN

    def test_window_start_from_join_returns_joined_at(self):
        joined = datetime.now(tz=UTC) - timedelta(days=5)
        comp = ChallengeFactory(history_window=Challenge.HistoryWindow.FROM_JOIN)
        participant = ChallengeParticipantFactory(challenge=comp, joined_at=joined)
        assert comp.window_start_for(participant) == joined

    def test_window_start_from_join_none_when_not_joined(self):
        comp = ChallengeFactory(history_window=Challenge.HistoryWindow.FROM_JOIN)
        participant = ChallengeParticipantFactory(challenge=comp, joined_at=None)
        assert comp.window_start_for(participant) is None

    def test_window_start_from_start_uses_challenge_start_date(self):
        comp = ChallengeFactory(
            history_window=Challenge.HistoryWindow.FROM_START,
            start_date=date(2026, 1, 15),
        )
        participant = ChallengeParticipantFactory(
            challenge=comp, joined_at=datetime.now(tz=UTC)
        )
        assert comp.window_start_for(participant) == datetime(2026, 1, 15, tzinfo=UTC)

    def test_window_start_from_start_ignores_missing_joined_at(self):
        comp = ChallengeFactory(
            history_window=Challenge.HistoryWindow.FROM_START,
            start_date=date(2026, 1, 15),
        )
        participant = ChallengeParticipantFactory(challenge=comp, joined_at=None)
        assert comp.window_start_for(participant) == datetime(2026, 1, 15, tzinfo=UTC)
