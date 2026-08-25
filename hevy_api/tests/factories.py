import factory
from django.utils import timezone

from accounts.tests.factories import UserFactory
from hevy_api.models import HevySyncLog


class HevySyncLogFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = HevySyncLog

    user = factory.SubFactory(UserFactory)
    started_at = factory.LazyFunction(timezone.now)
    completed_at = None
    success = None
    result_summary = ""
    error_detail = ""
