import factory
from django.utils import timezone

from accounts.tests.factories import UserFactory
from core.models import LiftAlias, LiftAliasSource
from wger.models import WgerSyncLog


class WgerLiftAliasFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = LiftAlias

    source = LiftAliasSource.WGER
    from_name = factory.Sequence(lambda n: f"Wger Exercise {n}")
    to_name = "Back Squat"


class WgerSyncLogFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = WgerSyncLog

    user = factory.SubFactory(UserFactory)
    started_at = factory.LazyFunction(timezone.now)
    completed_at = None
    success = None
    result_summary = ""
    error_detail = ""
