import uuid

from django.conf import settings
from django.db import models


class HevySyncLog(models.Model):
    """Per-user Hevy API sync attempt, mirroring liftosaur.LiftosaurSyncLog.

    A separate log from Liftosaur's rather than a shared/polymorphic one: the
    two trackers sync independently (a user could in principle have both a
    Liftosaur key and a Hevy key), and each needs its own watermark/cooldown
    bookkeeping.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="hevy_sync_logs",
    )
    started_at = models.DateTimeField()
    completed_at = models.DateTimeField(null=True, blank=True)
    success = models.BooleanField(null=True)
    result_summary = models.TextField(blank=True)
    error_detail = models.TextField(blank=True)

    class Meta:
        db_table = "hevy_api_synclog"
        ordering = ["-started_at"]

    def __str__(self):
        status = {True: "succeeded", False: "failed", None: "in-progress"}[self.success]
        return f"{self.user} Hevy sync {status} at {self.started_at}"
