import io
from decimal import Decimal

import pytest

from workout_imports.importers.hevy import HevyImporter
from workout_imports.tests.factories import HevyLiftAliasFactory

HEADER = (
    "title,start_time,end_time,description,exercise_title,superset_id,"
    "exercise_notes,set_index,set_type,weight_lbs,reps,distance_km,"
    "duration_seconds,rpe"
)


def csv_file(rows: str) -> io.BytesIO:
    content = HEADER + "\n" + rows
    return io.BytesIO(content.encode("utf-8"))


@pytest.mark.django_db
class TestHevyImporterParse:
    def test_normal_set_is_parsed(self):
        rows = 'Leg day,"01 Jan 2024, 09:15",,,Squat (Barbell),,,1,normal,225,5,,,\n'
        parsed = HevyImporter().parse(csv_file(rows))
        assert len(parsed) == 1
        assert parsed[0].reps == 5

    def test_warmup_set_is_excluded(self):
        rows = 'Leg day,"01 Jan 2024, 09:15",,,Squat (Barbell),,,1,warmup,135,5,,,\n'
        parsed = HevyImporter().parse(csv_file(rows))
        assert parsed == []

    @pytest.mark.parametrize("set_type", ["normal", "dropset", "failure"])
    def test_non_warmup_set_types_are_counted(self, set_type):
        rows = (
            f'Leg day,"01 Jan 2024, 09:15",,,Squat (Barbell),,,1,{set_type},225,5,,,\n'
        )
        parsed = HevyImporter().parse(csv_file(rows))
        assert len(parsed) == 1

    def test_weight_converted_from_lb_to_kg(self):
        rows = 'Leg day,"01 Jan 2024, 09:15",,,Squat (Barbell),,,1,normal,220,5,,,\n'
        parsed = HevyImporter().parse(csv_file(rows))
        # 220 lb * 0.45359237 = 99.7903214 -> quantized to 2dp
        assert parsed[0].weight_kg == Decimal("99.79")

    def test_heavy_load_conversion_matches_the_exact_pound_definition(self):
        """TASK-325: this constant used to be truncated to Decimal("0.453592"),
        which agreed with the internationally exact 0.45359237 kg/lb for most
        weights but diverged by 0.01 kg at some heavier loads -- 380 lb was
        one of them (172.36 kg under the old constant vs 172.37 kg with the
        exact one). That 0.01 kg gap is exactly LiftHistory's rounding
        granularity, so it was enough to create a second row for a set
        already pooled via the Hevy API sync path (hevy_api.services), which
        takes weight_kg straight from Hevy with no lb conversion at all. This
        pins the corrected, literal output rather than re-deriving it from
        the constant, so a regression back to the truncated value fails it."""
        rows = 'Leg day,"01 Jan 2024, 09:15",,,Squat (Barbell),,,1,normal,380,5,,,\n'
        parsed = HevyImporter().parse(csv_file(rows))
        assert parsed[0].weight_kg == Decimal("172.37")

    def test_non_numeric_weight_row_is_skipped_not_fatal(self):
        rows = (
            'Cardio,"01 Jan 2024, 09:15",,,Running,,,'
            "1,normal,not-a-number,10,5.2,1800,\n"
            'Leg day,"01 Jan 2024, 09:20",,,Squat (Barbell),,,'
            "1,normal,225,5,,,\n"
        )
        parsed = HevyImporter().parse(csv_file(rows))
        assert len(parsed) == 1

    def test_blank_weight_cardio_row_is_skipped_not_fatal(self):
        rows = (
            'Cardio,"01 Jan 2024, 09:15",,,Running,,,'
            "1,normal,,,5.2,1800,\n"
            'Leg day,"01 Jan 2024, 09:20",,,Squat (Barbell),,,'
            "1,normal,225,5,,,\n"
        )
        parsed = HevyImporter().parse(csv_file(rows))
        assert len(parsed) == 1

    def test_non_numeric_reps_row_is_skipped_not_fatal(self):
        rows = 'Leg day,"01 Jan 2024, 09:15",,,Squat (Barbell),,,1,normal,225,,,,\n'
        parsed = HevyImporter().parse(csv_file(rows))
        assert parsed == []

    def test_unparseable_start_time_row_is_skipped_not_fatal(self):
        rows = "Leg day,not-a-date,,,Squat (Barbell),,,1,normal,225,5,,,\n"
        parsed = HevyImporter().parse(csv_file(rows))
        assert parsed == []

    def test_blank_exercise_title_is_skipped(self):
        rows = 'Leg day,"01 Jan 2024, 09:15",,,,,,1,normal,225,5,,,\n'
        parsed = HevyImporter().parse(csv_file(rows))
        assert parsed == []

    def test_mapped_alias_resolves_to_canonical_name(self):
        HevyLiftAliasFactory(from_name="Squat (Barbell)", to_name="Back Squat")
        rows = 'Leg day,"01 Jan 2024, 09:15",,,Squat (Barbell),,,1,normal,225,5,,,\n'
        parsed = HevyImporter().parse(csv_file(rows))
        assert parsed[0].lift == "Back Squat"

    def test_unmapped_exercise_passes_through_unchanged(self):
        rows = (
            'Leg day,"01 Jan 2024, 09:15",,,Some Unmapped Exercise,,,'
            "1,normal,225,5,,,\n"
        )
        parsed = HevyImporter().parse(csv_file(rows))
        assert parsed[0].lift == "Some Unmapped Exercise"


