import factory

from accounts.tests.factories import UserFactory
from notifications.models import Notification


class NotificationFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Notification

    user = factory.SubFactory(UserFactory)
    event_type = Notification.EventType.INVITE_RECEIVED
    challenge = None
    is_read = False
    metadata = factory.LazyFunction(dict)
