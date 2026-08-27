import factory

from core.models import SupportedApp, SupportedAppMode


class SupportedAppFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = SupportedApp

    name = factory.Sequence(lambda n: f"Tracker {n}")
    url = factory.LazyAttribute(lambda obj: f"https://example.com/{obj.name.lower()}")
    is_affiliate = False
    sort_order = factory.Sequence(lambda n: n)
    description = "A workout tracker."


class SupportedAppModeFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = SupportedAppMode

    supported_app = factory.SubFactory(SupportedAppFactory)
    mode = SupportedAppMode.Mode.LIVE_SYNC