@pytest.mark.django_db
class TestHevyImporterFallbackResolutionStages:
    """Hevy now shares the same six-stage resolution chain Strong CSV import
    uses (core.lift_resolution), not just a bare alias-map lookup -- these
    fallback stages (equipment-qualifier stripping, reorder, fuzzy match)
    used to only apply to Strong imports.
    """

    def test_barbell_qualifier_is_stripped_without_an_explicit_alias(self):
        # "Pendlay Row" is a seeded canonical lift; nothing here seeds a
        # HevyLiftAlias for "Pendlay Row (Barbell)".
        rows = (
            'Back day,"01 Jan 2024, 09:15",,,Pendlay Row (Barbell),,,'
            "1,normal,135,5,,,\n"
        )
        parsed = HevyImporter().parse(csv_file(rows))
        assert parsed[0].lift == "Pendlay Row"

    def test_dumbbell_qualifier_does_not_collapse_onto_barbell_canonical_name(self):
        rows = (
            'Push day,"01 Jan 2024, 09:15",,,Bench Press (Dumbbell),,,'
            "1,normal,50,10,,,\n"
        )
        parsed = HevyImporter().parse(csv_file(rows))
        assert parsed[0].lift == "Bench Press (Dumbbell)"

    def test_reordered_qualifier_resolves_to_canonical_lift(self):
        rows = 'Back day,"01 Jan 2024, 09:15",,,Crunch (Cable),,,1,normal,50,10,,,\n'
        parsed = HevyImporter().parse(csv_file(rows))
        assert parsed[0].lift == "Cable Crunch"

    def test_separator_free_name_resolves_via_fuzzy_stage(self):
        rows = 'Back day,"01 Jan 2024, 09:15",,,Chinup,,,1,normal,0,5,,,\n'
        parsed = HevyImporter().parse(csv_file(rows))
        assert parsed[0].lift == "Chin-up"

    def test_fallback_stage_hits_emit_a_warning_naming_hevy(self, caplog):
        rows = 'Back day,"01 Jan 2024, 09:15",,,Chinup,,,1,normal,0,5,,,\n'
        with caplog.at_level("WARNING", logger="workout_imports.importers.hevy"):
            HevyImporter().parse(csv_file(rows))

        fuzzy_warnings = [
            r for r in caplog.records if "separator-insensitive fallback" in r.message
        ]
        assert len(fuzzy_warnings) == 1
        assert "Hevy CSV import" in fuzzy_warnings[0].message
