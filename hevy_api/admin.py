from django.contrib import admin

from .models import HevySyncLog


@admin.register(HevySyncLog)
class HevySyncLogAdmin(admin.ModelAdmin):
    list_display = ["user", "started_at", "success", "walk_complete", "completed_at"]
    list_filter = ["success", "walk_complete"]
    ordering = ["-started_at"]
    search_fields = ["user__username"]
