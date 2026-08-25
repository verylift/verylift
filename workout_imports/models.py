"""Models for the generic workout-CSV import (#11).

Multiple tracker apps' CSV exports land on the same upload endpoint/form; the
backend auto-detects which app produced a given file (workout_imports.importers)
and dispatches to that importer. Each supported app gets its own alias table
here, since raw exercise names differ by app -- Hevy suffixes equipment in
parens (e.g. "Bench Press (Barbell)"), which won't match another tracker's
naming convention. This is additive: a second importer needing its own alias
table is a new model in this file, not a change to an existing one.
"""

import uuid

from django.db import models


class HevyLiftAlias(models.Model):
    """Maps a raw Hevy exercise name to the canonical standard lift name.

    Mirrors fitnessvolt.models.FitnessVoltLiftAlias / liftosaur.models.LiftAlias
    exactly. Hevy's own exercise names won't match Liftosaur's LiftAlias
    from_name values, so this is a separate table rather than a share of it.
    Without an alias, a raw Hevy exercise name is pooled unchanged and never
    matches a canonical lift during scoring.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    from_name = models.CharField(
        max_length=100,
        unique=True,
        help_text="Exercise name as Hevy emits it in its CSV export.",
    )
    to_name = models.CharField(
        max_length=100,
        help_text="Canonical lift name used by the strength standards.",
    )

    class Meta:
        db_table = "hevy_liftalias"
        ordering = ["from_name"]
        verbose_name_plural = "Hevy lift aliases"

    def __str__(self):
        return f"{self.from_name} -> {self.to_name}"


class StrongLiftAlias(models.Model):
    """Maps a raw Strong exercise name to the canonical standard lift name.

    Mirrors fitnessvolt.models.FitnessVoltLiftAlias / liftosaur.models.LiftAlias
    exactly. Strong's own exercise names won't match Liftosaur's LiftAlias
    from_name values, so this is a separate table rather than a share of it.
    Without an alias, a raw Strong exercise name is pooled unchanged and never
    matches a canonical lift during scoring.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    from_name = models.CharField(
        max_length=100,
        unique=True,
        help_text="Exercise name as Strong emits it in its CSV export.",
    )
    to_name = models.CharField(
        max_length=100,
        help_text="Canonical lift name used by the strength standards.",
    )

    class Meta:
        db_table = "strong_liftalias"
        ordering = ["from_name"]
        verbose_name_plural = "Strong lift aliases"

    def __str__(self):
        return f"{self.from_name} -> {self.to_name}"
