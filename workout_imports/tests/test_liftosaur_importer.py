import io
from decimal import Decimal

import pytest

from core.tests.factories import LiftAliasFactory
from workout_imports.importers.liftosaur import LiftosaurImporter

HEADER = (
    "Workout DateTime,Program,Day Name,Exercise,Is Warmup Set?,Required Reps,"
    "Completed Reps,Is AMRAP?,Required RPE,Completed RPE,Log RPE?,"
    "Required Weight Value,Required Weight Unit,Completed Weight Value,"
    "Completed Weight Unit,Ask Weight?,Completed Reps Time,Target Muscles,"
    "Synergist Muscles,Notes"
)


def csv_file(rows: str) -> io.BytesIO:
    content = HEADER + "\n" + rows
    return io.BytesIO(content.encode("utf-8"))


def row(
    *,
    workout_datetime="2026-03-01T10:00:00.000Z",
    exercise="Squat",
    is_warmup="0",
    completed_reps="5",
    completed_weight_value="225",
    completed_weight_unit="lb",
) -> str:
    # The exercise cell can itself contain a comma ("Name, Equipment"), so it
    # must be quoted like Liftosaur's real export quotes it.
    return (
        f'{workout_datetime},My Program,Day 1,"{exercise}",{is_warmup},5,'
        f"{completed_reps},0,,,0,225,lb,{completed_weight_value},"
        f"{completed_weight_unit},0,{workout_datetime},,,\n"
    )


@pytest.mark.django_db
class TestLiftosaurImporterParse:
    def test_normal_set_is_parsed(self):
        parsed = LiftosaurImporter().parse(csv_file(row()))
        assert len(parsed) == 1
        assert parsed[0].reps == 5

    def test_warmup_set_is_included(self):
        # Unlike Hevy, Liftosaur's own API-sync path pools every completed
        # set (warmup included) -- only a Liftoscript "target" section is
        # skipped, and the CSV export has no equivalent of that.
        parsed = LiftosaurImporter().parse(csv_file(row(is_warmup="1")))
        assert len(parsed) == 1

    def test_weight_converted_from_lb_to_kg(self):
        rows = row(completed_weight_value="220", completed_weight_unit="lb")
        parsed = LiftosaurImporter().parse(csv_file(rows))
        # 220 lb * 0.45359237 = 99.7903214 -> quantized to 2dp
        assert parsed[0].weight_kg == Decimal("99.79")

    def test_weight_kg_passes_through_unconverted(self):
        rows = row(completed_weight_value="100", completed_weight_unit="kg")
        parsed = LiftosaurImporter().parse(csv_file(rows))
        assert parsed[0].weight_kg == Decimal("100.00")

    def test_non_numeric_reps_row_is_skipped_not_fatal(self):
        rows = row(completed_reps="five")
        parsed = LiftosaurImporter().parse(csv_file(rows))
        assert parsed == []

    def test_blank_completed_reps_row_is_skipped_not_fatal(self):
        rows = row(completed_reps="")
        parsed = LiftosaurImporter().parse(csv_file(rows))
        assert parsed == []

    def test_non_numeric_weight_row_is_skipped_not_fatal(self):
        rows = row(completed_weight_value="not-a-number")
        parsed = LiftosaurImporter().parse(csv_file(rows))
        assert parsed == []

    def test_unrecognized_weight_unit_row_is_skipped_not_fatal(self):
        rows = row(completed_weight_unit="stones")
        parsed = LiftosaurImporter().parse(csv_file(rows))
        assert parsed == []

    def test_unparseable_workout_datetime_row_is_skipped_not_fatal(self):
        rows = row(workout_datetime="not-a-date")
        parsed = LiftosaurImporter().parse(csv_file(rows))
        assert parsed == []

    def test_blank_exercise_is_skipped(self):
        rows = row(exercise="")
        parsed = LiftosaurImporter().parse(csv_file(rows))
        assert parsed == []

    def test_equipment_suffix_is_split_before_alias_lookup(self):
        LiftAliasFactory(from_name="Custom Exercise", to_name="Custom Canonical Name")
        rows = row(exercise="Custom Exercise, Leverage Machine")
        parsed = LiftosaurImporter().parse(csv_file(rows))
        assert parsed[0].lift == "Custom Canonical Name"

    def test_mapped_alias_resolves_to_canonical_name(self):
        LiftAliasFactory(from_name="Custom Exercise", to_name="Custom Canonical Name")
        rows = row(exercise="Custom Exercise")
        parsed = LiftosaurImporter().parse(csv_file(rows))
        assert parsed[0].lift == "Custom Canonical Name"

    def test_unmapped_exercise_passes_through_unchanged(self):
        rows = row(exercise="Some Unmapped Exercise")
        parsed = LiftosaurImporter().parse(csv_file(rows))
        assert parsed[0].lift == "Some Unmapped Exercise"


@pytest.mark.django_db
class TestLiftosaurImporterFallbackResolutionStages:
    """Liftosaur CSV import now shares the same six-stage resolution chain
    Strong CSV import uses (core.lift_resolution), not just a bare
    alias-map lookup -- these fallback stages used to only apply to Strong.
    """

    def test_barbell_qualifier_is_stripped_without_an_explicit_alias(self):
        rows = row(exercise="Pendlay Row (Barbell)")
        parsed = LiftosaurImporter().parse(csv_file(rows))
        assert parsed[0].lift == "Pendlay Row"

    def test_dumbbell_qualifier_does_not_collapse_onto_barbell_canonical_name(self):
        rows = row(exercise="Bench Press (Dumbbell)")
        parsed = LiftosaurImporter().parse(csv_file(rows))
        assert parsed[0].lift == "Bench Press (Dumbbell)"

    def test_reordered_qualifier_resolves_to_canonical_lift(self):
        rows = row(exercise="Crunch (Cable)")
        parsed = LiftosaurImporter().parse(csv_file(rows))
        assert parsed[0].lift == "Cable Crunch"

    def test_separator_free_name_resolves_via_fuzzy_stage(self):
        # "TBar Row" is deliberately not one of the fixture's seeded aliases
        # (unlike e.g. "Chinup"/"Pullup", which already have explicit
        # aliases and would hit stage 1 instead of this fallback stage).
        rows = row(exercise="TBar Row")
        parsed = LiftosaurImporter().parse(csv_file(rows))
        assert parsed[0].lift == "T Bar Row"

    def test_fallback_stage_hits_emit_a_warning_naming_liftosaur(self, caplog):
        rows = row(exercise="TBar Row")
        with caplog.at_level("WARNING", logger="workout_imports.importers.liftosaur"):
            LiftosaurImporter().parse(csv_file(rows))

        fuzzy_warnings = [
            r for r in caplog.records if "separator-insensitive fallback" in r.message
        ]
        assert len(fuzzy_warnings) == 1
        assert "Liftosaur CSV import" in fuzzy_warnings[0].message
