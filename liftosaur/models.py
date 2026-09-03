"""Liftosaur-specific persistence.

The lift register (``Lift``), the pooled set history every tracker writes into
(``LiftHistory``) and the provenance enum spanning all of them (``LiftSource``)
used to live here for historical reasons -- this was the first tracker
integration, so shared lift infrastructure grew inside it. They are core
product data, not Liftosaur data, and now live in ``core.models``
(TASK-347). What remains here is genuinely Liftosaur's alone.
"""

import uuid

from django.conf import settings
from django.db import models


class LiftosaurSyncLog(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="liftosaur_sync_logs",
    )
    started_at = models.DateTimeField()
    completed_at = models.DateTimeField(null=True, blank=True)
    success = models.BooleanField(null=True)
    result_summary = models.TextField(blank=True)
    error_detail = models.TextField(blank=True)

    class Meta:
        db_table = "liftosaur_synclog"
        ordering = ["-started_at"]

    def __str__(self):
        status = {True: "succeeded", False: "failed", None: "in-progress"}[self.success]
        return f"{self.user} sync {status} at {self.started_at}"
