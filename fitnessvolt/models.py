"""Models for the FitnessVolt strength standards cache (TASK-104, doc-1).

FitnessVolt data lives in its own standalone tables — no FK into
standards.models. Cache rows are keyed by FitnessVolt's own identifiers plus
the snapshot version (``data_version``) they were fetched under, so multiple
snapshots coexist side by side and challenges can pin one.
"""

import uuid

from django.db import models


class FitnessVoltStandardCache(models.Model):
    """One cached FitnessVolt weight-class percentile table for one snapshot.

    Each row is one ``weight_classes`` entry from ``/standards/{lift}
    ?format=table&unit=kg`` for one ``(population, lift_slug, sex)`` — the
    raw percentile table exactly as FitnessVolt published it, never reshaped
    into tiers at ingestion time (doc-1 §2: tier resolution is a pure
    read-time interpolation so it stays recomputable).

    Rows are append-only per ``source_snapshot_version``: a refresh inserts a
    new snapshot's rows alongside the old snapshot's rather than updating in
    place, mirroring how StrengthStandardVersion pins a dated, immutable
    snapshot for the built-in path.
    """

    class Population(models.TextChoices):
        VERIFIED = "verified", "Verified (OpenPowerlifting SBD)"
        GYM = "gym", "Gym (self-reported)"

    class Sex(models.TextChoices):
        MALE = "M", "Male"
        FEMALE = "F", "Female"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    population = models.CharField(max_length=20, choices=Population.choices)
    lift_slug = models.CharField(
        max_length=100,
        help_text="FitnessVolt's own lift slug, pre-mapping.",
    )
    sex = models.CharField(max_length=1, choices=Sex.choices)
    weight_class_kg = models.DecimalField(
        max_digits=6,
        decimal_places=2,
        help_text="Weight class in kg (unit=kg is requested at fetch time).",
    )
    weight_class_label = models.CharField(
        max_length=50,
        help_text="FitnessVolt's own weight-class label, stored verbatim ('83 kg').",
    )
    percentiles = models.JSONField(
        help_text=(
            "Verbatim percentile -> 1RM kg table from the response, e.g. "
            "{'p10': 140, 'p25': 162.5, ..., 'p99': 270.5}."
        )
    )
    sample_size = models.IntegerField()
    source_snapshot_version = models.CharField(
        max_length=100,
        help_text="Verbatim data_version from the response (e.g. '2026-06-09').",
    )
    fetched_at = models.DateTimeField()

    class Meta:
        db_table = "fitnessvolt_standardcache"
        unique_together = [
            (
                "population",
                "lift_slug",
                "sex",
                "weight_class_kg",
                "source_snapshot_version",
            ),
        ]

    def __str__(self):
        return (
            f"{self.population} / {self.lift_slug} / {self.sex} / "
            f"{self.weight_class_label} @ {self.source_snapshot_version}"
        )
