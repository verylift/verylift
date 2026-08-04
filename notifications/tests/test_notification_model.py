import pytest

from notifications.models import Notification
from notifications.tests.factories import NotificationFactory


@pytest.mark.django_db
class TestNotificationModel:
    def test_has_uuid_pk(self):
        n = NotificationFactory()
        assert n.pk is not None
        assert len(str(n.pk)) == 36

    def test_is_read_defaults_false(self):
        n = NotificationFactory()
        assert n.is_read is False

    def test_challenge_nullable(self):
        n = NotificationFactory(challenge=None)
        assert n.challenge is None

    def test_set_null_on_challenge_delete(self):
        from challenges.tests.factories import ChallengeFactory

        challenge = ChallengeFactory()
        n = NotificationFactory(challenge=challenge)
        challenge.delete()
        n.refresh_from_db()
        assert n.challenge is None

    def test_each_event_type_is_valid(self):
        for event_type in Notification.EventType.values:
            n = NotificationFactory(event_type=event_type)
            assert n.event_type == event_type

    def test_str_representation(self):
        n = NotificationFactory(event_type=Notification.EventType.OVERTAKEN)
        assert "overtaken" in str(n)

    def test_metadata_defaults_to_empty_dict(self):
        n = NotificationFactory()
        assert n.metadata == {}

    def test_metadata_can_hold_payload(self):
        payload = {"overtaken_by": "alice", "from_rank": 2, "to_rank": 3}
        n = NotificationFactory(metadata=payload)
        assert n.metadata["overtaken_by"] == "alice"
