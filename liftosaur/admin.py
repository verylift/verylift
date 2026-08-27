from django.contrib import admin

from .models import Lift, LiftHistory, LiftosaurSyncLog


@admin.register(LiftosaurSyncLog)
class LiftosaurSyncLogAdmin(admin.ModelAdmin):
    list_display = ["user", "started_at", "success", "completed_at"]
    list_filter = ["success"]
    ordering = ["-started_at"]
    search_fields = ["user__username"]


@admin.register(LiftHistory)
class LiftHistoryAdmin(admin.ModelAdmin):
    """Read-only browser for the pooled per-lifter set history.

    This table is written exclusively by tracker sync/import services and
    repaired in bulk by dedicated management commands (restamp_lb_converted_
    lift_history, recanonicalize_lift_history) when something needs fixing --
    editing a row by hand here would desync it from the PointEarnEvents
    already scored off it. The admin is for inspecting a lifter's pooled
    history when troubleshooting a sync or a scoring dispute, not for editing
    it.

    Performance: the table is large and grows per user per sync, so list_display
    avoids extra relations beyond the FK, list_select_related covers the one FK
    it does show, and search is restricted to user__username -- the only
    column here backed by an index (the composite unique_together index is
    keyed on user first; lift/performed_at have no standalone index and an
    icontains search on them would force a sequential scan).
    """

    list_display = [
        "user",
        "lift",
        "performed_at",
        "weight_kg",
        "reps",
        "equipment",
        "source",
    ]
    list_select_related = ["user"]
    list_filter = ["source"]
    date_hierarchy = "performed_at"
    search_fields = ["user__username"]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Lift)
class LiftAdmin(admin.ModelAdmin):
    list_display = ["name", "is_liftosaur_builtin", "is_bodyweight_added"]
    list_filter = ["is_liftosaur_builtin", "is_bodyweight_added"]
    search_fields = ["name"]
    ordering = ["name"]
