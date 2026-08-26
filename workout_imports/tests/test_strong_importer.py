import io
from decimal import Decimal
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from workout_imports.importers.strong import (
    StrongImporter,
    _parse_date,
    _parse_decimal,
    _parse_reps,
)
from workout_imports.tests.factories import StrongLiftAliasFactory

REAL_EXPORT_PATH = Path(__file__).parent / "fixtures" / "strong_sample_export.csv"

HEADER = (
    "Date,Workout Name,Duration,Exercise Name,Set Order,Weight,Reps,"
    "Distance,Seconds,Notes,Workout Notes,RPE"
)


def csv_file(rows: str) -> io.BytesIO:
    content = HEADER + "\n" + rows
    return io.BytesIO(content.encode("utf-8"))


@pytest.mark.django_db
class TestStrongImporterParse:
    def test_normal_set_is_parsed(self):
        rows = "2024-01-01 09:15:00,Leg day,1h,Squat (Barbell),1,225,5,0,0,,,\n"
        parsed = StrongImporter().parse(csv_file(rows))
        assert len(parsed) == 1
        assert parsed[0].reps == 5

    def test_weight_converted_from_lb_to_kg(self):
        rows = "2024-01-01 09:15:00,Leg day,1h,Squat (Barbell),1,220,5,0,0,,,\n"
        parsed = StrongImporter().parse(csv_file(rows))
        # 220 lb * 0.45359237 = 99.7903214 -> quantized to 2dp
        assert parsed[0].weight_kg == Decimal("99.79")

    def test_repeated_set_order_for_drop_sets_is_not_deduplicated(self):
        # Strong repeats Set Order for a drop-set chain rather than
        # incrementing it -- every row must still be counted.
        rows = (
            "2024-01-01 09:15:00,Leg day,1h,Squat (Barbell),1,225,5,0,0,,,\n"
            "2024-01-01 09:15:00,Leg day,1h,Squat (Barbell),1,185,8,0,0,,,\n"
        )
        parsed = StrongImporter().parse(csv_file(rows))
        assert len(parsed) == 2

    def test_cardio_row_with_distance_and_seconds_is_skipped_not_fatal(self):
        rows = (
            "2024-01-01 09:15:00,Cardio,30m,Running,1,,,5000,1800,,,\n"
            "2024-01-01 09:20:00,Leg day,1h,Squat (Barbell),1,225,5,0,0,,,\n"
        )
        parsed = StrongImporter().parse(csv_file(rows))
        assert len(parsed) == 1

    def test_blank_rest_day_row_is_skipped_not_fatal(self):
        rows = (
            "2024-01-01 09:15:00,Rest,,,,,,,,,,\n"
            "2024-01-01 09:20:00,Leg day,1h,Squat (Barbell),1,225,5,0,0,,,\n"
        )
        parsed = StrongImporter().parse(csv_file(rows))
        assert len(parsed) == 1

    def test_explicit_zero_weight_and_reps_row_is_skipped(self):
        # A cardio row can carry explicit "0" in Weight/Reps rather than
        # leaving them blank (observed in a real export, e.g. Swimming
        # logged as Weight=0, Reps=0, Distance=1.0) -- zero reps is never a
        # completed set regardless of whether the cell was blank or "0".
        rows = "2024-01-01 09:15:00,Swim day,30m,Swimming,1,0,0,1.0,1800,,,\n"
        parsed = StrongImporter().parse(csv_file(rows))
        assert parsed == []

    def test_zero_weight_with_real_reps_is_still_scored(self):
        # A bodyweight-only set (e.g. Push Up) legitimately reports zero
        # added weight -- only reps being zero means "not a completed set".
        rows = "2024-01-01 09:15:00,Leg day,1h,Push Up,1,0,10,0,0,,,\n"
        parsed = StrongImporter().parse(csv_file(rows))
        assert len(parsed) == 1
        assert parsed[0].weight_kg == Decimal("0.00")
        assert parsed[0].reps == 10

    def test_non_numeric_reps_row_is_skipped_not_fatal(self):
        rows = "2024-01-01 09:15:00,Leg day,1h,Squat (Barbell),1,225,,0,0,,,\n"
        parsed = StrongImporter().parse(csv_file(rows))
        assert parsed == []

    def test_unparseable_date_row_is_skipped_not_fatal(self):
        rows = "not-a-date,Leg day,1h,Squat (Barbell),1,225,5,0,0,,,\n"
        parsed = StrongImporter().parse(csv_file(rows))
        assert parsed == []

    def test_blank_exercise_name_is_skipped(self):
        rows = "2024-01-01 09:15:00,Leg day,1h,,1,225,5,0,0,,,\n"
        parsed = StrongImporter().parse(csv_file(rows))
        assert parsed == []

    def test_mapped_alias_resolves_to_canonical_name(self):
        StrongLiftAliasFactory(from_name="Squat (Barbell)", to_name="Back Squat")
        rows = "2024-01-01 09:15:00,Leg day,1h,Squat (Barbell),1,225,5,0,0,,,\n"
        parsed = StrongImporter().parse(csv_file(rows))
        assert parsed[0].lift == "Back Squat"

    def test_unmapped_exercise_passes_through_unchanged(self):
        rows = "2024-01-01 09:15:00,Leg day,1h,Some Unmapped Exercise,1,225,5,0,0,,,\n"
        parsed = StrongImporter().parse(csv_file(rows))
        assert parsed[0].lift == "Some Unmapped Exercise"


