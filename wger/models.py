import uuid

from django.conf import settings
from django.db import models


class WgerLiftAlias(models.Model):
    """Maps a raw Wger exercise name (resolved from its numeric exercise ID
    via WgerClient.get_exercise_name) to the canonical standard lift name.

    Mirrors liftosaur.models.LiftAlias. Without an alias, sets logged under a
    Wger exercise name never match a StrengthStandardMultiplier and are
    silently dropped during sync.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    from_name = models.CharField(
        max_length=100,
        unique=True,
        help_text="Exercise name as Wger's exerciseinfo endpoint returns it.",
    )
    to_name = models.CharField(
        max_length=100,
        help_text="Canonical lift name used by the strength standards.",
    )

    class Meta:
        db_table = "wger_liftalias"
        ordering = ["from_name"]
        verbose_name_plural = "lift aliases"

    def __str__(self):
        return f"{self.from_name} -> {self.to_name}"


class WgerSyncLog(models.Model):
    """Mirrors liftosaur.models.LiftosaurSyncLog."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="wger_sync_logs",
    )
    started_at = models.DateTimeField()
    completed_at = models.DateTimeField(null=True, blank=True)
    success = models.BooleanField(null=True)
    result_summary = models.TextField(blank=True)
    error_detail = models.TextField(blank=True)

    class Meta:
        db_table = "wger_synclog"
        ordering = ["-started_at"]

    def __str__(self):
        status = {True: "succeeded", False: "failed", None: "in-progress"}[self.success]
        return f"{self.user} sync {status} at {self.started_at}"
