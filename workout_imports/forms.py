"""Forms for the generic workout-CSV import (#11)."""

import logging

from django import forms
from django.utils.translation import gettext_lazy as _

from workout_imports.importers import (
    UnrecognizedCsvFormatError,
    csv_header,
    detect_importer,
)

logger = logging.getLogger(__name__)


class WorkoutCsvImportForm(forms.Form):
    """Settings workout-CSV-upload form.

    Validates the file's format is recognized by some registered importer up
    front (wrong extension, or a CSV whose header matches no known tracker
    export) so the view can surface a friendly error instead of a 500 on a
    garbage upload.
    """

    csv_file = forms.FileField()

    def __init__(self, *args, user=None, **kwargs):
        # user is optional and log-only: it never gates validation, it just
        # lets a rejected upload be traced back to who submitted it.
        self.user = user
        super().__init__(*args, **kwargs)

    def clean_csv_file(self):
        uploaded = self.cleaned_data["csv_file"]
        if not uploaded.name.lower().endswith(".csv"):
            raise forms.ValidationError(_("Please upload a .csv file."))
        try:
            detect_importer(uploaded)
        except UnrecognizedCsvFormatError as exc:
            # Log the column header only -- it's structural (a fixed set of
            # column names), never the file's actual workout data -- and it's
            # exactly what's needed to tell whether to add a new importer.
            logger.warning(
                "Workout CSV import rejected for user %s: unrecognized header %s",
                self.user.id if self.user else None,
                csv_header(uploaded),
            )
            raise forms.ValidationError(str(exc)) from exc
        finally:
            uploaded.seek(0)
        return uploaded
