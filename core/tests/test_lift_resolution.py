"""Tests for the tracker-agnostic lift-name resolution chain.

resolve_lift_name/LiftNameMaps are pure (no DB), so most of this suite builds
maps directly rather than going through build_lift_alias_maps -- exercising
the actual six-stage matching logic, safety guards, and status codes without
paying for a database round trip.
"""

import logging

import pytest

from core.lift_resolution import (
    LiftNameMaps,
    LiftNameResolver,
    normalize_lift_name,
    normalize_lift_name_strict,
    resolve_lift_name,
)

CANONICAL_NAMES = [
    "Back Squat",
    "Pendlay Row",
    "Chin-up",
    "Pull-up",
    "Cable Crunch",
    "Bench Press",
]


def _maps(alias_pairs=()):
    return LiftNameMaps.build(alias_pairs, CANONICAL_NAMES)


class TestResolveLiftNameStages:
    def test_stage1_explicit_alias_wins_case_insensitively(self):
        maps = _maps([("Barbell Row", "Pendlay Row")])
        assert resolve_lift_name("barbell row", maps) == ("Pendlay Row", "matched")

    def test_stage1_alias_wins_even_when_stage2_would_also_match(self):
        # An explicit human-confirmed alias always wins over the algorithmic
        # barbell-stripping stage, even when both would resolve the same
        # raw name.
        maps = _maps([("Pendlay Row (Barbell)", "Bent Over Row")])
        assert resolve_lift_name("Pendlay Row (Barbell)", maps) == (
            "Bent Over Row",
            "matched",
        )

    def test_stage2_strips_trailing_barbell_qualifier(self):
        maps = _maps()
        assert resolve_lift_name("Pendlay Row (Barbell)", maps) == (
            "Pendlay Row",
            "matched",
        )

    def test_stage2_does_not_strip_other_equipment_qualifiers(self):
        # The highest-value safety guard: a dumbbell variant must not
        # collapse onto the bare (implicitly-barbell) canonical name.
        maps = _maps()
        assert resolve_lift_name("Bench Press (Dumbbell)", maps) == (
            "Bench Press (Dumbbell)",
            "unmapped",
        )

    def test_stage3_punctuation_and_case_insensitive_match(self):
        maps = _maps()
        assert resolve_lift_name("chin up", maps) == ("Chin-up", "matched")
        assert resolve_lift_name("PULL-UP", maps) == ("Pull-up", "matched")

    def test_stage4_reorders_qualifier_to_match_a_prefixed_canonical_name(self):
        maps = _maps()
        assert resolve_lift_name("Crunch (Cable)", maps) == (
            "Cable Crunch",
            "reordered",
        )

    def test_stage4_reorder_guard_does_not_collapse_onto_bare_name(self):
        # Reordering "Bench Press (Dumbbell)" -> "Dumbbell Bench Press" must
        # not match anything, since no canonical name contains "dumbbell" --
        # unlike a blind strip, reordering is information-preserving.
        maps = _maps()
        assert resolve_lift_name("Bench Press (Dumbbell)", maps) == (
            "Bench Press (Dumbbell)",
            "unmapped",
        )

    def test_stage5_separator_free_catch_all(self):
        maps = _maps()
        assert resolve_lift_name("Chinup", maps) == ("Chin-up", "fuzzy")
        assert resolve_lift_name("PullUp", maps) == ("Pull-up", "fuzzy")

    def test_stage5_matches_against_alias_keys_too(self):
        maps = _maps([("T Bar Row", "Bent Over Row")])
        assert resolve_lift_name("TBarRow", maps) == ("Bent Over Row", "fuzzy")

    def test_unmapped_name_passes_through_verbatim(self):
        maps = _maps()
        assert resolve_lift_name("Some Wholly Unknown Exercise", maps) == (
            "Some Wholly Unknown Exercise",
            "unmapped",
        )


class TestNormalizeHelpers:
    def test_normalize_lift_name_collapses_separators_but_keeps_parens_content(self):
        assert normalize_lift_name("Chin-up") == normalize_lift_name("Chin Up")
        assert normalize_lift_name("Bench Press (Dumbbell)") != normalize_lift_name(
            "Bench Press"
        )

    def test_normalize_lift_name_strict_removes_separators_entirely(self):
        assert normalize_lift_name_strict("Chin-up") == normalize_lift_name_strict(
            "Chinup"
        )


