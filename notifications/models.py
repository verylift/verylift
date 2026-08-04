import uuid

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _


class Notification(models.Model):
    class EventType(models.TextChoices):
        CHALLENGE_CLOSED = "challenge_closed", _("Challenge Closed")
        OVERTAKEN = "overtaken", _("Overtaken")
        USER_JOINED = "user_joined", _("User Joined")
        INVITE_RECEIVED = "invite_received", _("Invite Received")
        REMOVED_FROM_CHALLENGE = (
            "removed_from_challenge",
            _("Removed From Challenge"),
        )
        OWNERSHIP_TRANSFERRED = "ownership_transferred", _("Ownership Transferred")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="notifications",
    )
    event_type = models.CharField(max_length=30, choices=EventType.choices)
    challenge = models.ForeignKey(
        "challenges.Challenge",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="notifications",
    )
    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = "notifications_notification"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.event_type} for {self.user}"
