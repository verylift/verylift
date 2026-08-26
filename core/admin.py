import csv

from django.contrib import admin
from django.http import HttpResponse

from .models import LiftAlias, NewsletterSubscriber, SiteSettings

# Formula-injection guard for CSV export: a spreadsheet app treats a cell
# starting with any of these as a formula, not text. Django's EmailField
# validator is permissive enough to accept e.g. "-2+3@example.com" as a
# syntactically valid address, so a subscriber's own (valid) email can
# land here -- prefix with a single quote to force text interpretation.
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _csv_safe(value):
    return f"'{value}" if value.startswith(_FORMULA_PREFIXES) else value


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
            writer.writerow(
                [_csv_safe(subscriber.email), subscriber.created_at.isoformat()]
            )
        return response


@admin.register(LiftAlias)
class LiftAliasAdmin(admin.ModelAdmin):
    list_display = ["source", "from_name", "to_name"]
    list_filter = ["source"]
    search_fields = ["from_name", "to_name"]
    ordering = ["source", "from_name"]


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
