from django.contrib import admin

from .models import (
    Challenge,
    ChallengeEvent,
    ChallengeInviteLink,
    ChallengeLift,
    ChallengeParticipant,
    CustomGoal,
    CustomGoalTarget,
    RepTargetGoal,
    RepTargetGoalTarget,
)


@admin.register(Challenge)
class ChallengeAdmin(admin.ModelAdmin):
    list_display = [
        "name",
        "creator",
        "status",
        "start_date",
        "end_date",
        "plate_unit",
        "smallest_plate",
    ]
    list_filter = ["status", "plate_unit"]
    search_fields = ["name"]
    fields = [
        "name",
        "creator",
        "status",
        "start_date",
        "end_date",
        "history_window",
        "plate_unit",
        "smallest_plate",
    ]


@admin.register(ChallengeParticipant)
class ChallengeParticipantAdmin(admin.ModelAdmin):
    list_display = ["challenge", "user", "invite_status", "is_bailed"]
    list_filter = ["invite_status", "is_bailed"]
    search_fields = ["challenge__name", "user__username"]


@admin.register(ChallengeEvent)
class ChallengeEventAdmin(admin.ModelAdmin):
    """Read-only: the activity log is append-only, and an operator editing it
    would be rewriting the record of what happened. Add is off for the same
    reason -- a row here should only ever come from the action it describes."""

    list_display = ["challenge", "event_type", "actor", "created_at"]
    list_filter = ["event_type"]
    search_fields = ["challenge__name"]
    readonly_fields = ["challenge", "event_type", "actor", "metadata", "created_at"]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(ChallengeLift)
class ChallengeLiftAdmin(admin.ModelAdmin):
    list_display = ["challenge", "name"]
    search_fields = ["challenge__name", "name"]


@admin.register(ChallengeInviteLink)
class ChallengeInviteLinkAdmin(admin.ModelAdmin):
    # token is deliberately excluded everywhere on this page — an admin
    # changelist is not a place to spray live bearer tokens.
    list_display = ["challenge", "created_by", "created_at", "expires_at", "revoked_at"]
    list_filter = ["revoked_at"]


class CustomGoalTargetInline(admin.TabularInline):
    model = CustomGoalTarget
    extra = 0


@admin.register(CustomGoal)
class CustomGoalAdmin(admin.ModelAdmin):
    list_display = ["name", "participant", "source_method", "created_at"]
    list_filter = ["source_method"]
    search_fields = ["name", "participant__user__username"]
    inlines = [CustomGoalTargetInline]


@admin.register(CustomGoalTarget)
class CustomGoalTargetAdmin(admin.ModelAdmin):
    # Kept as an inline on CustomGoal too (above) for editing a goal's whole
    # ladder in place; this standalone list exists so an operator can search
    # across every participant's targets for a lift (e.g. tracking down an
    # outlier feeding a scoring dispute) without opening each goal in turn.
    list_display = ["goal", "lift", "rep_count", "target_weight"]
    list_select_related = ["goal", "goal__participant"]
    search_fields = ["lift", "goal__name", "goal__participant__user__username"]
    ordering = ["lift", "rep_count"]


class RepTargetGoalTargetInline(admin.TabularInline):
    model = RepTargetGoalTarget
    extra = 0


@admin.register(RepTargetGoal)
class RepTargetGoalAdmin(admin.ModelAdmin):
    list_display = ["name", "participant", "source_method", "created_at"]
    list_filter = ["source_method"]
    search_fields = ["name", "participant__user__username"]
    inlines = [RepTargetGoalTargetInline]


@admin.register(RepTargetGoalTarget)
class RepTargetGoalTargetAdmin(admin.ModelAdmin):
    # Same standalone-plus-inline rationale as CustomGoalTarget above.
    list_display = ["goal", "lift", "target_weight", "target_reps"]
    list_select_related = ["goal", "goal__participant"]
    search_fields = ["lift", "goal__name", "goal__participant__user__username"]
    ordering = ["lift"]
