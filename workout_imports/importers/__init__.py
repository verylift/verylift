"""Registry of supported workout-CSV importers (#11).

Multiple tracker apps' CSV exports land on the same upload endpoint; the
backend auto-detects which app produced a given file by its header
signature and dispatches to that importer. Hevy is the first supported
format -- adding another tracker is adding a new importer module and
registering it below, not a new upload endpoint or view.
"""

import csv
import io

from workout_imports.importers.base import (
    CsvImporter,
    ParsedSet,
    UnrecognizedCsvFormatError,
    decode_csv_text,
)
from workout_imports.importers.hevy import HevyImporter
from workout_imports.importers.strong import StrongImporter

__all__ = [
    "REGISTRY",
    "CsvImporter",
    "ParsedSet",
    "UnrecognizedCsvFormatError",
    "csv_header",
    "detect_importer",
    "get_importer_for_header",
]

REGISTRY: list[CsvImporter] = [HevyImporter(), StrongImporter()]


def csv_header(file_obj) -> list[str]:
    """Return an uploaded CSV's column header row, without consuming its data.

    Public (not the file's content, just its column names) so a caller like
    the upload form can log *what* an unrecognized file looked like without
    ever touching a data row.
    """
    text = decode_csv_text(file_obj)
    return next(csv.reader(io.StringIO(text)), [])


def get_importer_for_header(header: list[str]) -> CsvImporter | None:
    """Return the first registered importer whose detect() matches, or None."""
    for importer in REGISTRY:
        if importer.detect(header):
            return importer
    return None


def detect_importer(file_obj) -> CsvImporter:
    """Return the importer matching an uploaded file's CSV header.

    Raises UnrecognizedCsvFormatError if no known importer's header
    signature matches -- this is a user-uploaded file, so an unsupported
    format must surface as a friendly error, not a 500 or a silent no-op.
    """
    header = csv_header(file_obj)
    importer = get_importer_for_header(header)
    if importer is None:
        supported = ", ".join(sorted({str(i.source.label) for i in REGISTRY}))
        raise UnrecognizedCsvFormatError(
            "We don't recognize this CSV format yet. Supported exports: " + supported
        )
    return importer
