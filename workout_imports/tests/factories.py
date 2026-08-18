import factory

from workout_imports.models import HevyLiftAlias, StrongLiftAlias


class HevyLiftAliasFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = HevyLiftAlias

    from_name = factory.Sequence(lambda n: f"Hevy Exercise {n}")
    to_name = "Back Squat"


class StrongLiftAliasFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = StrongLiftAlias

    from_name = factory.Sequence(lambda n: f"Strong Exercise {n}")
    to_name = "Back Squat"
