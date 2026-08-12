from django.core.files.uploadedfile import SimpleUploadedFile

from accounts.tests.factories import UserFactory
from workout_imports.forms import WorkoutCsvImportForm

HEADER = (
    "title,start_time,end_time,description,exercise_title,superset_id,"
    "exercise_notes,set_index,set_type,weight_lbs,reps,distance_km,"
    "duration_seconds,rpe\n"
)


def make_upload(name, content):
    return SimpleUploadedFile(name, content.encode("utf-8"), content_type="text/csv")


class TestWorkoutCsvImportForm:
    def test_valid_hevy_csv_passes(self):
        content = HEADER + (
            'Leg day,"01 Jan 2024, 09:15",,,Squat (Barbell),,,1,normal,225,5,,,\n'
        )
        form = WorkoutCsvImportForm(
            data={}, files={"csv_file": make_upload("export.csv", content)}
        )
        assert form.is_valid()

    def test_non_csv_extension_rejected(self):
        content = HEADER + "\n"
        form = WorkoutCsvImportForm(
            data={}, files={"csv_file": make_upload("export.txt", content)}
        )
        assert not form.is_valid()
        assert "Please upload a .csv file." in form.errors["csv_file"]

    def test_unrecognized_format_rejected_with_friendly_message(self):
        content = "some,other,columns\na,b,c\n"
        form = WorkoutCsvImportForm(
            data={}, files={"csv_file": make_upload("export.csv", content)}
        )
        assert not form.is_valid()
        assert "don't recognize" in form.errors["csv_file"][0]

    def test_unrecognized_format_is_logged_with_user_and_header(self, db, caplog):
        user = UserFactory()
        content = "some,other,columns\na,b,c\n"
        form = WorkoutCsvImportForm(
            data={},
            files={"csv_file": make_upload("mystery-export.csv", content)},
            user=user,
        )
        with caplog.at_level("WARNING", logger="workout_imports.forms"):
            assert not form.is_valid()
        [record] = caplog.records
        assert str(user.id) in record.message
        # The column header is logged (useful for diagnosing a new format);
        # the filename and any data row must not be -- only the header names
        # are structural, not the user's own workout content.
        assert "some" in record.message and "other" in record.message
        assert "mystery-export.csv" not in record.message
        assert "a,b,c" not in record.message

    def test_valid_upload_file_pointer_reset_for_reuse(self):
        content = HEADER + (
            'Leg day,"01 Jan 2024, 09:15",,,Squat (Barbell),,,1,normal,225,5,,,\n'
        )
        form = WorkoutCsvImportForm(
            data={}, files={"csv_file": make_upload("export.csv", content)}
        )
        assert form.is_valid()
        cleaned_file = form.cleaned_data["csv_file"]
        # The view re-reads this file for the actual import, so clean() must
        # leave the pointer at the start rather than exhausted from detection.
        assert cleaned_file.read(1) != b""
