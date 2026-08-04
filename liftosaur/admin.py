from django.contrib import admin

from .models import Lift, LiftAlias, LiftosaurSyncLog


@admin.register(LiftosaurSyncLog)
class LiftosaurSyncLogAdmin(admin.ModelAdmin):
    list_display = ["user", "started_at", "success", "completed_at"]
    list_filter = ["success"]
    ordering = ["-started_at"]
    search_fields = ["user__username"]


@admin.register(Lift)
class LiftAdmin(admin.ModelAdmin):
    list_display = ["name", "is_liftosaur_builtin", "is_bodyweight_added"]
    list_filter = ["is_liftosaur_builtin", "is_bodyweight_added"]
    search_fields = ["name"]
    ordering = ["name"]


@admin.register(LiftAlias)
class LiftAliasAdmin(admin.ModelAdmin):
    list_display = ["from_name", "to_name"]
    search_fields = ["from_name", "to_name"]
    ordering = ["from_name"]
