"""Shared types for workout-CSV importers (#11).

Split out from importers/__init__.py so individual importer modules (e.g.
hevy.py) can import these without a circular import back through the
package's own __init__, which is what builds the registry out of them.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Protocol

from core.models import LiftSource


class UnrecognizedCsvFormatError(Exception):
    """Raised when an uploaded file's header matches no known importer.

    Caught at the form boundary and surfaced as a friendly ValidationError --
    this is a user-uploaded file, so an unrecognized format must not 500.
    """


@dataclass(frozen=True)
class ParsedSet:
    """One completed working set parsed from a workout-tracker CSV export.

    ``lift`` is already canonicalized against the source importer's own
    alias table -- each importer resolves its own raw exercise names before
    returning, so the pool-write layer never needs to know which tracker app
    a row came from.
    """

    lift: str
    performed_at: date
    reps: int
    weight_kg: Decimal


class CsvImporter(Protocol):
    """One tracker app's CSV export format."""

    source: LiftSource

    def detect(self, header: list[str]) -> bool:
        """Return True if this importer recognizes the CSV column header.

        Must be a distinctive check (e.g. an exact required-column set), not
        a fuzzy one -- a false-positive match silently parses one tracker's
        export using another's column semantics.
        """
        ...

    def parse(self, file_obj) -> list[ParsedSet]:
        """Parse the file into its completed working sets."""
        ...


def decode_csv_text(file_obj) -> str:
    """Decode an uploaded CSV file into text, from wherever its pointer sits.

    Shared by every importer's parse() and by header detection, so a BOM or
    encoding quirk only needs handling in one place. Rewinds first so it can
    be called more than once against the same uploaded file.
    """
    file_obj.seek(0)
    raw = file_obj.read()
    if isinstance(raw, bytes):
        return raw.decode("utf-8-sig", errors="replace")
    return raw
