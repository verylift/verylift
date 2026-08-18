import logging

from django.contrib import admin, messages
from django.contrib.auth import get_user_model
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from django.contrib.auth.forms import AdminUserCreationForm as BaseUserCreationForm
from django.contrib.auth.forms import UserChangeForm as BaseUserChangeForm

from accounts.services import mask_api_key
from hevy_api.services import sync_user_lifts as sync_hevy_lifts
from liftosaur.services import sync_user_lifts
from wger.services import sync_wger_lifts

logger = logging.getLogger(__name__)

User = get_user_model()


class UserChangeForm(BaseUserChangeForm):
    class Meta(BaseUserChangeForm.Meta):
        model = User


class UserCreationForm(BaseUserCreationForm):
    class Meta(BaseUserCreationForm.Meta):
        model = User


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    form = UserChangeForm
    add_form = UserCreationForm

    list_display = (
        "username",
        "email",
        "display_name",
        "is_staff",
        "is_active",
        "acquisition_source",
    )
    list_filter = ("is_staff", "is_active", "acquisition_source")
    search_fields = ("username", "email", "display_name")
    ordering = ("username",)
    readonly_fields = (
        "date_joined",
        "last_login",
        "acquisition_source",
        "liftosaur_api_key_masked",
        "wger_api_token_masked",
        "hevy_api_key_masked",
    )
    actions = (
        "backfill_lift_history",
        "backfill_wger_lift_history",
        "backfill_hevy_lift_history",
    )

    fieldsets = (
        (None, {"fields": ("username", "password")}),
        ("Profile", {"fields": ("email", "display_name", "acquisition_source")}),
        (
            "Integrations",
            {
                "fields": (
                    "oidc_sub",
                    "liftosaur_api_key_masked",
                    "wger_instance_url",
                    "wger_api_token_masked",
                    "hevy_api_key_masked",
                )
            },
        ),
        (
            "Permissions",
            {
                "fields": (
                    "is_active",
                    "is_staff",
                    "is_superuser",
                    "groups",
                    "user_permissions",
                )
            },
        ),
        ("Dates", {"fields": ("date_joined", "last_login", "deactivated_at")}),
    )
    add_fieldsets = (
        (
            None,
            {
                "classes": ("wide",),
                "fields": (
                    "username",
                    "usable_password",
                    "password1",
                    "password2",
                    "email",
                    "display_name",
                    "is_staff",
                    "is_superuser",
                    "is_active",
                ),
            },
        ),
    )

    @admin.display(description="Liftosaur API key")
    def liftosaur_api_key_masked(self, obj):
        """Masked stand-in for the key, which is never rendered in the admin.

        The real field is left out of the fieldsets entirely (TASK-285), so it is
        not part of the change form and cannot be read or written here; this
        readonly display only tells an operator whether a key is connected.
        """
        return mask_api_key(obj.liftosaur_api_key) or "—"

    @admin.display(description="Wger API token")
    def wger_api_token_masked(self, obj):
        """Masked stand-in for the token, mirroring liftosaur_api_key_masked."""
        return mask_api_key(obj.wger_api_token) or "—"

    @admin.display(description="Hevy API key")
    def hevy_api_key_masked(self, obj):
        """Masked stand-in for the Hevy key, mirroring liftosaur_api_key_masked."""
        return mask_api_key(obj.hevy_api_key) or "—"

    @admin.action(description="Backfill lift history (last 12 months)")
    def backfill_lift_history(self, request, queryset):
        """Synchronously force a 12-month LiftHistory backfill for each user.

        Runs with force=True so the admin's explicit request bypasses the sync
        cooldown. Reports per-user failures rather than aborting the batch.
        """
        succeeded = 0
        for user in queryset:
            try:
                sync_user_lifts(user, force=True)
            except Exception:
                logger.exception(
                    "Admin lift history backfill failed for user %s", user.id
                )
                self.message_user(
                    request,
                    f"Backfill failed for {user.username}.",
                    level=messages.ERROR,
                )
            else:
                succeeded += 1

        if succeeded:
            self.message_user(
                request,
                f"Backfilled lift history for {succeeded} user(s).",
                level=messages.SUCCESS,
            )

    @admin.action(description="Backfill Wger lift history (last 12 months)")
    def backfill_wger_lift_history(self, request, queryset):
        """Synchronously force a 12-month Wger LiftHistory backfill per user."""
        succeeded = 0
        for user in queryset:
            try:
                sync_wger_lifts(user, force=True)
            except Exception:
                logger.exception(
                    "Admin Wger lift history backfill failed for user %s", user.id
                )
                self.message_user(
                    request,
                    f"Wger backfill failed for {user.username}.",
                    level=messages.ERROR,
                )
            else:
                succeeded += 1

        if succeeded:
            self.message_user(
                request,
                f"Backfilled Wger lift history for {succeeded} user(s).",
                level=messages.SUCCESS,
            )

    @admin.action(description="Backfill Hevy lift history (last 12 months)")
    def backfill_hevy_lift_history(self, request, queryset):
        """Synchronously force a Hevy API backfill for each user.

        Mirrors backfill_lift_history for the Hevy source.
        """
        succeeded = 0
        for user in queryset:
            try:
                sync_hevy_lifts(user, force=True)
            except Exception:
                logger.exception(
                    "Admin Hevy lift history backfill failed for user %s", user.id
                )
                self.message_user(
                    request,
                    f"Hevy backfill failed for {user.username}.",
                    level=messages.ERROR,
                )
            else:
                succeeded += 1

        if succeeded:
            self.message_user(
                request,
                f"Backfilled Hevy lift history for {succeeded} user(s).",
                level=messages.SUCCESS,
            )
