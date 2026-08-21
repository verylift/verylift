import uuid

from django.contrib.auth.models import (
    AbstractBaseUser,
    BaseUserManager,
    PermissionsMixin,
)
from django.db import models
from django.utils.translation import gettext
from django.utils.translation import gettext_lazy as _

from core.fields import EncryptedCharField


class UserManager(BaseUserManager):
    def create_user(self, username, email=None, password=None, **extra_fields):
        user = self.model(username=username, email=email or "", **extra_fields)
        if password:
            user.set_password(password)
        else:
            user.set_unusable_password()
        user.save(using=self._db)
        return user

    def create_superuser(self, username, email=None, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        return self.create_user(username, email, password, **extra_fields)


class User(AbstractBaseUser, PermissionsMixin):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    username = models.CharField(max_length=150, unique=True)
    email = models.EmailField(blank=True)
    display_name = models.CharField(max_length=150, blank=True)

    # User-uploaded profile photo. Falls back to initials avatar (see
    # components/_avatar.html) everywhere this is unset. New uploads are stored
    # as AVIF at up to 1024px (accounts.services.transcode_avatar_to_avif);
    # rows predating that keep whatever format they were uploaded in.
    avatar = models.ImageField(upload_to="avatars/", null=True, blank=True)

    # OIDC subject claim; unique external-identity key from whichever OIDC
    # provider is configured.
    oidc_sub = models.CharField(max_length=255, unique=True, null=True, blank=True)

    # Fernet-encrypted at rest (TASK-285). Reads and writes are transparent, but
    # max_length bounds the *ciphertext*: 600 covers the ~420-char token of a
    # 255-char plaintext with headroom. The column cannot be filtered on.
    liftosaur_api_key = EncryptedCharField(max_length=600, null=True, blank=True)

    # Wger is self-hostable, so (unlike Liftosaur) there is no single fixed
    # API base URL -- each user supplies their own instance's URL alongside
    # their API token.
    wger_instance_url = models.URLField(max_length=500, null=True, blank=True)
    # Fernet-encrypted at rest, same rationale as liftosaur_api_key above.
    wger_api_token = EncryptedCharField(max_length=600, null=True, blank=True)

    class UnitPreference(models.TextChoices):
        KG = "kg", _("Kilograms (kg)")
        LB = "lb", _("Pounds (lb)")

    # Defaults to lb, matching the onboarding wizard's own unit step, so an
    # account that never finishes onboarding still lands on a wizard-consistent
    # value instead of a silent kg fallback.
    unit_preference = models.CharField(
        max_length=2,
        choices=UnitPreference.choices,
        default=UnitPreference.LB,
    )

    class AcquisitionSource(models.TextChoices):
        UNKNOWN = "", _("Unknown")
        INVITE_LINK = "invite_link", _("Challenge invite link")
        DIRECT = "direct", _("Direct signup")
        OIDC = "oidc", _("Single sign-on")
        ADMIN = "admin", _("Created by an operator")

    # How this account was created (TASK-249, AC#3). Default is UNKNOWN,
    # deliberately not DIRECT — a row created through some path nobody
    # instrumented should read as unknown rather than be silently
    # mislabelled. The per-membership half of this provenance is
    # challenges.ChallengeParticipant.joined_via_link (which invite link
    # admitted a user to a given challenge, as distinct from how their
    # account itself came to exist).
    acquisition_source = models.CharField(
        max_length=20,
        choices=AcquisitionSource.choices,
        blank=True,
        default=AcquisitionSource.UNKNOWN,
    )

    # Empty string means "automatic" — fall through to LocaleMiddleware's
    # cookie/Accept-Language detection. Deliberately no `choices` kwarg: valid
    # values are checked against settings.LANGUAGES in LanguageForm, so adding
    # a future language never requires a migration.
    language = models.CharField(max_length=10, blank=True, default="")

    # Pinned IANA timezone (e.g. "America/Toronto"). Empty string means
    # "automatic" — fall through to the browser-detected value in the
    # pp_timezone cookie, then to settings.TIME_ZONE. Deliberately no
    # `choices` kwarg, matching `language` above: valid values are checked
    # against zoneinfo.available_timezones() in TimezoneForm, so a tz-database
    # update never requires a migration.
    timezone = models.CharField(max_length=64, blank=True, default="")

    # Best-known browser-detected IANA timezone (TASK-300), refreshed
    # opportunistically by accounts.middleware.UserTimezoneMiddleware whenever
    # the pp_timezone cookie resolves to something new. Distinct from the
    # pinned `timezone` field above: this exists purely so code with no live
    # request to read that cookie from (e.g. the close_challenges cron) has
    # something better than UTC to fall back to for an "automatic" account,
    # without turning "automatic" into a de-facto pin for the live
    # per-request rendering path, which must stay exactly as dynamic as it
    # already is.
    detected_timezone = models.CharField(max_length=64, blank=True, default="")

    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    deactivated_at = models.DateTimeField(null=True, blank=True)

    # When the user acknowledged the Terms of Service and Privacy Policy during
    # self-serve registration. Null for accounts that predate the checkbox and for
    # SSO/OIDC users, who never see the registration form and are not gated.
    tos_accepted_at = models.DateTimeField(null=True, blank=True)

    date_joined = models.DateTimeField(auto_now_add=True)

    objects = UserManager()

    USERNAME_FIELD = "username"
    REQUIRED_FIELDS = []

    class Meta:
        db_table = "accounts_user"

    def __str__(self):
        return self.display_name or self.username

    @property
    def effective_display_name(self) -> str:
        """The name to show this user under anywhere participant-facing.

        Deactivated accounts (self-serve deletion -- the only path that ever
        sets ``is_active=False``) already have their real username/
        display_name replaced with a random pseudonym by
        ``accounts.services.anonymize_account``. Appending "(deleted)" marks
        that clearly instead of leaving a departed member looking like an
        unexplained stranger next to real names on the same leaderboard or
        chart. Every leaderboard/chart/participant-list display site should
        use this instead of ``__str__``/``display_name or username`` directly,
        so a deactivated account is annotated the same way everywhere rather
        than each call site re-deciding whether and how to flag it.
        """
        name = self.display_name or self.username
        if self.is_active:
            return name
        return gettext("%(name)s (deleted)") % {"name": name}


class TrackerRequest(models.Model):
    """A free-text "I use a different tracker" signal from onboarding.

    Not a connection or a credential of any kind -- purely product-feedback
    data for triaging which tracker to build next (mirrors the existing
    GitHub-issue-driven tracker backlog, e.g. #26). Created only when a lifter
    picks "A different one" in onboarding_tracking_method_view and then names
    it in onboarding_other_tracker_view; a blank/skipped name creates nothing.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="tracker_requests",
    )
    app_name = models.CharField(max_length=200)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = _("Tracker Request")
        verbose_name_plural = _("Tracker Requests")

    def __str__(self):
        return f"{self.user} wants {self.app_name}"
