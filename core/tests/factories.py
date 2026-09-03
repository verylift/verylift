"""Factories for core's shared lift models.

Moved out of liftosaur/tests/factories.py with the models themselves
(TASK-347); LiftosaurSyncLogFactory stays there, being genuinely Liftosaur's.
"""

import factory
from django.utils import timezone

from accounts.tests.factories import UserFactory
from core.models import Lift, LiftAlias, LiftAliasSource, LiftHistory


class LiftFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Lift

    name = factory.Sequence(lambda n: f"Lift {n}")
    is_bodyweight_added = False


class LiftAliasFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = LiftAlias

    source = LiftAliasSource.LIFTOSAUR
    from_name = factory.Sequence(lambda n: f"Liftosaur Exercise {n}")
    to_name = "Back Squat"


class LiftHistoryFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = LiftHistory

    user = factory.SubFactory(UserFactory)
    lift = "Back Squat"
    performed_at = factory.LazyFunction(lambda: timezone.now().date())
    weight_kg = factory.Faker("pydecimal", left_digits=3, right_digits=2, positive=True)
    reps = 5
    equipment = ""
    synced_at = factory.LazyFunction(timezone.now)
