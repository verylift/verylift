from django.contrib import admin

from .models import Notification


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ["user", "event_type", "is_read", "created_at"]
    list_filter = ["event_type", "is_read"]
    ordering = ["-created_at"]
    search_fields = ["user__username"]
