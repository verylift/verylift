import logging

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils.html import format_html

from policies.models import Policy, PolicyConsent, PolicyVersion

logger = logging.getLogger(__name__)


class PolicyVersionInline(admin.TabularInline):
    model = PolicyVersion
    extra = 1
    fields = ("version", "url", "effective_date", "is_active", "changelog")
    ordering = ("-effective_date",)


@admin.register(Policy)
class PolicyAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "policy_type",
        "requires_consent",
        "gates_access",
        "active_version_label",
    )
    list_filter = ("policy_type", "requires_consent", "gates_access")
    search_fields = ("name", "slug")
    readonly_fields = ("id", "created_at", "updated_at")
    prepopulated_fields = {"slug": ("name",)}
    inlines = [PolicyVersionInline]

    @admin.display(description="Active version")
    def active_version_label(self, obj):
        version = obj.versions.filter(is_active=True).first()
        return version.version if version else "—"


@admin.register(PolicyVersion)
class PolicyVersionAdmin(admin.ModelAdmin):
    list_display = (
        "policy",
        "version",
        "effective_date",
        "is_active",
        "non_consented_link",
    )
    list_filter = ("policy", "is_active")
    search_fields = ("policy__name", "version")
    readonly_fields = ("id", "created_at")
    actions = ["mark_as_active", "send_notifications"]

    def _non_consented_qs(self, obj):
        User = get_user_model()
        consented_ids = PolicyConsent.objects.filter(policy_version=obj).values_list(
            "user_id", flat=True
        )
        return User.objects.filter(is_active=True).exclude(id__in=consented_ids)

    @admin.display(description="Without consent")
    def non_consented_link(self, obj):
        count = self._non_consented_qs(obj).count()
        label = f"{count} user{'s' if count != 1 else ''}"
        if obj.is_active:
            url = reverse("admin:policies_policyversion_non_consented", args=[obj.pk])
            return format_html('<a href="{}">{}</a>', url, label)
        return label

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path(
                "<uuid:version_id>/non-consented/",
                self.admin_site.admin_view(self.non_consented_view),
                name="policies_policyversion_non_consented",
            ),
        ]
        return custom_urls + urls

    def non_consented_view(self, request, version_id):
        from django.shortcuts import get_object_or_404

        version = get_object_or_404(PolicyVersion, pk=version_id)
        non_consented = self._non_consented_qs(version).order_by("email")
        context = {
            **self.admin_site.each_context(request),
            "version": version,
            "non_consented": non_consented,
            "opts": self.model._meta,
            "title": f"Users without consent: {version}",
        }
        return TemplateResponse(
            request, "admin/policies/policyversion/non_consented.html", context
        )

    @admin.action(description="Send policy update notifications to non-consented users")
    def send_notifications(self, request, queryset):
        from policies.notifications import notify_users_for_version

        total = 0
        for version in queryset:
            total += notify_users_for_version(version, request)
        self.message_user(request, f"Sent {total} notification(s).")

    @admin.action(description="Mark selected version as active")
    def mark_as_active(self, request, queryset):
        if queryset.count() != 1:
            self.message_user(
                request, "Select exactly one version to activate.", level="error"
            )
            return
        version = queryset.first()
        version.is_active = True
        version.save()
        self.message_user(request, f"{version} is now the active version.")


@admin.register(PolicyConsent)
class PolicyConsentAdmin(admin.ModelAdmin):
    list_display = ("user", "policy_version", "consented_at", "method", "ip_address")
    list_filter = ("policy_version__policy", "method")
    search_fields = ("user__email", "policy_version__policy__name")
    date_hierarchy = "consented_at"
    readonly_fields = (
        "id",
        "user",
        "policy_version",
        "consented_at",
        "ip_address",
        "user_agent",
        "method",
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
