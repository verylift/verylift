import pytest

from notifications.models import Notification
from notifications.tests.factories import NotificationFactory


@pytest.mark.django_db
class TestNotificationModel:
    def test_set_null_on_challenge_delete(self):
        from challenges.tests.factories import ChallengeFactory

        challenge = ChallengeFactory()
        n = NotificationFactory(challenge=challenge)
        challenge.delete()
        n.refresh_from_db()
        assert n.challenge is None

    def test_str_representation(self):
        n = NotificationFactory(event_type=Notification.EventType.OVERTAKEN)
        assert "overtaken" in str(n)
