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


class NewsletterSubscriber(models.Model):
    email = models.EmailField(unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = _("Newsletter Subscriber")
        verbose_name_plural = _("Newsletter Subscribers")

    def __str__(self):
        return self.email
