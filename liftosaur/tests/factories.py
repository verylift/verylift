import factory
from django.utils import timezone

from accounts.tests.factories import UserFactory
from liftosaur.models import LiftosaurSyncLog


class LiftosaurSyncLogFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = LiftosaurSyncLog

    user = factory.SubFactory(UserFactory)
    started_at = factory.LazyFunction(timezone.now)
    completed_at = None
    success = None
    result_summary = ""
    error_detail = ""
