"""Tests for fitnessvolt.services (TASK-104).

refresh_cache() tests use a fake client — no real FitnessVolt calls are ever
made. Lift aliases come from the session-seeded fitnessvolt_lifts fixture
(see conftest.py), the same reference data a deployed instance has.

Interpolation tests use the round-number percentile table from
factories.DEFAULT_PERCENTILES so every expected value is checkable by hand:
Intermediate/Elite land exactly on p50/p95, Novice/Advanced interpolate
between columns, Beginner extrapolates below p10 on the p10->p25 slope.
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from challenges.tests.factories import CustomGoalFactory
from fitnessvolt import services
from fitnessvolt.client import FitnessVoltAPIError
from fitnessvolt.models import FitnessVoltStandardCache
from fitnessvolt.tests.factories import (
    DEFAULT_PERCENTILES,
    FitnessVoltStandardCacheFactory,
)

pytestmark = pytest.mark.django_db

SNAPSHOT = "2026-06-09"

# A second weight class whose columns sit exactly 40 kg above the factory
# default (80 kg class), so interpolating at any bodyweight in between is
# simple proportional arithmetic.
HEAVIER_PERCENTILES = {
    column: value + 40 for column, value in DEFAULT_PERCENTILES.items()
}


class FakeClient:
    """Stands in for FitnessVoltClient during refresh_cache() tests."""

    def __init__(
        self,
        data_version="2026-06-09",
        sources=None,
        fail_on_slug=None,
    ):
        self.data_version = data_version
        self.sources = sources or {
            "verified": ["squat", "bench-press", "deadlift"],
            "gym": ["back_squat", "overhead_press"],
        }
        self.fail_on_slug = fail_on_slug
        self.lift_calls = []

    def get_capabilities(self):
        return {
            "success": True,
            "api_version": "1.0.0",
            "data_version": self.data_version,
            "sources": {
                population: {
                    "population": population,
                    "lifts": [{"lift": slug, "label": slug} for slug in slugs],
                    "sexes": ["male", "female"],
                }
                for population, slugs in self.sources.items()
            },
        }

    def get_lift_standards(self, lift_slug, population, sex):
        self.lift_calls.append((population, lift_slug, sex))
        if lift_slug == self.fail_on_slug:
            raise FitnessVoltAPIError(429, "rate limited", retry_after=60)
        return {
            "success": True,
            "api_version": "1.0.0",
            "data_version": self.data_version,
            "lift": lift_slug,
            "sex": sex,
            "unit": "kg",
            "format": "table",
            "weight_classes": [
                {
                    "weight_class": 80,
                    "weight_class_label": "80 kg",
                    "sample_size": 500,
                    "percentiles": DEFAULT_PERCENTILES,
                },
                {
                    "weight_class": 100,
                    "weight_class_label": "100 kg",
                    "sample_size": 400,
                    "percentiles": HEAVIER_PERCENTILES,
                },
            ],
        }


@pytest.fixture
def fake_client(monkeypatch):
    client = FakeClient()
    monkeypatch.setattr(services, "FitnessVoltClient", lambda: client)
    return client


class TestLiftNameMapping:
    def test_canonical_lift_name_maps_seeded_slugs(self):
        # verified slugs (hyphenated where multi-word) and gym slugs
        # (underscored) both resolve.
        assert services.canonical_lift_name("squat") == "Back Squat"
        assert services.canonical_lift_name("back_squat") == "Back Squat"
        assert services.canonical_lift_name("bench-press") == "Bench Press"
        assert services.canonical_lift_name("pullup") == "Pull-up"

    def test_canonical_lift_name_returns_none_for_unknown_slug(self):
        assert services.canonical_lift_name("zercher-yoke-carry") is None

    def test_slugs_for_lift_name_returns_all_population_variants(self):
        # The two populations slug Back Squat differently; scoring tries each.
        assert set(services.slugs_for_lift_name("Back Squat")) == {
            "squat",
            "back_squat",
        }

    def test_slugs_for_lift_name_returns_empty_for_unknown_name(self):
        assert services.slugs_for_lift_name("Zercher Yoke Carry") == []


class TestCurrentSnapshotVersion:
    def test_returns_none_when_population_never_warmed(self):
        assert services.current_snapshot_version("verified") is None

    def test_returns_latest_snapshot_by_fetch_time(self):
        FitnessVoltStandardCacheFactory(
            source_snapshot_version="2026-01-01",
            fetched_at=timezone.now() - timedelta(days=90),
        )
        FitnessVoltStandardCacheFactory(
            source_snapshot_version="2026-06-09",
            fetched_at=timezone.now(),
        )
        assert services.current_snapshot_version("verified") == "2026-06-09"

    def test_scoped_per_population(self):
        FitnessVoltStandardCacheFactory(
            population="gym", lift_slug="back_squat", source_snapshot_version=SNAPSHOT
        )
        assert services.current_snapshot_version("verified") is None


class TestStandardsMethodAvailable:
    def test_false_when_disabled_even_with_warmed_cache(self, settings):
        settings.FITNESSVOLT_ENABLED = False
        FitnessVoltStandardCacheFactory(source_snapshot_version=SNAPSHOT)
        assert services.standards_method_available() is False

    def test_false_when_enabled_but_nothing_warmed(self, settings):
        settings.FITNESSVOLT_ENABLED = True
        assert services.standards_method_available() is False

    def test_true_when_enabled_and_any_population_warmed(self, settings):
        settings.FITNESSVOLT_ENABLED = True
        FitnessVoltStandardCacheFactory(
            population="gym", source_snapshot_version=SNAPSHOT
        )
        assert services.standards_method_available() is True


class TestGetFitnessVoltThreshold:
    def _threshold(self, tier_label, bodyweight, **kwargs):
        return services.get_fitnessvolt_threshold(
            population=kwargs.pop("population", "verified"),
            snapshot_version=kwargs.pop("snapshot_version", SNAPSHOT),
            lift_slug=kwargs.pop("lift_slug", "squat"),
            sex=kwargs.pop("sex", "M"),
            tier_label=tier_label,
            bodyweight_kg=Decimal(bodyweight),
        )

    def test_intermediate_is_exact_p50_match(self):
        FitnessVoltStandardCacheFactory()
        assert self._threshold("Intermediate", "80") == Decimal("160.00")

    def test_elite_is_exact_p95_match(self):
        FitnessVoltStandardCacheFactory()
        assert self._threshold("Elite", "80") == Decimal("205.00")

    def test_novice_interpolates_between_p10_and_p25(self):
        # 20th percentile between p10=100 and p25=130:
        # 100 + 30 * (20-10)/(25-10) = 120.
        FitnessVoltStandardCacheFactory()
        assert self._threshold("Novice", "80") == Decimal("120.00")

    def test_advanced_interpolates_between_p75_and_p90(self):
        # 80th percentile between p75=175 and p90=190:
        # 175 + 15 * (80-75)/(90-75) = 180.
        FitnessVoltStandardCacheFactory()
        assert self._threshold("Advanced", "80") == Decimal("180.00")

    def test_beginner_extrapolates_below_p10_on_p10_p25_slope(self):
        # 5th percentile below p10=100, extrapolated on the p10->p25 slope:
        # 100 + 30 * (5-10)/(25-10) = 90.
        FitnessVoltStandardCacheFactory()
        assert self._threshold("Beginner", "80") == Decimal("90.00")

    def test_bodyweight_between_weight_classes_interpolates_linearly(self):
        # p50 is 160 at the 80 kg class and 200 at the 100 kg class; a 85 kg
        # lifter sits a quarter of the way up: 160 + 40 * 5/20 = 170.
        FitnessVoltStandardCacheFactory()
        FitnessVoltStandardCacheFactory(
            weight_class_kg=Decimal("100"),
            weight_class_label="100 kg",
            percentiles=HEAVIER_PERCENTILES,
        )
        assert self._threshold("Intermediate", "85") == Decimal("170.00")

    def test_bodyweight_below_lowest_class_uses_nearest_row_verbatim(self):
        FitnessVoltStandardCacheFactory()
        FitnessVoltStandardCacheFactory(
            weight_class_kg=Decimal("100"),
            weight_class_label="100 kg",
            percentiles=HEAVIER_PERCENTILES,
        )
        assert self._threshold("Intermediate", "52") == Decimal("160.00")

    def test_bodyweight_above_highest_class_uses_nearest_row_verbatim(self):
        FitnessVoltStandardCacheFactory()
        FitnessVoltStandardCacheFactory(
            weight_class_kg=Decimal("100"),
            weight_class_label="100 kg",
            percentiles=HEAVIER_PERCENTILES,
        )
        assert self._threshold("Intermediate", "140") == Decimal("200.00")

    def test_no_cached_rows_returns_none(self):
        assert self._threshold("Intermediate", "80") is None

    def test_pinned_snapshot_is_respected(self):
        FitnessVoltStandardCacheFactory()
        assert (
            self._threshold("Intermediate", "80", snapshot_version="2025-01-01") is None
        )

    def test_unknown_tier_label_returns_none(self):
        FitnessVoltStandardCacheFactory()
        assert self._threshold("World Class", "80") is None

    def test_target_above_highest_cached_column_returns_none(self):
        # Elite (95th) can't be resolved when the table stops at p90.
        FitnessVoltStandardCacheFactory(
            percentiles={"p10": 100, "p25": 130, "p50": 160, "p75": 175, "p90": 190}
        )
        assert self._threshold("Elite", "80") is None
        assert self._threshold("Intermediate", "80") == Decimal("160.00")


class TestGetStandardsBulk:
    def test_returns_five_tiers_per_covered_lift_sorted(self):
        FitnessVoltStandardCacheFactory(lift_slug="squat")
        FitnessVoltStandardCacheFactory(lift_slug="deadlift")
        cells = services.get_standards_bulk("verified", SNAPSHOT, "M", Decimal("80"))
        assert [(c["lift"], c["tier_label"]) for c in cells] == [
            ("Back Squat", "Beginner"),
            ("Back Squat", "Novice"),
            ("Back Squat", "Intermediate"),
            ("Back Squat", "Advanced"),
            ("Back Squat", "Elite"),
            ("Deadlift", "Beginner"),
            ("Deadlift", "Novice"),
            ("Deadlift", "Intermediate"),
            ("Deadlift", "Advanced"),
            ("Deadlift", "Elite"),
        ]
        intermediate = next(
            c
            for c in cells
            if c["lift"] == "Back Squat" and c["tier_label"] == "Intermediate"
        )
        # Effective multiplier: interpolated threshold / bodyweight = 160/80.
        assert intermediate["multiplier"] == Decimal("2")
        assert intermediate["lift_slug"] == "squat"

    def test_filters_by_sex_and_snapshot(self):
        FitnessVoltStandardCacheFactory(sex="F")
        FitnessVoltStandardCacheFactory(sex="M", source_snapshot_version="2025-01-01")
        assert (
            services.get_standards_bulk("verified", SNAPSHOT, "M", Decimal("80")) == []
        )

    def test_skips_unmapped_slugs(self):
        FitnessVoltStandardCacheFactory(lift_slug="mystery-lift")
        assert (
            services.get_standards_bulk("verified", SNAPSHOT, "M", Decimal("80")) == []
        )

    def test_unresolvable_tier_is_omitted_not_guessed(self):
        # Table stops at p90 -> Elite (95th) has no bracketing columns.
        FitnessVoltStandardCacheFactory(
            percentiles={"p10": 100, "p25": 130, "p50": 160, "p75": 175, "p90": 190}
        )
        cells = services.get_standards_bulk("verified", SNAPSHOT, "M", Decimal("80"))
        assert [c["tier_label"] for c in cells] == [
            "Beginner",
            "Novice",
            "Intermediate",
            "Advanced",
        ]

    def test_none_bodyweight_emits_cells_without_multipliers(self):
        FitnessVoltStandardCacheFactory()
        cells = services.get_standards_bulk("verified", SNAPSHOT, "M", None)
        assert len(cells) == 5
        assert all(c["multiplier"] is None for c in cells)


class TestCoveredLiftNames:
    def test_returns_canonical_names_for_snapshot(self):
        FitnessVoltStandardCacheFactory(lift_slug="squat")
        FitnessVoltStandardCacheFactory(lift_slug="deadlift")
        FitnessVoltStandardCacheFactory(
            lift_slug="bench-press", source_snapshot_version="2025-01-01"
        )
        assert services.covered_lift_names("verified", SNAPSHOT) == {
            "Back Squat",
            "Deadlift",
        }


class TestRefreshCache:
    def test_full_pull_inserts_both_populations(self, fake_client):
        summary = services.refresh_cache()
        assert summary == {
            "verified": "inserted:2026-06-09",
            "gym": "inserted:2026-06-09",
        }
        # (3 verified + 2 gym lifts) x 2 sexes x 2 weight classes each.
        assert FitnessVoltStandardCache.objects.count() == 20
        row = FitnessVoltStandardCache.objects.get(
            population="verified",
            lift_slug="squat",
            sex="M",
            weight_class_kg=Decimal("80"),
        )
        assert row.source_snapshot_version == "2026-06-09"
        assert row.weight_class_label == "80 kg"
        assert row.percentiles == DEFAULT_PERCENTILES
        assert row.sample_size == 500

    def test_one_call_per_lift_and_sex_with_api_sex_values(self, fake_client):
        services.refresh_cache()
        assert ("verified", "squat", "male") in fake_client.lift_calls
        assert ("verified", "squat", "female") in fake_client.lift_calls
        assert ("gym", "back_squat", "male") in fake_client.lift_calls
        # 5 lifts x 2 sexes.
        assert len(fake_client.lift_calls) == 10

    def test_second_run_with_same_data_version_is_noop(self, fake_client):
        services.refresh_cache()
        fake_client.lift_calls.clear()
        summary = services.refresh_cache()
        assert summary == {"verified": "noop", "gym": "noop"}
        assert fake_client.lift_calls == []
        assert FitnessVoltStandardCache.objects.count() == 20

    def test_new_data_version_inserts_alongside_old_snapshot(self, fake_client):
        services.refresh_cache()
        fake_client.data_version = "2026-07-01"
        services.refresh_cache()
        versions = set(
            FitnessVoltStandardCache.objects.values_list(
                "source_snapshot_version", flat=True
            )
        )
        assert versions == {"2026-06-09", "2026-07-01"}
        assert services.current_snapshot_version("verified") == "2026-07-01"

    def test_unmapped_slug_is_skipped_not_guessed(self, fake_client):
        fake_client.sources["verified"] = ["squat", "mystery-lift"]
        services.refresh_cache()
        assert not FitnessVoltStandardCache.objects.filter(
            lift_slug="mystery-lift"
        ).exists()
        assert not any(slug == "mystery-lift" for _, slug, _ in fake_client.lift_calls)

    def test_capability_fetch_failure_keeps_existing_cache(self, monkeypatch):
        existing = FitnessVoltStandardCacheFactory()

        class DownClient:
            def get_capabilities(self):
                raise FitnessVoltAPIError(503, "down")

        monkeypatch.setattr(services, "FitnessVoltClient", DownClient)
        assert services.refresh_cache() == {}
        assert FitnessVoltStandardCache.objects.get() == existing

    def test_mid_pull_failure_rolls_back_partial_snapshot(self, fake_client):
        services.refresh_cache()
        fake_client.data_version = "2026-07-01"
        fake_client.fail_on_slug = "deadlift"  # third verified lift
        summary = services.refresh_cache()
        # verified's partial new snapshot must not survive; the old snapshot
        # stays current. gym (no failing slug) still pulls the new version.
        assert "verified" not in summary
        assert not FitnessVoltStandardCache.objects.filter(
            population="verified", source_snapshot_version="2026-07-01"
        ).exists()
        assert services.current_snapshot_version("verified") == "2026-06-09"
        assert summary["gym"] == "inserted:2026-07-01"

    def test_missing_data_version_aborts(self, fake_client):
        fake_client.data_version = None
        assert services.refresh_cache() == {}
        assert FitnessVoltStandardCache.objects.count() == 0

    def test_gym_string_weight_classes_normalized_and_all_skipped(self, monkeypatch):
        # gym returns string weight classes ("76kg"), an open-ended top class
        # ("84+kg" -> numeric anchor plus epsilon so heavier bodyweights
        # clamp to it), and an "all" aggregate that is not a weight class.
        class GymStyleClient(FakeClient):
            def get_lift_standards(self, lift_slug, population, sex):
                self.lift_calls.append((population, lift_slug, sex))
                return {
                    "data_version": self.data_version,
                    "weight_classes": [
                        {
                            "weight_class": "76kg",
                            "weight_class_label": "76 kg",
                            "sample_size": 100,
                            "percentiles": DEFAULT_PERCENTILES,
                        },
                        {
                            "weight_class": "84+kg",
                            "weight_class_label": "84+ kg",
                            "sample_size": 90,
                            "percentiles": HEAVIER_PERCENTILES,
                        },
                        {
                            "weight_class": "all",
                            "weight_class_label": "All bodyweights",
                            "sample_size": 500,
                            "percentiles": DEFAULT_PERCENTILES,
                        },
                    ],
                }

        client = GymStyleClient(sources={"gym": ["back_squat"]})
        monkeypatch.setattr(services, "FitnessVoltClient", lambda: client)
        summary = services.refresh_cache()
        assert summary == {"gym": "inserted:2026-06-09"}
        classes = sorted(
            FitnessVoltStandardCache.objects.values_list("weight_class_kg", flat=True)
        )
        # Two sexes x (76kg + 84+kg); the "all" aggregate is never cached.
        assert classes == [
            Decimal("76.00"),
            Decimal("76.00"),
            Decimal("84.01"),
            Decimal("84.01"),
        ]


class TestGarbageCollection:
    def _old_snapshot(self, version="2025-01-01", population="verified"):
        return FitnessVoltStandardCacheFactory(
            population=population,
            source_snapshot_version=version,
            fetched_at=timezone.now() - timedelta(days=365),
        )

    def test_new_snapshot_pull_sweeps_old_unreferenced_snapshot(self, fake_client):
        self._old_snapshot()
        services.refresh_cache()
        assert not FitnessVoltStandardCache.objects.filter(
            source_snapshot_version="2025-01-01"
        ).exists()

    def test_referenced_snapshot_is_never_swept(self, fake_client):
        self._old_snapshot()
        CustomGoalFactory(
            source_method="standards",
            source_detail={
                "population": "verified",
                "snapshot_version": "2025-01-01",
                "tier": "Intermediate",
                "sex": "M",
                "bodyweight_kg": "80.00",
            },
        )
        services.refresh_cache()
        assert FitnessVoltStandardCache.objects.filter(
            source_snapshot_version="2025-01-01"
        ).exists()

    def test_recent_unreferenced_snapshot_survives_retention_window(self, fake_client):
        FitnessVoltStandardCacheFactory(
            source_snapshot_version="2026-05-01",
            fetched_at=timezone.now() - timedelta(days=7),
        )
        services.refresh_cache()
        assert FitnessVoltStandardCache.objects.filter(
            source_snapshot_version="2026-05-01"
        ).exists()

    def test_noop_run_does_not_sweep(self, fake_client):
        services.refresh_cache()
        self._old_snapshot()
        services.refresh_cache()  # same data_version -> noop, no sweeping
        assert FitnessVoltStandardCache.objects.filter(
            source_snapshot_version="2025-01-01"
        ).exists()
