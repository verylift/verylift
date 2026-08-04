import pytest

from scoring.models import PointEarnEvent
from scoring.tests.factories import PointEarnEventFactory


@pytest.mark.django_db
class TestPointEarnEventModel:
    def test_has_uuid_pk(self):
        event = PointEarnEventFactory()
        assert event.pk is not None
        assert len(str(event.pk)) == 36

    def test_str_current_best(self):
        event = PointEarnEventFactory(
            lift="Deadlift", points_earned=8, is_current_best=True
        )
        s = str(event)
        assert "Deadlift" in s
        assert "8" in s
        assert "best" in s

    def test_str_superseded(self):
        event = PointEarnEventFactory(is_current_best=False)
        assert "superseded" in str(event)

    def test_is_current_best_flag(self):
        event = PointEarnEventFactory(is_current_best=True)
        assert event.is_current_best is True

    def test_multiple_events_same_user_challenge_lift(self):
        from accounts.tests.factories import UserFactory
        from challenges.tests.factories import ChallengeFactory

        user = UserFactory()
        challenge = ChallengeFactory()
        PointEarnEventFactory(
            user=user,
            challenge=challenge,
            lift="Bench Press",
            is_current_best=False,
        )
        PointEarnEventFactory(
            user=user,
            challenge=challenge,
            lift="Bench Press",
            is_current_best=True,
        )
        events = PointEarnEvent.objects.filter(
            user=user, challenge=challenge, lift="Bench Press"
        )
        assert events.count() == 2
        assert events.filter(is_current_best=True).count() == 1
        assert events.filter(is_current_best=False).count() == 1

    def test_points_earned_in_valid_range(self):
        for pts in [1, 5, 10]:
            event = PointEarnEventFactory(points_earned=pts)
            assert 1 <= event.points_earned <= 10
