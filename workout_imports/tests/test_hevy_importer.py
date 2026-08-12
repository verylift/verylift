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
        # 220 lb * 0.453592 = 99.79024 -> quantized to 2dp
        assert parsed[0].weight_kg == Decimal("99.79")

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
