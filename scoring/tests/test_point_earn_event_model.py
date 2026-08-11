import pytest

from scoring.models import PointEarnEvent
from scoring.tests.factories import PointEarnEventFactory


@pytest.mark.django_db
class TestPointEarnEventModel:
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