class TestLiftNameResolverDedupeAndLogging:
    """LiftNameResolver wraps resolve_lift_name with per-instance dedupe and
    source-labeled warning logs -- what every CSV importer and live-sync
    service now shares instead of reimplementing.
    """

    def _resolver(self, maps=None, source_label="Test Source", logger=None):
        return LiftNameResolver(
            maps or _maps(),
            source_label=source_label,
            logger=logger or logging.getLogger("core.tests.fake_caller"),
        )

    def test_resolve_returns_the_resolved_lift_name(self):
        resolver = self._resolver()
        assert resolver.resolve("Pendlay Row (Barbell)") == "Pendlay Row"

    def test_source_label_appears_in_the_unmapped_warning(self, caplog):
        logger = logging.getLogger("core.tests.fake_caller")
        resolver = self._resolver(source_label="Acme Tracker import", logger=logger)
        with caplog.at_level(logging.WARNING, logger="core.tests.fake_caller"):
            resolver.resolve("Some Unknown Exercise")

        assert len(caplog.records) == 1
        assert "Acme Tracker import" in caplog.records[0].message
        assert "Some Unknown Exercise" in caplog.records[0].message

    def test_source_label_appears_in_the_reordered_warning(self, caplog):
        logger = logging.getLogger("core.tests.fake_caller")
        resolver = self._resolver(source_label="Acme Tracker import", logger=logger)
        with caplog.at_level(logging.WARNING, logger="core.tests.fake_caller"):
            resolver.resolve("Crunch (Cable)")

        assert len(caplog.records) == 1
        assert "Acme Tracker import" in caplog.records[0].message
        assert "reordering its equipment qualifier" in caplog.records[0].message

    def test_source_label_appears_in_the_fuzzy_warning(self, caplog):
        logger = logging.getLogger("core.tests.fake_caller")
        resolver = self._resolver(source_label="Acme Tracker import", logger=logger)
        with caplog.at_level(logging.WARNING, logger="core.tests.fake_caller"):
            resolver.resolve("Chinup")

        assert len(caplog.records) == 1
        assert "Acme Tracker import" in caplog.records[0].message
        assert "separator-insensitive fallback" in caplog.records[0].message

    def test_matched_stages_never_warn(self, caplog):
        logger = logging.getLogger("core.tests.fake_caller")
        resolver = self._resolver(logger=logger)
        with caplog.at_level(logging.WARNING, logger="core.tests.fake_caller"):
            resolver.resolve("Chin Up")
        assert caplog.records == []

    def test_warning_is_deduped_per_distinct_raw_name_and_status(self, caplog):
        logger = logging.getLogger("core.tests.fake_caller")
        resolver = self._resolver(logger=logger)
        with caplog.at_level(logging.WARNING, logger="core.tests.fake_caller"):
            resolver.resolve("Chinup")
            resolver.resolve("Chinup")
            resolver.resolve("PullUp")
        assert len(caplog.records) == 2

    def test_reordered_fuzzy_unmapped_dedupe_sets_are_independent(self, caplog):
        # A raw name that happens to hit different stages across resolvers
        # (impossible for the *same* name here, but the dedupe sets
        # themselves must not share state) each get their own warning.
        logger = logging.getLogger("core.tests.fake_caller")
        resolver = self._resolver(logger=logger)
        with caplog.at_level(logging.WARNING, logger="core.tests.fake_caller"):
            resolver.resolve("Crunch (Cable)")  # reordered
            resolver.resolve("Chinup")  # fuzzy
            resolver.resolve("Some Unknown Exercise")  # unmapped
        assert len(caplog.records) == 3


@pytest.mark.django_db
class TestBuildLiftAliasMaps:
    def test_builds_maps_scoped_to_the_requested_source_only(self):
        from core.lift_resolution import build_lift_alias_maps
        from core.models import LiftAlias, LiftAliasSource

        LiftAlias.objects.create(
            source=LiftAliasSource.HEVY, from_name="Foo", to_name="Bar"
        )
        LiftAlias.objects.create(
            source=LiftAliasSource.STRONG, from_name="Foo", to_name="Baz"
        )

        maps = build_lift_alias_maps(LiftAliasSource.HEVY, ["Bar"])

        assert maps.alias == {"foo": "Bar"}
