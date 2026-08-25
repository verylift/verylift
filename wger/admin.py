from django.contrib import admin

from .models import WgerSyncLog


@admin.register(WgerSyncLog)
class WgerSyncLogAdmin(admin.ModelAdmin):
    list_display = ["user", "started_at", "success", "completed_at"]
    list_filter = ["success"]
    ordering = ["-started_at"]
    search_fields = ["user__username"]
