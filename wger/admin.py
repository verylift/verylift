from django.contrib import admin

from .models import WgerLiftAlias, WgerSyncLog


@admin.register(WgerSyncLog)
class WgerSyncLogAdmin(admin.ModelAdmin):
    list_display = ["user", "started_at", "success", "completed_at"]
    list_filter = ["success"]
    ordering = ["-started_at"]
    search_fields = ["user__username"]


@admin.register(WgerLiftAlias)
class WgerLiftAliasAdmin(admin.ModelAdmin):
    list_display = ["from_name", "to_name"]
    search_fields = ["from_name", "to_name"]
    ordering = ["from_name"]
