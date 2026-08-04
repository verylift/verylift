from django.contrib import admin

from .models import PointEarnEvent


@admin.register(PointEarnEvent)
class PointEarnEventAdmin(admin.ModelAdmin):
    list_display = [
        "user",
        "challenge",
        "lift",
        "points_earned",
        "is_current_best",
        "performed_at",
    ]
    list_filter = ["is_current_best", "lift"]
    ordering = ["-synced_at"]
    search_fields = ["user__username", "lift"]