@pytest.mark.django_db
class TestStrongImporterLiftResolutionChain:
    """The multi-stage resolution chain that replaced the bare alias lookup.

    Regression coverage for the real UAT bug: a "Pendlay Row (Barbell)"
    LiftHistory row that silently never pooled with the canonical
    "Pendlay Row" lift because the raw name (equipment suffix and all) was
    imported verbatim.
    """

    def test_barbell_qualifier_is_stripped_and_matched_without_an_alias(self):
        # "Pendlay Row" is a seeded canonical lift but nothing here seeds a
        # StrongLiftAlias for it -- resolution has to come purely from
        # stripping "(Barbell)" and matching the bare canonical catalogue.
        rows = "2024-01-01 09:15:00,Back day,1h,Pendlay Row (Barbell),1,135,5,0,0,,,\n"
        parsed = StrongImporter().parse(csv_file(rows))
        assert parsed[0].lift == "Pendlay Row"

    def test_dumbbell_qualifier_does_not_collapse_onto_barbell_canonical_name(self):
        # The highest-value guard here: a dumbbell variant must NOT resolve
        # to the bare "Bench Press" canonical name (which implicitly means
        # barbell), or dumbbell pressing would silently inflate barbell
        # bench press standings. No canonical "Bench Press (Dumbbell)"
        # variant exists in the seeded catalogue, so this must stay
        # unmapped/verbatim rather than guess.
        rows = "2024-01-01 09:15:00,Push day,1h,Bench Press (Dumbbell),1,50,10,0,0,,,\n"
        parsed = StrongImporter().parse(csv_file(rows))
        assert parsed[0].lift == "Bench Press (Dumbbell)"

    def test_machine_qualifier_does_not_collapse_onto_bare_canonical_name(self):
        rows = (
            "2024-01-01 09:15:00,Push day,1h,Overhead Press (Machine),1,50,10,0,0,,,\n"
        )
        parsed = StrongImporter().parse(csv_file(rows))
        assert parsed[0].lift == "Overhead Press (Machine)"

    @pytest.mark.parametrize(
        "raw_name,expected",
        [
            ("Chin Up", "Chin-up"),
            ("chin up", "Chin-up"),
            ("Pull Up", "Pull-up"),
            ("PULL-UP", "Pull-up"),
        ],
    )
    def test_case_and_punctuation_insensitive_match_against_canonical_catalogue(
        self, raw_name, expected
    ):
        rows = f"2024-01-01 09:15:00,Back day,1h,{raw_name},1,0,5,0,0,,,\n"
        parsed = StrongImporter().parse(csv_file(rows))
        assert parsed[0].lift == expected

    def test_explicit_alias_wins_over_algorithmic_barbell_stripping(self):
        # An explicit alias is a human-confirmed mapping and must take
        # priority even when the algorithmic chain would resolve the same
        # raw name to something else.
        StrongLiftAliasFactory(
            from_name="Pendlay Row (Barbell)", to_name="Bent Over Row"
        )
        rows = "2024-01-01 09:15:00,Back day,1h,Pendlay Row (Barbell),1,135,5,0,0,,,\n"
        parsed = StrongImporter().parse(csv_file(rows))
        assert parsed[0].lift == "Bent Over Row"

    def test_unmapped_exercise_still_imports_its_rows(self):
        rows = (
            "2024-01-01 09:15:00,Leg day,1h,Some Wholly Unknown Machine,1,90,8,0,0,,,\n"
        )
        parsed = StrongImporter().parse(csv_file(rows))
        assert len(parsed) == 1
        assert parsed[0].lift == "Some Wholly Unknown Machine"

    def test_unmapped_exercise_emits_one_warning_per_distinct_name(self, caplog):
        rows = (
            "2024-01-01 09:15:00,Leg day,1h,Some Wholly Unknown Machine,1,90,8,0,0,,,\n"
            "2024-01-01 09:16:00,Leg day,1h,Some Wholly Unknown Machine,2,95,6,0,0,,,\n"
            "2024-01-01 09:17:00,Leg day,1h,Another Unknown Exercise,1,45,10,0,0,,,\n"
        )
        with caplog.at_level("WARNING", logger="workout_imports.importers.strong"):
            StrongImporter().parse(csv_file(rows))

        unmapped_warnings = [
            r for r in caplog.records if "did not match any known" in r.message
        ]
        assert len(unmapped_warnings) == 2
        messages = {r.message for r in unmapped_warnings}
        assert any("Some Wholly Unknown Machine" in m for m in messages)
        assert any("Another Unknown Exercise" in m for m in messages)

    def test_mapped_exercise_does_not_emit_a_warning(self, caplog):
        rows = "2024-01-01 09:15:00,Back day,1h,Pendlay Row (Barbell),1,135,5,0,0,,,\n"
        with caplog.at_level("WARNING", logger="workout_imports.importers.strong"):
            StrongImporter().parse(csv_file(rows))
        assert not [r for r in caplog.records if "did not match any known" in r.message]


