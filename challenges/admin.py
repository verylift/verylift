from django.contrib import admin

from .models import (
    Challenge,
    ChallengeInviteLink,
    ChallengeLift,
    ChallengeParticipant,
    CustomGoal,
    CustomGoalTarget,
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
