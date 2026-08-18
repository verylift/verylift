from django.contrib import admin

from .models import HevySyncLog


@admin.register(HevySyncLog)
class HevySyncLogAdmin(admin.ModelAdmin):
    list_display = ["user", "started_at", "success", "completed_at"]
    list_filter = ["success"]
    ordering = ["-started_at"]
    search_fields = ["user__username"]