def _fuzzy_warnings(caplog):
    return [r for r in caplog.records if "separator-insensitive fallback" in r.message]


def _reordered_warnings(caplog):
    return [
        r for r in caplog.records if "reordering its equipment qualifier" in r.message
    ]


def _unmapped_warnings(caplog):
    return [r for r in caplog.records if "did not match any known" in r.message]


@pytest.mark.django_db
class TestStrongImporterFuzzyFallback:
    """Stage 5: the separator-free catch-all that runs after stages 1-4 miss.

    Each of these raw names only resolves once every non-alphanumeric
    character is stripped entirely (not just collapsed to a space), so
    stages 1-4 all miss and only stage 5 catches them -- and a stage-5 hit
    must be logged distinctly, since it's a "loose match stood in" signal
    the user wants visible in development.
    """

    @pytest.mark.parametrize(
        "raw_name,expected",
        [
            ("Chinup", "Chin-up"),
            ("PullUp", "Pull-up"),
            ("Pushup", "Push Up"),
            ("SitUp", "Sit Up"),
            ("TBar Row", "T Bar Row"),
            ("Handstand Pushup", "Handstand Push Up"),
        ],
    )
    def test_separator_free_name_resolves_via_stage_four(self, raw_name, expected):
        rows = f"2024-01-01 09:15:00,Back day,1h,{raw_name},1,0,5,0,0,,,\n"
        parsed = StrongImporter().parse(csv_file(rows))
        assert parsed[0].lift == expected

    @pytest.mark.parametrize(
        "raw_name,expected",
        [
            ("Chinup", "Chin-up"),
            ("PullUp", "Pull-up"),
            ("Pushup", "Push Up"),
            ("SitUp", "Sit Up"),
            ("TBar Row", "T Bar Row"),
            ("Handstand Pushup", "Handstand Push Up"),
        ],
    )
    def test_separator_free_name_emits_the_fuzzy_warning(
        self, raw_name, expected, caplog
    ):
        rows = f"2024-01-01 09:15:00,Back day,1h,{raw_name},1,0,5,0,0,,,\n"
        with caplog.at_level("WARNING", logger="workout_imports.importers.strong"):
            StrongImporter().parse(csv_file(rows))

        fuzzy = _fuzzy_warnings(caplog)
        assert len(fuzzy) == 1
        assert raw_name in fuzzy[0].message
        assert expected in fuzzy[0].message
        assert not _unmapped_warnings(caplog)
        assert not _reordered_warnings(caplog)

    @pytest.mark.parametrize(
        "raw_name,expected",
        [
            ("Chin Up", "Chin-up"),
            ("Chin-up", "Chin-up"),
            ("Pendlay Row (Barbell)", "Pendlay Row"),
            ("Overhead Press (Barbell)", "Overhead Press"),
        ],
    )
    def test_names_matched_by_earlier_stages_do_not_emit_the_fuzzy_warning(
        self, raw_name, expected, caplog
    ):
        rows = f"2024-01-01 09:15:00,Back day,1h,{raw_name},1,135,5,0,0,,,\n"
        with caplog.at_level("WARNING", logger="workout_imports.importers.strong"):
            parsed = StrongImporter().parse(csv_file(rows))
        # Confirms these actually resolved (not just "happened not to be
        # fuzzy" by virtue of being unmapped, which would also satisfy a
        # bare not-fuzzy assertion).
        assert parsed[0].lift == expected
        assert not _fuzzy_warnings(caplog)
        assert not _unmapped_warnings(caplog)
        assert not _reordered_warnings(caplog)

    def test_dumbbell_qualifier_still_ends_up_unmapped_not_fuzzy_matched(self, caplog):
        # The equipment-collapse guard has to hold through stage 5 too:
        # "Bench Press (Dumbbell)" normalizes (separator-free) to
        # "benchpressdumbbell", which must NOT match the "benchpress" key
        # for the canonical "Bench Press" lift.
        rows = "2024-01-01 09:15:00,Push day,1h,Bench Press (Dumbbell),1,50,10,0,0,,,\n"
        with caplog.at_level("WARNING", logger="workout_imports.importers.strong"):
            parsed = StrongImporter().parse(csv_file(rows))

        assert parsed[0].lift == "Bench Press (Dumbbell)"
        assert not _fuzzy_warnings(caplog)
        assert not _reordered_warnings(caplog)
        unmapped = _unmapped_warnings(caplog)
        assert len(unmapped) == 1
        assert "Bench Press (Dumbbell)" in unmapped[0].message

    def test_fuzzy_warning_is_deduped_per_distinct_raw_name(self, caplog):
        rows = (
            "2024-01-01 09:15:00,Back day,1h,Chinup,1,0,5,0,0,,,\n"
            "2024-01-01 09:16:00,Back day,1h,Chinup,2,0,6,0,0,,,\n"
        )
        with caplog.at_level("WARNING", logger="workout_imports.importers.strong"):
            StrongImporter().parse(csv_file(rows))
        assert len(_fuzzy_warnings(caplog)) == 1


