import csv

from django.contrib import admin
from django.http import HttpResponse

from .models import NewsletterSubscriber, SiteSettings


@admin.register(NewsletterSubscriber)
class NewsletterSubscriberAdmin(admin.ModelAdmin):
    list_display = ["email", "created_at"]
    ordering = ["-created_at"]
    search_fields = ["email"]
    actions = ["export_as_csv"]

    @admin.action(description="Export selected as CSV")
    def export_as_csv(self, request, queryset):
        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = (
            "attachment; filename=newsletter_subscribers.csv"
        )
        writer = csv.writer(response)
        writer.writerow(["email", "created_at"])
        for subscriber in queryset.order_by("-created_at"):
            writer.writerow([subscriber.email, subscriber.created_at.isoformat()])
        return response


@admin.register(SiteSettings)
class SiteSettingsAdmin(admin.ModelAdmin):
    """Singleton admin: always edits the one row, never lists/adds/deletes."""

    def has_add_permission(self, request):
        return not SiteSettings.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        obj = SiteSettings.load()
        return self.change_view(request, str(obj.pk), extra_context=extra_context)
