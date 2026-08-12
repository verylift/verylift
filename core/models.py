from django.db import models
from django.utils.translation import gettext_lazy as _


class NewsletterSubscriber(models.Model):
    email = models.EmailField(unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = _("Newsletter Subscriber")
        verbose_name_plural = _("Newsletter Subscribers")

    def __str__(self):
        return self.email