@pytest.mark.django_db
class TestStrongImporterQualifierReorder:
    """Stage 4: reorder "Base (Qualifier)" to "Qualifier Base" and retry.

    Strong suffixes equipment; our catalogue prefixes it for some lifts
    (e.g. "Cable Crunch", "Trap Bar Deadlift", "Goblet Squat"). Unlike
    stage 2, this has no equipment allowlist -- reordering is
    information-preserving, so a mismatched equipment token still fails to
    match anything real rather than silently landing on the wrong lift.
    """

    @pytest.mark.parametrize(
        "raw_name,expected",
        [
            ("Crunch (Cable)", "Cable Crunch"),
            ("Crossover (Cable)", "Cable Crossover"),
            ("Pull Through (Cable)", "Cable Pull Through"),
            ("Kickback (Cable)", "Cable Kickback"),
            ("Twist (Cable)", "Cable Twist"),
            ("Swing (Kettlebell)", "Kettlebell Swing"),
            ("Deadlift (Trap Bar)", "Trap Bar Deadlift"),
            ("Row (T Bar)", "T Bar Row"),
            ("Squat (Goblet)", "Goblet Squat"),
            ("Squat (Safety Squat Bar)", "Safety Squat Bar Squat"),
        ],
    )
    def test_reordered_qualifier_resolves_to_canonical_lift(self, raw_name, expected):
        rows = f"2024-01-01 09:15:00,Back day,1h,{raw_name},1,50,10,0,0,,,\n"
        parsed = StrongImporter().parse(csv_file(rows))
        assert parsed[0].lift == expected

    @pytest.mark.parametrize(
        "raw_name,expected",
        [
            ("Crunch (Cable)", "Cable Crunch"),
            ("Crossover (Cable)", "Cable Crossover"),
            ("Pull Through (Cable)", "Cable Pull Through"),
            ("Kickback (Cable)", "Cable Kickback"),
            ("Twist (Cable)", "Cable Twist"),
            ("Swing (Kettlebell)", "Kettlebell Swing"),
            ("Deadlift (Trap Bar)", "Trap Bar Deadlift"),
            ("Row (T Bar)", "T Bar Row"),
            ("Squat (Goblet)", "Goblet Squat"),
            ("Squat (Safety Squat Bar)", "Safety Squat Bar Squat"),
        ],
    )
    def test_reordered_qualifier_emits_the_reorder_warning(
        self, raw_name, expected, caplog
    ):
        rows = f"2024-01-01 09:15:00,Back day,1h,{raw_name},1,50,10,0,0,,,\n"
        with caplog.at_level("WARNING", logger="workout_imports.importers.strong"):
            StrongImporter().parse(csv_file(rows))

        reordered = _reordered_warnings(caplog)
        assert len(reordered) == 1
        assert raw_name in reordered[0].message
        assert expected in reordered[0].message
        assert not _fuzzy_warnings(caplog)
        assert not _unmapped_warnings(caplog)

    @pytest.mark.parametrize("raw_name", ["Bench Press (Dumbbell)", "Curl (Dumbbell)"])
    def test_dumbbell_qualifier_reorder_still_ends_up_unmapped(self, raw_name, caplog):
        # The equipment guard has to hold for the reorder stage too:
        # reordering to "Dumbbell Bench Press"/"Dumbbell Curl" must not
        # match anything, since no canonical name contains "Dumbbell".
        rows = f"2024-01-01 09:15:00,Push day,1h,{raw_name},1,50,10,0,0,,,\n"
        with caplog.at_level("WARNING", logger="workout_imports.importers.strong"):
            parsed = StrongImporter().parse(csv_file(rows))

        assert parsed[0].lift == raw_name
        assert not _reordered_warnings(caplog)
        assert not _fuzzy_warnings(caplog)
        unmapped = _unmapped_warnings(caplog)
        assert len(unmapped) == 1
        assert raw_name in unmapped[0].message

    @pytest.mark.parametrize(
        "raw_name,expected",
        [
            ("Pendlay Row (Barbell)", "Pendlay Row"),
            ("Squat (Barbell)", "Back Squat"),
            ("Chin Up", "Chin-up"),
        ],
    )
    def test_names_matched_by_earlier_stages_do_not_emit_the_reorder_warning(
        self, raw_name, expected, caplog
    ):
        StrongLiftAliasFactory(from_name="Squat (Barbell)", to_name="Back Squat")
        rows = f"2024-01-01 09:15:00,Back day,1h,{raw_name},1,135,5,0,0,,,\n"
        with caplog.at_level("WARNING", logger="workout_imports.importers.strong"):
            parsed = StrongImporter().parse(csv_file(rows))
        assert parsed[0].lift == expected
        assert not _reordered_warnings(caplog)
        assert not _fuzzy_warnings(caplog)
        assert not _unmapped_warnings(caplog)

    def test_reorder_warning_is_deduped_per_distinct_raw_name(self, caplog):
        rows = (
            "2024-01-01 09:15:00,Back day,1h,Swing (Kettlebell),1,50,10,0,0,,,\n"
            "2024-01-01 09:16:00,Back day,1h,Swing (Kettlebell),2,50,8,0,0,,,\n"
        )
        with caplog.at_level("WARNING", logger="workout_imports.importers.strong"):
            StrongImporter().parse(csv_file(rows))
        assert len(_reordered_warnings(caplog)) == 1


