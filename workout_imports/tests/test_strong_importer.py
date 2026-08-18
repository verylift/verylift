import io
from decimal import Decimal

import pytest

from workout_imports.importers.strong import StrongImporter
from workout_imports.tests.factories import StrongLiftAliasFactory

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
        # 220 lb * 0.453592 = 99.79024 -> quantized to 2dp
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
