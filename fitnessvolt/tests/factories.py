from decimal import Decimal

import factory
from django.utils import timezone

from fitnessvolt.models import FitnessVoltStandardCache

# Round-number percentile table used across tests so interpolated tier
# thresholds are easy to verify by hand (see test_services.py):
#   Intermediate (p50 exact) -> 160
#   Elite (p95 exact)        -> 205
#   Novice (20th, between p10/p25)   -> 100 + (30 * 10 / 15) = 120
#   Advanced (80th, between p75/p90) -> 175 + (15 * 5 / 15)  = 180
#   Beginner (5th, below p10, extrapolated on the p10->p25 slope)
#                                    -> 100 + (30 * -5 / 15) = 90
DEFAULT_PERCENTILES = {
    "p10": 100,
    "p25": 130,
    "p50": 160,
    "p75": 175,
    "p90": 190,
    "p95": 205,
    "p99": 250,
}


class FitnessVoltStandardCacheFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = FitnessVoltStandardCache

    population = FitnessVoltStandardCache.Population.VERIFIED
    lift_slug = "squat"
    sex = FitnessVoltStandardCache.Sex.MALE
    weight_class_kg = Decimal("80")
    weight_class_label = "80 kg"
    percentiles = factory.LazyFunction(lambda: dict(DEFAULT_PERCENTILES))
    sample_size = 1000
    source_snapshot_version = "2026-06-09"
    fetched_at = factory.LazyFunction(timezone.now)
