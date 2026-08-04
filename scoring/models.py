import uuid

from django.conf import settings
from django.db import models


class PointEarnEvent(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="point_earn_events",
    )
    challenge = models.ForeignKey(
        "challenges.Challenge",
        on_delete=models.PROTECT,
        related_name="point_earn_events",
    )
    lift = models.CharField(max_length=100)
    performed_at = models.DateField()
    synced_at = models.DateTimeField()
    reps = models.PositiveIntegerField()
    weight = models.DecimalField(max_digits=7, decimal_places=2)
    points_earned = models.PositiveIntegerField()
    is_current_best = models.BooleanField()
    equipment = models.CharField(
        max_length=100,
        blank=True,
        default="",
        help_text=(
            "Equipment/variant suffix of the source LiftHistory set (e.g. "
            "'Leverage Machine'); empty when the set named no equipment. Part "
            "of the re-scoring idempotency key so a bodyweight set and an "
            "assisted set of the same lift on the same day with the same rep "
            "count are never collapsed into one event."
        ),
    )

    class Meta:
        db_table = "scoring_pointearnevent"
        ordering = ["-synced_at"]

    def __str__(self):
        return (
            f"{self.user} — {self.lift} {self.points_earned}pts "
            f"({'best' if self.is_current_best else 'superseded'})"
        )