@pytest.mark.django_db
class TestStrongImporterRealExport:
    """Parses an actual Strong app export rather than a hand-written fixture.

    Source: github.com/AlexandrosKyriakakis/StrongAppAnalytics, Data/strong.csv
    (cited in the importer's module docstring as the sample its column format
    was verified against, but until this test nothing actually parsed it --
    the docstring's claim was never locked in against regression).
    """

    def test_parses_every_scoreable_row_in_the_real_export(self):
        with REAL_EXPORT_PATH.open("rb") as f:
            parsed = StrongImporter().parse(f)
        # 1903 total rows, minus 21 Swimming/Aerobics rows explicitly logged
        # as Weight=0, Reps=0 with Distance/Seconds instead -- a real-export
        # shape the hand-written synthetic fixtures never exercised, where
        # Weight/Reps are "0" rather than blank.
        assert len(parsed) == 1903 - 21

    def test_every_parsed_set_is_well_formed(self):
        with REAL_EXPORT_PATH.open("rb") as f:
            parsed = StrongImporter().parse(f)
        for s in parsed:
            # weight_kg == 0 is valid (a bodyweight-only set, e.g. Push Up,
            # reports zero added weight with real reps) -- reps > 0 is the
            # actual "was this a completed set" signal.
            assert s.weight_kg >= 0
            assert s.reps > 0
            assert s.lift
            assert s.performed_at is not None

    def test_zero_reps_cardio_row_is_not_scored(self):
        with REAL_EXPORT_PATH.open("rb") as f:
            parsed = StrongImporter().parse(f)
        assert not any(s.lift == "Swimming" for s in parsed)

    def test_bodyweight_only_set_with_zero_weight_is_still_scored(self):
        with REAL_EXPORT_PATH.open("rb") as f:
            parsed = StrongImporter().parse(f)
        push_ups = [s for s in parsed if s.lift == "Push Up"]
        assert len(push_ups) == 7
        assert all(s.weight_kg == Decimal("0.00") and s.reps == 10 for s in push_ups)

    def test_first_row_converts_lb_to_kg_correctly(self):
        with REAL_EXPORT_PATH.open("rb") as f:
            parsed = StrongImporter().parse(f)
        # First row: 2020-12-30 18:51:52, Snatch (Barbell), 40.0 lb x 3 reps.
        # "Snatch (Barbell)" now resolves to the seeded "Snatch" canonical
        # lift via the algorithmic barbell-qualifier-stripping stage, even
        # though nothing here seeds a StrongLiftAlias for it.
        first = parsed[0]
        assert first.lift == "Snatch"
        assert first.reps == 3
        assert first.weight_kg == Decimal("18.14")


