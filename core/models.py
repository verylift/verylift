import uuid

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _


class SiteSettings(models.Model):
    """Singleton row for small, operator-editable site settings.

    Admin-configurable rather than env-configurable so a change (e.g. a
    rotated Discord invite) takes effect immediately -- no restart/redeploy
    needed. Always has exactly one row, pk=1; use ``SiteSettings.load()``
    rather than querying directly.
    """

    discord_invite_url = models.URLField(
        default="https://discord.gg/DH5ZWDXJdH",
        help_text=_("Community Discord invite link, shown on the landing page."),
    )
    very_open_invite_url = models.URLField(
        blank=True,
        default="",
        help_text=_(
            "Join link for the current year's Very Open. Blank hides the "
            "onboarding invite step entirely -- clear this once the invite "
            "window closes rather than leaving a dead link live."
        ),
    )
    very_open_label = models.CharField(
        max_length=100,
        blank=True,
        default="",
        help_text=_(
            "Displayed name for the current year's Very Open, e.g. \"The Very "
            "Open '26\". Only shown when the invite URL above is also set."
        ),
    )

    class Meta:
        verbose_name = _("Site Settings")
        verbose_name_plural = _("Site Settings")

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        pass

    @classmethod
    def load(cls):
        obj, _created = cls.objects.get_or_create(pk=1)
        return obj

    def __str__(self):
        return "Site Settings"


class LiftAliasSource(models.TextChoices):
    """Which tracker/data source's raw exercise-name vocabulary a LiftAlias
    row translates from.

    Deliberately its own enum rather than a reuse of :class:`LiftSource`, even
    now that both live in core (TASK-347 moved LiftSource here; the original
    reason for the split was that importing it would have inverted the
    app dependency, and that reason is gone). They are kept apart because
    their member sets answer different questions and genuinely differ:

    * A *vocabulary* is per-tracker, not per-transport. Liftosaur's API and
      its CSV export emit the same exercise names, so LIFTOSAUR here covers
      both of LiftSource's LIFTOSAUR and LIFTOSAUR_CSV. Collapsing them would
      force every alias to be seeded twice.
    * FITNESSVOLT has a name vocabulary to translate but never produces a
      set, so it can never be a LiftSource.
    * MANUAL produces sets but has no vocabulary -- a lifter picks a
      canonical lift from the catalogue, so there is nothing to translate.

    A new tracker importer gets a new choice here, never a second alias model.
    """

    LIFTOSAUR = "liftosaur", _("Liftosaur")
    HEVY = "hevy", _("Hevy")
    STRONG = "strong", _("Strong")
    WGER = "wger", _("Wger")
    FITNESSVOLT = "fitnessvolt", _("FitnessVolt")


class LiftAlias(models.Model):
    """Maps a raw tracker exercise name to the canonical standard lift name.

    Single table for every tracker's aliases (Liftosaur, Hevy, Strong, Wger),
    replacing four near-identical per-app tables that only differed in which
    tracker's raw names they held. ``source`` disambiguates: two trackers can
    legitimately use the same raw name for different canonical lifts, so
    uniqueness is scoped to (source, from_name) rather than a global from_name
    uniqueness the old per-tracker tables enforced implicitly (each table only
    ever held one tracker's names).

    Without an alias, a raw exercise name is pooled unchanged and never
    matches a canonical lift during scoring.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    source = models.CharField(
        max_length=20,
        choices=LiftAliasSource.choices,
        help_text=(
            "Which tracker's raw exercise-name vocabulary this alias translates from."
        ),
    )
    from_name = models.CharField(
        max_length=100,
        help_text="Exercise name as this tracker emits it.",
    )
    to_name = models.CharField(
        max_length=100,
        help_text="Canonical lift name used by the strength standards.",
    )

    class Meta:
        db_table = "core_liftalias"
        ordering = ["source", "from_name"]
        verbose_name_plural = "lift aliases"
        constraints = [
            models.UniqueConstraint(
                fields=["source", "from_name"],
                name="core_liftalias_unique_source_from_name",
            )
        ]

    def __str__(self):
        return f"[{self.source}] {self.from_name} -> {self.to_name}"


class NewsletterSubscriber(models.Model):
    email = models.EmailField(unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = _("Newsletter Subscriber")
        verbose_name_plural = _("Newsletter Subscribers")

    def __str__(self):
        return self.email


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
    - core.management.commands.restamp_lb_converted_lift_history's
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
    from a fixture by the ``seed_lifts`` management command and are
    admin-editable, so a new bodyweight-added lift needs no code change or
    redeploy.

    Qualities here are facts about the MOVEMENT, not about any tracker. An
    ``is_liftosaur_builtin`` flag used to live alongside them, meant to mark
    lifts needing a custom exercise provisioned in the user's Liftosaur
    account; it was true on every row (the catalogue is Liftosaur-derived, so
    the column restated this table's own membership) and had no consumer, so
    it was removed. Should tracker-specific per-lift facts ever be needed,
    they belong in a ``(source, lift)`` join table alongside :class:`LiftAlias`,
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

    The shared pool every ingestion path writes into -- the Liftosaur, Hevy
    and Wger live syncs, the CSV importers, and manual self-reporting -- with
    ``source`` recording which. Challenges read from this pool; scoring still
    lives in scoring.PointEarnEvent.

    ``db_table`` stays "liftosaur_lifthistory" for the same reason
    :class:`Lift` keeps "liftosaur_lift": TASK-347 moved these models out of
    the liftosaur app in Django state only, so no table was renamed and no
    data moved.
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
