import uuid

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

    Deliberately its own enum rather than a reuse of liftosaur.models.LiftSource
    (the provenance tag on a pooled LiftHistory/PointEarnEvent row): that enum
    also carries MANUAL, which never has aliases to translate, and it lives in
    an app that itself depends on core (liftosaur/wger/workout_imports all
    import from core -- core.client, core.http). Importing LiftSource back into
    core.models would invert that dependency; a small independent enum here
    keeps core free of any app-specific import. A new tracker importer gets a
    new choice here, never a second alias model.
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


class SupportedAppQuerySet(models.QuerySet):
    def featured(self):
        return self.filter(is_affiliate=True)

    def other(self):
        return self.filter(is_affiliate=False)


class SupportedApp(models.Model):
    """A third-party app listed on the "supported apps" page (TASK-254).

    ``is_affiliate`` drives both the page's featured/other split and whether
    the affiliate disclosure renders -- keep it in sync with reality: only
    set it once a real commercial relationship (a coupon, a tracked link)
    exists, per the Liftosaur precedent in _liftosaur_coupon_cta.html.
    """

    name = models.CharField(max_length=100)
    url = models.URLField()
    is_affiliate = models.BooleanField(
        default=False,
        help_text="Whether a commercial relationship (e.g. an affiliate "
        "coupon) exists, requiring the disclosure to render.",
    )
    sort_order = models.PositiveIntegerField(default=0)
    description = models.CharField(max_length=255, blank=True)
    coupon_code = models.CharField(
        max_length=50,
        blank=True,
        default="",
        help_text="Affiliate coupon code for this app, if any (e.g. "
        "Liftosaur's VERYLIFT). Blank means no code renders on this "
        "app's card.",
    )

    objects = SupportedAppQuerySet.as_manager()

    class Meta:
        ordering = ["sort_order", "name"]
        verbose_name = "supported app"

    def __str__(self):
        return self.name


class SupportedAppMode(models.Model):
    """One capability tag (live sync, CSV upload, ...) for a SupportedApp.

    A separate row per mode, not an ArrayField, so the modes shown on the
    page and the modes seeded/edited in admin are the same rows -- one
    source of truth, per TASK-254's requirement that the tags be real data.
    """

    class Mode(models.TextChoices):
        LIVE_SYNC = "live_sync", _("live sync")
        CSV_UPLOAD = "csv_upload", _("csv upload")

    supported_app = models.ForeignKey(
        SupportedApp, on_delete=models.CASCADE, related_name="modes"
    )
    mode = models.CharField(max_length=20, choices=Mode.choices)

    class Meta:
        ordering = ["mode"]
        constraints = [
            models.UniqueConstraint(
                fields=["supported_app", "mode"],
                name="core_supportedappmode_unique_app_mode",
            )
        ]

    def __str__(self):
        return self.get_mode_display()
