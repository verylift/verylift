import uuid

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from accounts.units import (
    LB_TO_KG,  # noqa: F401 -- re-exported for callers that parse lb weights
)


class LiftSource(models.TextChoices):
    """Provenance of a pooled LiftHistory/PointEarnEvent row.

    Naming convention (TASK-332): a bare ``<tracker>`` value always means the
    live API sync for that tracker; ``<tracker>_CSV`` always means a one-shot
    CSV upload dispatched through the generic
    workout_imports.services.import_workout_csv importer registry (#11, #10,
    TASK-313) rather than a tracker-specific service function. A tracker with
    no live API (Strong) only ever has the CSV member. MANUAL is a lifter
    self-reporting a completed set with no tracker connected (TASK-25).

    LIFTOSAUR is liftosaur.services.sync_user_lifts; LIFTOSAUR_CSV is
    workout_imports.importers.liftosaur.LiftosaurImporter. HEVY is
    hevy_api.services.sync_user_lifts (TASK-312); HEVY_CSV is
    workout_imports.importers.hevy.HevyImporter. WGER is
    wger.services.sync_wger_lifts (#9), which has no CSV counterpart.
    STRONG_CSV is workout_imports.importers.strong.StrongImporter, which has
    no live-API counterpart (#10).

    This is not purely provenance-only bookkeeping — four call sites branch
    on it and depend on getting it right:
    - liftosaur.services.history_watermark, hevy_api.services.history_watermark,
      and wger.services.history_watermark each scope their delta-sync
      watermark query to their own live-sync source, so another source's
      (or that same tracker's CSV import's) rows never truncate a first-ever
      backfill (TASK-319, TASK-332).
    - workout_imports.services.last_imported_at spans every registered
      importer's ``source`` to report "last CSV import" across trackers.
    - liftosaur.management.commands.restamp_lb_converted_lift_history's
      _CANDIDATE_SOURCES depends on which sources ever run a weight through
      LB_TO_KG: HEVY_CSV and STRONG_CSV always do (their export files carry
      lbs only, unconditionally converted); LIFTOSAUR, LIFTOSAUR_CSV, and
      WGER sometimes do (converted only when the synced/uploaded set's unit
      is lb); HEVY never does (Hevy's API returns weight_kg directly, no
      conversion ever runs); MANUAL never does (a manual report copies an
      existing target weight rather than converting a freshly reported
      one). Get this tuple wrong and the command silently restamps — or
      fails to restamp — the wrong rows.

    A new source is a new choice here, never a second boolean field.
    """

    LIFTOSAUR = "liftosaur", _("Liftosaur")
    LIFTOSAUR_CSV = "liftosaur_csv", _("Liftosaur (CSV import)")
    MANUAL = "manual", _("Manual")
    HEVY = "hevy", _("Hevy")
    HEVY_CSV = "hevy_csv", _("Hevy (CSV import)")
    WGER = "wger", _("Wger")
    STRONG_CSV = "strong_csv", _("Strong (CSV import)")


class Lift(models.Model):
    """Reference table of known lifts and their qualities.

    Replaces the hardcoded BODYWEIGHT_ADDED_LIFTS frozenset: rows are seeded
    from a fixture by the ``seed_liftosaur_lifts`` management command and are
    admin-editable, so a new bodyweight-added lift needs no code change or
    redeploy.

    Qualities here are facts about the MOVEMENT, not about any tracker. An
    ``is_liftosaur_builtin`` flag used to live alongside them, meant to mark
    lifts needing a custom exercise provisioned in the user's Liftosaur
    account; it was true on every row (the catalogue is Liftosaur-derived, so
    the column restated this table's own membership) and had no consumer, so
    it was removed. Should tracker-specific per-lift facts ever be needed,
    they belong in a ``(source, lift)`` join table alongside core.LiftAlias,
    not as columns here.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=100, unique=True)
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
