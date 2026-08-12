import io

import pytest

from accounts.tests.factories import UserFactory
from liftosaur.models import LiftHistory, LiftSource
from workout_imports.importers import UnrecognizedCsvFormatError
from workout_imports.services import import_workout_csv, last_imported_at

HEADER = (
    "title,start_time,end_time,description,exercise_title,superset_id,"
    "exercise_notes,set_index,set_type,weight_lbs,reps,distance_km,"
    "duration_seconds,rpe"
)


def csv_file(rows: str) -> io.BytesIO:
    content = HEADER + "\n" + rows
    return io.BytesIO(content.encode("utf-8"))


@pytest.mark.django_db
class TestImportWorkoutCsv:
    def test_pools_sets_and_reports_detected_source(self):
        user = UserFactory()
        rows = 'Leg day,"01 Jan 2024, 09:15",,,Squat (Barbell),,,1,normal,225,5,,,\n'
        result = import_workout_csv(user, csv_file(rows))
        assert result.source == LiftSource.HEVY
        assert result.pooled_count == 1
        history = LiftHistory.objects.get(user=user)
        assert history.source == LiftSource.HEVY

    def test_reimporting_same_file_upserts_not_duplicates(self):
        user = UserFactory()
        rows = 'Leg day,"01 Jan 2024, 09:15",,,Squat (Barbell),,,1,normal,225,5,,,\n'
        first = import_workout_csv(user, csv_file(rows))
        second = import_workout_csv(user, csv_file(rows))
        assert first.pooled_count == second.pooled_count == 1
        assert LiftHistory.objects.filter(user=user).count() == 1

    def test_returns_count_of_parsed_working_sets(self):
        user = UserFactory()
        rows = (
            'Leg day,"01 Jan 2024, 09:15",,,Squat (Barbell),,,'
            "1,warmup,135,5,,,\n"
            'Leg day,"01 Jan 2024, 09:15",,,Squat (Barbell),,,'
            "2,normal,225,5,,,\n"
            'Leg day,"01 Jan 2024, 09:15",,,Bench Press (Barbell),,,'
            "1,normal,185,5,,,\n"
        )
        result = import_workout_csv(user, csv_file(rows))
        assert result.pooled_count == 2
        assert LiftHistory.objects.filter(user=user).count() == 2

    def test_unrecognized_format_raises_instead_of_pooling_anything(self):
        user = UserFactory()
        bogus = io.BytesIO(b"not,a,recognized,tracker,header\na,b,c,d,e\n")
        with pytest.raises(UnrecognizedCsvFormatError):
            import_workout_csv(user, bogus)
        assert not LiftHistory.objects.filter(user=user).exists()


@pytest.mark.django_db
class TestLastImportedAt:
    def test_none_when_never_imported(self):
        user = UserFactory()
        assert last_imported_at(user) is None

    def test_reflects_most_recent_import(self):
        user = UserFactory()
        rows = 'Leg day,"01 Jan 2024, 09:15",,,Squat (Barbell),,,1,normal,225,5,,,\n'
        import_workout_csv(user, csv_file(rows))
        assert last_imported_at(user) is not None
