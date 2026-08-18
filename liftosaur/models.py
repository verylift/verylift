import uuid

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from accounts.units import (
    LB_TO_KG,  # noqa: F401 -- re-exported for callers that parse lb weights
)


class LiftSource(models.TextChoices):
    """Provenance of a pooled LiftHistory/PointEarnEvent row.

    LIFTOSAUR is every row written by liftosaur.services.sync_user_lifts; MANUAL
    is a lifter self-reporting a completed set with no tracker connected
    (TASK-25); HEVY is a one-shot CSV upload, dispatched by the generic
    workout_imports.services.import_workout_csv importer registry rather than
    a tracker-specific service function (#11); WGER is a live-sync integration
    (wger.services.sync_wger_lifts), mirroring Liftosaur's own sync pattern
    rather than a one-shot upload (#9). Left open for future importers
    (Strong, #10) — a new source is a new choice here, never a second boolean
    field.
    """

    LIFTOSAUR = "liftosaur", _("Liftosaur")
    MANUAL = "manual", _("Manual")
    HEVY = "hevy", _("Hevy")
    WGER = "wger", _("Wger")


class Lift(models.Model):
    """Reference table of known lifts and their qualities.

    Replaces the hardcoded LIFTOSAUR_BUILTIN_LIFTS and BODYWEIGHT_ADDED_LIFTS
    frozensets: rows are seeded from a fixture by the ``seed_liftosaur_lifts``
    management command and are admin-editable, so a Liftosaur catalogue change
    or a new bodyweight-added lift needs no code change or redeploy. Absence of
    a row means "not built-in, not bodyweight-added" — only lifts with at least
    one quality need a row.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=100, unique=True)
    is_liftosaur_builtin = models.BooleanField(
        default=False,
        help_text=(
            "Liftosaur ships this exercise natively; no custom exercise needs "
            "to be provisioned in the user's Liftosaur account."
        ),
    )
    is_bodyweight_added = models.BooleanField(
        default=False,
        help_text=(
            "The strength standard threshold for this lift already includes "
            "the lifter's bodyweight; the recorded weight is the added weight "
            "on top of bodyweight (e.g. Pull-up, Chin-up, Dip)."
        ),
    )

    class Meta:
        db_table = "liftosaur_lift"
        ordering = ["name"]

    def __str__(self):
        return self.name

    @classmethod
    def builtin_names(cls) -> frozenset[str]:
        """Return the set of lift names Liftosaur ships natively."""
        return frozenset(
            cls.objects.filter(is_liftosaur_builtin=True).values_list("name", flat=True)
        )


class LiftAlias(models.Model):
    """Maps a raw Liftosaur exercise name to the canonical standard lift name.

    Replaces the hardcoded LIFT_NAME_ALIASES dict. Without an alias, sets
    logged under a Liftosaur name (e.g. "Squat") never match a
    StrengthStandardMultiplier and are silently dropped during sync.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    from_name = models.CharField(
        max_length=100,
        unique=True,
        help_text="Exercise name as Liftosaur emits it in history payloads.",
    )
    to_name = models.CharField(
        max_length=100,
        help_text="Canonical lift name used by the strength standards.",
    )

    class Meta:
        db_table = "liftosaur_liftalias"
        ordering = ["from_name"]
        verbose_name_plural = "lift aliases"

    def __str__(self):
        return f"{self.from_name} -> {self.to_name}"


class LiftHistory(models.Model):
    """Raw per-lifter completed-set history, independent of any challenge.

    Populated during Liftosaur sync from parsed sets. Challenges read from
    this shared pool; scoring still lives in scoring.PointEarnEvent.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="lift_history",
    )
    lift = models.CharField(max_length=100)
    performed_at = models.DateField()
    weight_kg = models.DecimalField(max_digits=7, decimal_places=2)
    reps = models.PositiveIntegerField()
    equipment = models.CharField(
        max_length=100,
        blank=True,
        default="",
        help_text=(
            "Equipment/variant suffix from the Liftosaur history line (e.g. "
            "'Leverage Machine'); empty when the set named no equipment. "
            "Distinguishes assisted-machine sets, whose recorded weight is "
            "already the net total load, from free bodyweight or added-weight "
            "sets of the same lift."
        ),
    )
    synced_at = models.DateTimeField(null=True, blank=True)
    source = models.CharField(
        max_length=20,
        choices=LiftSource.choices,
        default=LiftSource.LIFTOSAUR,
        help_text=(
            "Where this set came from: a Liftosaur sync pull, or a lifter "
            "self-reporting a completed set with no tracker connected."
        ),
    )

    class Meta:
        db_table = "liftosaur_lifthistory"
        ordering = ["-performed_at"]
        # A set's identity, given Liftosaur exposes no stable set ID: who, which
        # lift, which day, reps, and load. Keying on reps alone collapsed two
        # genuinely different sets performed the same day with the same rep count
        # (e.g. a bodyweight Pull-up recorded as 0 added and an assisted
        # Leverage-Machine Pull-up recorded as its net load, or a top set and a
        # lighter back-off set) into one row, silently destroying the earlier
        # set. weight_kg widens the key so those distinct sets no longer overwrite
        # each other, while a true re-sync of the same set still upserts.
        # equipment is deliberately NOT in the key: it stays in the upsert
        # defaults so a full re-sync restamps it in place onto rows pooled before
        # equipment was captured (the resync_assisted_lifts remediation relies on
        # this) rather than orphaning a stale blank-equipment duplicate.
        unique_together = [("user", "lift", "performed_at", "reps", "weight_kg")]

    def __str__(self):
        return (
            f"{self.user} — {self.lift} {self.weight_kg}kg × {self.reps} "
            f"({self.performed_at})"
        )


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
