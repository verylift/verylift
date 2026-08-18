import io

import pytest

from liftosaur.models import LiftSource
from workout_imports.importers import (
    REGISTRY,
    UnrecognizedCsvFormatError,
    detect_importer,
    get_importer_for_header,
)
from workout_imports.importers.hevy import REQUIRED_HEADERS, HevyImporter
from workout_imports.importers.strong import (
    REQUIRED_HEADERS as STRONG_REQUIRED_HEADERS,
)
from workout_imports.importers.strong import StrongImporter

HEVY_HEADER = [
    "title",
    "start_time",
    "end_time",
    "description",
    "exercise_title",
    "superset_id",
    "exercise_notes",
    "set_index",
    "set_type",
    "weight_lbs",
    "reps",
    "distance_km",
    "duration_seconds",
    "rpe",
]

STRONG_HEADER = [
    "Date",
    "Workout Name",
    "Duration",
    "Exercise Name",
    "Set Order",
    "Weight",
    "Reps",
    "Distance",
    "Seconds",
    "Notes",
    "Workout Notes",
    "RPE",
]


class TestHevyImporterDetect:
    def test_matches_full_hevy_header(self):
        assert HevyImporter().detect(HEVY_HEADER) is True

    def test_does_not_match_unrelated_header(self):
        assert HevyImporter().detect(["date", "exercise", "sets", "reps"]) is False

    def test_does_not_match_when_one_required_column_is_missing(self):
        # Detection must be exact, not fuzzy: dropping a single required
        # column (here, weight_lbs) must flip the match to False, not still
        # match on "close enough".
        header = [h for h in HEVY_HEADER if h != "weight_lbs"]
        assert HevyImporter().detect(header) is False

    def test_source_is_hevy(self):
        assert HevyImporter().source == LiftSource.HEVY


class TestStrongImporterDetect:
    def test_matches_full_strong_header(self):
        assert StrongImporter().detect(STRONG_HEADER) is True

    def test_does_not_match_unrelated_header(self):
        assert StrongImporter().detect(["date", "exercise", "sets", "reps"]) is False

    def test_does_not_match_when_one_required_column_is_missing(self):
        # Detection must be exact, not fuzzy: dropping a single required
        # column (here, Weight) must flip the match to False, not still
        # match on "close enough".
        header = [h for h in STRONG_HEADER if h != "Weight"]
        assert StrongImporter().detect(header) is False

    def test_does_not_match_hevy_header(self):
        assert StrongImporter().detect(HEVY_HEADER) is False

    def test_source_is_strong(self):
        assert StrongImporter().source == LiftSource.STRONG


class TestGetImporterForHeader:
    def test_hevy_header_resolves_to_hevy_importer(self):
        importer = get_importer_for_header(HEVY_HEADER)
        assert isinstance(importer, HevyImporter)

    def test_strong_header_resolves_to_strong_importer(self):
        importer = get_importer_for_header(STRONG_HEADER)
        assert isinstance(importer, StrongImporter)

    def test_unrecognized_header_returns_none(self):
        assert get_importer_for_header(["foo", "bar", "baz"]) is None

    def test_registry_contains_required_hevy_headers_subset(self):
        # Sanity-checks the fixture header above actually contains every
        # column HevyImporter requires, so the "matches" test isn't
        # accidentally passing on a header that's missing a required column.
        assert set(HEVY_HEADER) >= REQUIRED_HEADERS

    def test_registry_contains_required_strong_headers_subset(self):
        assert set(STRONG_HEADER) >= STRONG_REQUIRED_HEADERS


class TestDetectImporter:
    def test_recognized_csv_returns_matching_importer(self):
        content = ",".join(HEVY_HEADER) + "\n"
        importer = detect_importer(io.BytesIO(content.encode("utf-8")))
        assert isinstance(importer, HevyImporter)

    def test_recognized_strong_csv_returns_matching_importer(self):
        content = ",".join(STRONG_HEADER) + "\n"
        importer = detect_importer(io.BytesIO(content.encode("utf-8")))
        assert isinstance(importer, StrongImporter)

    def test_unrecognized_csv_raises_friendly_error(self):
        content = "some,other,columns\na,b,c\n"
        with pytest.raises(UnrecognizedCsvFormatError) as exc_info:
            detect_importer(io.BytesIO(content.encode("utf-8")))
        assert "don't recognize" in str(exc_info.value)

    def test_empty_file_raises_friendly_error(self):
        with pytest.raises(UnrecognizedCsvFormatError):
            detect_importer(io.BytesIO(b""))

    def test_registry_is_not_empty(self):
        assert len(REGISTRY) >= 1
