import uuid

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _


class Policy(models.Model):
    class PolicyType(models.TextChoices):
        TOS = "TOS", _("Terms of Service")
        PRIVACY = "PRIVACY", _("Privacy Policy")
        COOKIE = "COOKIE", _("Cookie Policy")
        EULA = "EULA", _("EULA")
        OTHER = "OTHER", _("Other")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    slug = models.SlugField(unique=True)
    policy_type = models.CharField(max_length=20, choices=PolicyType.choices)
    requires_consent = models.BooleanField(
        default=True,
        help_text="Whether this policy requires tracked user acceptance.",
    )
    gates_access = models.BooleanField(
        default=True,
        help_text="Whether non-acceptance blocks web app access.",
    )
    description = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name_plural = "policies"
        ordering = ["name"]

    def __str__(self):
        return self.name


class PolicyVersionQuerySet(models.QuerySet):
    def active_requiring_consent(self):
        """Active versions of policies that require user consent."""
        return self.filter(is_active=True, policy__requires_consent=True)

    def active_gated(self):
        """Subset of active_requiring_consent that also gate app access."""
        return self.active_requiring_consent().filter(policy__gates_access=True)


class PolicyVersion(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    policy = models.ForeignKey(
        Policy, on_delete=models.CASCADE, related_name="versions"
    )
    version = models.CharField(max_length=50)
    url = models.URLField(help_text="Link to the actual document content.")
    effective_date = models.DateField()
    is_active = models.BooleanField(
        default=False,
        help_text="Only one active version per policy at a time.",
    )
    changelog = models.TextField(
        blank=True, help_text="Summary of what changed from the prior version."
    )
    created_at = models.DateTimeField(auto_now_add=True)

    objects = PolicyVersionQuerySet.as_manager()

    class Meta:
        ordering = ["-effective_date"]

    def __str__(self):
        return f"{self.policy.name} v{self.version}"

    def save(self, *args, **kwargs):
        if self.is_active:
            PolicyVersion.objects.filter(policy=self.policy, is_active=True).exclude(
                pk=self.pk
            ).update(is_active=False)
        super().save(*args, **kwargs)


class PolicyNotification(models.Model):
    class Method(models.TextChoices):
        EMAIL = "EMAIL", _("Email")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="policy_notifications",
    )
    policy_version = models.ForeignKey(
        PolicyVersion, on_delete=models.CASCADE, related_name="notifications"
    )
    notified_at = models.DateTimeField(auto_now_add=True)
    method = models.CharField(
        max_length=20, choices=Method.choices, default=Method.EMAIL
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "policy_version"],
                name="unique_user_policy_notification",
            ),
        ]

    def __str__(self):
        return f"{self.user} notified of {self.policy_version}"


class PolicyConsent(models.Model):
    class Method(models.TextChoices):
        SIGNUP = "SIGNUP", _("Signup")
        RE_CONSENT = "RE_CONSENT", _("Re-consent")
        ADMIN_OVERRIDE = "ADMIN_OVERRIDE", _("Admin Override")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="policy_consents",
    )
    policy_version = models.ForeignKey(
        PolicyVersion, on_delete=models.CASCADE, related_name="consents"
    )
    consented_at = models.DateTimeField(auto_now_add=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(blank=True)
    method = models.CharField(max_length=20, choices=Method.choices)

    class Meta:
        ordering = ["-consented_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "policy_version"], name="unique_user_policy_version"
            ),
        ]

    def __str__(self):
        return f"{self.user} — {self.policy_version}"
