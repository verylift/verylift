import uuid

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _


class Notification(models.Model):
    """One in-app notification for one user.

    Never write a row for a user with ``is_active=False``. Deactivation is
    self-serve account deletion (accounts.services.anonymize_account) and it
    blocks login, so such a row can never be read by anyone -- it is dead data
    on a table every other user's unread count scans. Every producer filters
    its recipients accordingly: challenges.views._notify_user_joined,
    challenges.services.close_challenge/remove_participant/transfer_ownership,
    policies.notifications, and scoring.services.notify_ranking_changes (which
    gets it for free -- its deltas come from rank_participants, which already
    drops deactivated accounts).
    """

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
