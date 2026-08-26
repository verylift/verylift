import factory

from core.models import LiftAlias, LiftAliasSource


class HevyLiftAliasFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = LiftAlias

    source = LiftAliasSource.HEVY
    from_name = factory.Sequence(lambda n: f"Hevy Exercise {n}")
    to_name = "Back Squat"


class StrongLiftAliasFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = LiftAlias

    source = LiftAliasSource.STRONG
    from_name = factory.Sequence(lambda n: f"Strong Exercise {n}")
    to_name = "Back Squat"