class TestParseDecimalFuzz:
    """No cell value from a user-uploaded CSV should ever crash the parser --
    an unparseable cell must resolve to None (row skipped), never raise.
    """

    @given(st.text())
    def test_never_raises_and_result_is_always_finite(self, raw):
        result = _parse_decimal(raw)
        if result is not None:
            assert result.is_finite()

    def test_nan_is_rejected_not_silently_stored(self):
        # Decimal("nan") parses without raising -- must be explicitly
        # rejected, or a NaN weight_kg would reach the database silently.
        assert _parse_decimal("nan") is None
        assert _parse_decimal("NaN") is None

    def test_infinity_is_rejected_not_left_to_crash_quantize(self):
        # Decimal("Infinity") also parses without raising; multiplying and
        # quantizing it downstream raises InvalidOperation uncaught if this
        # helper doesn't reject it here first.
        assert _parse_decimal("Infinity") is None
        assert _parse_decimal("-Infinity") is None

    def test_comma_decimal_is_not_recognized(self):
        # Known gap, not fixed here (same judgment call as the module's
        # documented lbs-only assumption): a European-locale Strong export
        # using a comma decimal separator fails to parse and the row is
        # silently skipped rather than erroring loudly or being reinterpreted.
        assert _parse_decimal("225,5") is None

    def test_thousands_separator_is_not_recognized(self):
        assert _parse_decimal("1,225.5") is None


class TestParseRepsFuzz:
    @given(st.text())
    def test_never_raises(self, raw):
        _parse_reps(raw)

    def test_huge_exponent_string_does_not_crash(self):
        # float("1e400") returns inf rather than raising, so int() on that
        # result raises OverflowError, not ValueError -- a plain `except
        # ValueError` around the float-fallback path misses this entirely.
        assert _parse_reps("1e400") is None
        assert _parse_reps("inf") is None


class TestParseDateFuzz:
    @given(st.text())
    def test_never_raises(self, raw):
        _parse_date(raw)
